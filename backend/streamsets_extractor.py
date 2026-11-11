from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Response
from fastapi.responses import StreamingResponse

from audit import write_audit_event
from auth import require_permission

logger = logging.getLogger(__name__)

streamsets_router = APIRouter(prefix="/api/streamsets", tags=["Data Studio - StreamSets"])

PERM = "runStreamsetsExtractor"
SECRET_PERM = "viewStreamsetsSecrets"   # reveal passwords / keys / secret files, one at a time

_config: Optional[Dict[str, Any]] = None
_scripts: Dict[str, Any] = {}

LIST_TTL = 60            # seconds the pipeline list is reused before asking Control Hub again
LATEST_TTL = 60          # same for "which commit is latest" per pipeline
COMMIT_CACHE_MAX = 300   # commits never change, so these are kept until evicted
MAX_FRAGMENT_DEPTH = 3
MAX_VALUE_CHARS = 20_000
MAX_REPORT_BYTES = 64 * 1024 * 1024
HEARTBEAT_SECONDS = 15
RESOURCE_TTL = 300                  # a resolved resource file is reused for this long
MAX_RESOURCE_BYTES = 64 * 1024
DEFAULT_RESOURCES_DIR = "/projects/var/lib/sdc-resources"

PIPELINE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,300}$")
# (inside a list/map setting the value is JSON text, so its quotes arrive escaped as \")
LOAD_RESOURCE_RE = re.compile(r"""runtime:loadResource(?:Raw)?\(\s*\\?["']([^"'\\]+)\\?["']\s*,\s*(true|false)\s*\)""", re.I)
RESOURCE_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_. /-]{0,299}$")
PARAM_REF_RE = re.compile(r"\$\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}")
SECRET_RE = re.compile(r"passw|pwd|passphrase|secret|private_?key|token|api_?key|access_?key|credential", re.I)
MASK = "********"
KINDS = ("SOURCE", "PROCESSOR", "TARGET", "EXECUTOR")

# First match wins, so the specific names sit above the generic ones.
TECHNOLOGIES = [
    ("snowflake", "Snowflake"), ("oracle", "Oracle"), ("sqlserver", "SQL Server"), ("mysql", "MySQL"),
    ("postgres", "PostgreSQL"), ("teradata", "Teradata"), ("jdbc", "JDBC"), ("kafka", "Kafka"),
    ("kinesis", "Amazon Kinesis"), ("_s3_", "Amazon S3"), ("amazons3", "Amazon S3"), ("azure", "Azure"),
    ("gcs", "Google Cloud Storage"), ("bigquery", "BigQuery"), ("hdfs", "HDFS"), ("hive", "Hive"),
    ("mongodb", "MongoDB"), ("elasticsearch", "Elasticsearch"), ("salesforce", "Salesforce"),
    ("sftp", "SFTP"), ("ftp", "FTP"), ("http", "HTTP"), ("spooldir", "Directory"), ("directory", "Directory"),
    ("jython", "Jython"), ("groovy", "Groovy"), ("javascript", "JavaScript"), ("fragment", "Fragment"),
    ("finishpipeline", "Pipeline Finisher"), ("devnull", "Trash"), ("trash", "Trash"),
]


def set_streamsets_dependencies(config: Optional[Dict[str, Any]], scripts: Optional[Dict[str, Any]] = None,
                                it_validation: Optional[Dict[str, Any]] = None):
    """Called once from app.py with the 'streamsets_extractor' and 'scripts' sections of dq_config.json."""
    global _config, _scripts, _token, _semaphore
    _config = config or None
    _scripts = dict(scripts or {})
    # IT Checks already knows where DQ_IT_Validation lives on this server - the extractor sits next to it.
    if (it_validation or {}).get("script_path"):
        _scripts["it_script_dir"] = os.path.dirname(it_validation["script_path"])
    _token = None
    _semaphore = threading.BoundedSemaphore(int((_config or {}).get("max_concurrent_runs") or 2))
    with _cache_lock:
        _list_cache.clear()
        _latest_cache.clear()
        _commit_cache.clear()
    if not _configured():
        logger.warning("⚠️ streamsets_extractor config not found - StreamSets Extractor endpoints will be unavailable")
    else:
        logger.info("StreamSets Extractor configured for %s", urlparse(_config["url"]).netloc)


def _configured() -> bool:
    return bool(_config and _config.get("url") and _config.get("username") and _config.get("password"))


def _settings() -> Dict[str, Any]:
    if not _configured():
        raise HTTPException(status_code=503,
                            detail="StreamSets Extractor is not configured - add a 'streamsets_extractor' section to dq_config.json")
    cfg = _config
    candidates = [os.path.join(d, "Streamsets", "Simplified_extractor.py")
                  for d in (_scripts.get("it_script_dir"), _scripts.get("script_dir")) if d]
    script = cfg.get("script_path") or next((c for c in candidates if os.path.isfile(c)), candidates[0] if candidates else "")
    return {
        "url": cfg["url"].strip().rstrip("/"),
        "username": cfg["username"],
        "password": cfg["password"],
        "verify": cfg.get("ca_bundle") or bool(cfg.get("verify_ssl", False)),
        "timeout": int(cfg.get("request_timeout") or 30),
        "script_path": script,
        "python_path": cfg.get("python_path") or _scripts.get("python_path") or sys.executable,
        "max_per_run": int(cfg.get("max_pipelines_per_run") or 25),
        "run_timeout": int(cfg.get("run_timeout_seconds") or 1800),
        "thread_pool_size": int(cfg.get("thread_pool_size") or 5),
    }


# ============================================================================
# Control Hub client
# ============================================================================

_token: Optional[str] = None
_token_lock = threading.Lock()
_cache_lock = threading.Lock()
_list_cache: Dict[str, Any] = {}
_latest_cache: Dict[str, Any] = {}
_commit_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_semaphore = threading.BoundedSemaphore(2)
_resource_cache: Dict[str, Any] = {}   # path -> (time, value, host)
_sdc_cache: Dict[str, Any] = {}


class _NotFound(Exception):
    pass


def _login(cfg) -> str:
    global _token
    try:
        res = requests.post(f"{cfg['url']}/security/public-rest/v1/authentication/login",
                            json={"userName": cfg["username"], "password": cfg["password"]},
                            headers={"Content-Type": "application/json", "X-Requested-By": "SCH"},
                            verify=cfg["verify"], timeout=cfg["timeout"])
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Couldn't reach StreamSets: {type(e).__name__}")
    token = res.headers.get("X-SS-User-Auth-Token") if res.status_code == 200 else None
    if not token:
        logger.error("StreamSets login failed: HTTP %s", res.status_code)
        raise HTTPException(status_code=502, detail=f"StreamSets rejected the service account login (HTTP {res.status_code})")
    _token = token
    return token


def _get(path: str, params: Optional[Dict[str, Any]] = None):
    """GET a Control Hub REST path. Logs in on first use and once more if the token has expired."""
    cfg = _settings()
    url = f"{cfg['url']}/" + "/".join(quote(p, safe="") for p in path.split("/"))
    for attempt in (1, 2):
        with _token_lock:
            token = _token or _login(cfg)
        try:
            res = requests.get(url, params=params, verify=cfg["verify"], timeout=cfg["timeout"],
                               headers={"X-SS-User-Auth-Token": token, "X-Requested-By": "SCH"})
        except requests.Timeout:
            raise HTTPException(status_code=504, detail="StreamSets took too long to answer - try again")
        except requests.RequestException as e:
            raise HTTPException(status_code=502, detail=f"Couldn't reach StreamSets: {type(e).__name__}")
        if res.status_code in (401, 403) and attempt == 1:
            with _token_lock:
                if _token == token:
                    _login(cfg)
            continue
        if res.status_code == 404:
            raise _NotFound(path)
        if res.status_code != 200:
            logger.warning("StreamSets GET %s -> HTTP %s", path, res.status_code)
            raise HTTPException(status_code=502, detail=f"StreamSets returned HTTP {res.status_code}")
        if not res.text.strip():
            return None
        try:
            data = res.json()
        except ValueError:
            raise HTTPException(status_code=502, detail="StreamSets sent back something that isn't JSON")
        return data["data"] if isinstance(data, dict) and "data" in data else data
    raise HTTPException(status_code=502, detail="StreamSets rejected the service account")


def _get_commit(commit_id: str) -> Dict[str, Any]:
    with _cache_lock:
        if commit_id in _commit_cache:
            _commit_cache.move_to_end(commit_id)
            return _commit_cache[commit_id]
    commit = _get(f"pipelinestore/rest/v1/pipelineCommit/{commit_id}")
    if not isinstance(commit, dict):
        raise _NotFound(commit_id)
    with _cache_lock:
        _commit_cache[commit_id] = commit
        while len(_commit_cache) > COMMIT_CACHE_MAX:
            _commit_cache.popitem(last=False)
    return commit


def _definition(commit: Dict[str, Any]) -> Dict[str, Any]:
    raw = commit.get("pipelineDefinition")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=502, detail="StreamSets returned a pipeline definition that can't be read")
    return parsed


def _iso(value) -> Optional[str]:
    """Control Hub sends epoch millis; the page wants ISO."""
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    return value if isinstance(value, str) and value else None


def _labels(raw) -> List[str]:
    """Control Hub sends labels as plain strings or as {'id', 'label', 'parentId', ...} objects."""
    out = []
    for x in raw if isinstance(raw, list) else []:
        text = (x.get("label") or x.get("name") or str(x.get("id") or "").split(":")[0]) if isinstance(x, dict) else x
        if text and str(text) not in out:
            out.append(str(text))
    return out


def _pipelines(refresh: bool = False) -> Dict[str, Any]:
    with _cache_lock:
        cached = _list_cache.get("value")
        if cached and not refresh and time.time() - _list_cache["at"] < LIST_TTL:
            return cached
    raw = _get("pipelinestore/rest/v1/pipelines") or []
    if not isinstance(raw, list):
        raise HTTPException(status_code=502, detail="StreamSets sent an unexpected pipeline list")
    items = []
    for p in raw:
        if not isinstance(p, dict) or not p.get("pipelineId") or p.get("fragment") is True:
            continue
        labels = p.get("pipelineLabels") or p.get("labels") or []
        items.append({
            "id": p["pipelineId"],
            "name": p.get("name") or p.get("title") or p["pipelineId"],
            "description": p.get("description") or "",
            "version": p.get("version"),
            "commit_id": p.get("commitId"),
            "engine": p.get("executorType"),
            "labels": _labels(labels),
            "last_modified": _iso(p.get("lastModified") or p.get("lastModifiedOn")),
            "modified_by": p.get("lastModifier") or p.get("lastModifiedBy") or p.get("committer"),
        })
    items.sort(key=lambda x: x["name"].lower())
    value = {"pipelines": items, "total": len(items), "fetched_at": datetime.now(timezone.utc).isoformat()}
    with _cache_lock:
        _list_cache.update(value=value, at=time.time())
    return value


def _latest(pipeline_id: str, refresh: bool) -> Dict[str, Any]:
    with _cache_lock:
        hit = _latest_cache.get(pipeline_id)
        if hit and not refresh and time.time() - hit[0] < LATEST_TTL:
            return hit[1]
    latest = _get(f"pipelinestore/rest/v1/pipeline/{pipeline_id}/latest")
    if not isinstance(latest, dict) or not latest.get("commitId"):
        raise _NotFound(pipeline_id)
    with _cache_lock:
        _latest_cache[pipeline_id] = (time.time(), latest)
    return latest


# ============================================================================
# Pipeline definition -> what the page shows
# ============================================================================

def _kind(stage: Dict[str, Any]) -> str:
    kind = str((stage.get("uiInfo") or {}).get("stageType") or "").upper()
    if kind in KINDS:
        return kind
    name = str(stage.get("stageName") or "").lower()
    if "executor" in name:
        return "EXECUTOR"
    if "origin" in name or name.endswith("source"):
        return "SOURCE"
    if "destination" in name or name.endswith("target"):
        return "TARGET"
    return "PROCESSOR"


def _technology(stage: Dict[str, Any]) -> str:
    hay = f"_{stage.get('library', '')}_{stage.get('stageName', '')}_".lower().replace("-", "_")
    for needle, label in TECHNOLOGIES:
        if needle in hay:
            return label
    return "StreamSets"


def _type_name(stage_name: str) -> str:
    """com_..._expression_ExpressionDProcessor -> 'Expression'."""
    last = (stage_name or "").split("_")[-1]
    last = re.sub(r"D?(Source|Processor|Target|Executor)$", "", last) or last
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", last).strip() or stage_name


def _display(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "\n… (truncated)"


def _is_secret(name: str) -> bool:
    return bool(SECRET_RE.search(str(name).split(".")[-1]))


def _constants(pdef: Dict[str, Any]) -> Dict[str, str]:
    out = {}
    for item in pdef.get("configuration") or []:
        if isinstance(item, dict) and item.get("name") == "constants" and isinstance(item.get("value"), list):
            for c in item["value"]:
                if isinstance(c, dict) and c.get("key"):
                    out[str(c["key"])] = "" if c.get("value") is None else str(c["value"])
    return out


def _config_items(stage: Dict[str, Any], constants: Dict[str, str], prefix: str) -> List[Dict[str, Any]]:
    items = []
    for item in stage.get("configuration") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        value = item.get("value")
        if value is None or value == "" or value == [] or value == {}:
            continue
        secret = _is_secret(item["name"]) and isinstance(value, str) and not value.strip().startswith("${")
        shown = MASK if secret else _display(value)
        params = []
        if not secret:
            for ref in dict.fromkeys(PARAM_REF_RE.findall(shown)):
                key = prefix + ref if prefix and prefix + ref in constants else ref
                known = key in constants
                params.append({"name": key, "value": (MASK if _is_secret(key) else constants[key]) if known else None})
        items.append({"name": item["name"], "value": shown, "secret": secret, "params": params})
    return items


def _wire(stages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fill upstream/downstream (records) and triggered_by/triggers (events) from the lanes,
    and return the stages in flow order."""
    data_from: Dict[str, List[str]] = {}
    event_from: Dict[str, List[str]] = {}
    for s in stages:
        for lane in s["_out"]:
            data_from.setdefault(lane, []).append(s["instance"])
        for lane in s["_events"]:
            event_from.setdefault(lane, []).append(s["instance"])
    by_name = {s["instance"]: s for s in stages}

    def feeding(s, lanes):
        return list(dict.fromkeys(p for lane in s["_in"] for p in lanes.get(lane, []) if p != s["instance"]))

    taking: Dict[str, List[str]] = {}
    for s in stages:
        for lane in s["_in"]:
            taking.setdefault(lane, []).append(s["instance"])

    for s in stages:
        s["upstream"] = feeding(s, data_from)
        s["triggered_by"] = feeding(s, event_from)
        s["downstream"], s["triggers"] = [], []
        # A stage with several outputs (Stream Selector) - which lane feeds which stages, so the
        # page can say "records where X go to Y" instead of guessing from the order.
        if len(s["_out"]) > 1:
            s["routes"] = [{"lane": lane, "to": taking.get(lane, [])} for lane in s["_out"]]
    for s in stages:
        for key, back in (("upstream", "downstream"), ("triggered_by", "triggers")):
            for up in s[key]:
                if up in by_name and s["instance"] not in by_name[up][back]:
                    by_name[up][back].append(s["instance"])
    # Kahn's sort, ties kept in the order the designer saved them.
    order, waiting = [], {s["instance"]: len([u for u in s["upstream"] + s["triggered_by"] if u in by_name]) for s in stages}
    ready = [s for s in stages if waiting[s["instance"]] == 0]
    while ready:
        s = ready.pop(0)
        order.append(s)
        for down in s["downstream"] + s["triggers"]:
            waiting[down] -= 1
            if waiting[down] == 0:
                ready.append(by_name[down])
        ready.sort(key=lambda x: x["_index"])
    seen = {s["instance"] for s in order}
    order += [s for s in stages if s["instance"] not in seen]   # a cycle can't happen, but never drop a stage
    for s in order:
        for k in ("_in", "_out", "_events", "_index"):
            s.pop(k, None)
    return order


def _build_stages(raw_stages, pdef, constants, prefix="", depth=0) -> List[Dict[str, Any]]:
    inline = {f.get("fragmentInstanceId"): f for f in pdef.get("fragments") or []
              if isinstance(f, dict) and f.get("fragmentInstanceId")}
    # Fetch the distinct fragment commits in parallel the first time; after that they come from cache.
    commit_ids = list(dict.fromkeys(
        (s.get("uiInfo") or {}).get("pipelineCommitId") for s in raw_stages
        if isinstance(s, dict) and (s.get("uiInfo") or {}).get("fragmentGroupStage")
        and (s.get("uiInfo") or {}).get("pipelineCommitId")
        and not (inline.get((s.get("uiInfo") or {}).get("fragmentInstanceId")) or {}).get("stages")))
    fetched: Dict[str, Any] = {}
    if commit_ids and depth < MAX_FRAGMENT_DEPTH:
        def load(cid):
            try:
                return cid, _definition(_get_commit(cid))
            except (_NotFound, HTTPException) as e:
                return cid, e
        with ThreadPoolExecutor(max_workers=min(6, len(commit_ids))) as pool:
            fetched = dict(pool.map(load, commit_ids))

    out = []
    for index, s in enumerate(raw_stages):
        if not isinstance(s, dict):
            continue
        ui = s.get("uiInfo") or {}
        stage = {
            "instance": s.get("instanceName") or f"stage_{index}",
            "label": ui.get("label") or s.get("instanceName") or "Unnamed stage",
            "description": ui.get("description") or "",
            "kind": _kind(s),
            "stage_name": s.get("stageName") or "",
            "type_name": _type_name(s.get("stageName") or ""),
            "library": s.get("library") or "",
            "technology": _technology(s),
            "config": _config_items(s, constants, prefix),
            "fragment": None,
            "_in": list(s.get("inputLanes") or []),
            "_out": list(s.get("outputLanes") or []),
            "_events": list(s.get("eventLanes") or []),
            "_index": index,
        }
        if ui.get("fragmentGroupStage"):
            frag = {
                "instance_id": ui.get("fragmentInstanceId"),
                "name": ui.get("fragmentName") or ui.get("fragmentId") or "Fragment",
                "fragment_id": ui.get("fragmentId"),
                "pipeline_id": ui.get("pipelineId"),
                "commit_id": ui.get("pipelineCommitId"),
                "version": ui.get("pipelineCommitLabel"),
                "prefix": ui.get("parameterNamePrefix") or "",
                "stages": [],
                "error": None,
            }
            inner_def = inline.get(frag["instance_id"])
            if not (inner_def or {}).get("stages"):
                inner_def = fetched.get(frag["commit_id"])
            if depth >= MAX_FRAGMENT_DEPTH:
                frag["error"] = "Nested too deep to expand"
            elif isinstance(inner_def, dict) and isinstance(inner_def.get("stages"), list):
                frag["stages"] = _build_stages(inner_def["stages"], inner_def, constants, frag["prefix"], depth + 1)
            elif isinstance(inner_def, HTTPException):
                frag["error"] = f"Couldn't load this fragment: {inner_def.detail}"
            else:
                frag["error"] = "This fragment's commit wasn't found in StreamSets"
            stage["fragment"] = frag
        out.append(stage)
    return _wire(out)


def _flatten(stages):
    for s in stages:
        yield s
        if s["fragment"]:
            yield from _flatten(s["fragment"]["stages"])


def _detail(pipeline_id: str, refresh: bool) -> Dict[str, Any]:
    listed = next((p for p in _pipelines()["pipelines"] if p["id"] == pipeline_id), None)
    latest = _latest(pipeline_id, refresh)
    commit = _get_commit(latest["commitId"])
    pdef = _definition(commit)
    constants = _constants(pdef)
    stages = _build_stages(pdef.get("stages") or [], pdef, constants)

    used_by: Dict[str, List[str]] = {}
    for s in _flatten(stages):
        for c in s["config"]:
            for p in c["params"]:
                users = used_by.setdefault(p["name"], [])
                if s["instance"] not in users:
                    users.append(s["instance"])
    parameters = [{"name": k, "value": MASK if _is_secret(k) else v, "used_by": used_by.get(k, [])}
                  for k, v in sorted(constants.items())]

    leaf = [s for s in _flatten(stages) if not s["fragment"]]
    counts = {k.lower(): sum(1 for s in leaf if s["kind"] == k) for k in KINDS}
    counts["fragments"] = sum(1 for s in _flatten(stages) if s["fragment"])
    counts["fragment_errors"] = sum(1 for s in _flatten(stages) if s["fragment"] and s["fragment"]["error"])
    counts["parameters"] = len(parameters)

    return {
        "id": pipeline_id,
        "name": (listed or {}).get("name") or pdef.get("title") or pipeline_id,
        "description": pdef.get("description") or (listed or {}).get("description") or "",
        "labels": (listed or {}).get("labels") or [],
        "engine": (listed or {}).get("engine"),
        "version": latest.get("version") or commit.get("version"),
        "commit_id": latest.get("commitId"),
        "commit_message": latest.get("commitMessage") or "",
        "committed_by": latest.get("committer") or (listed or {}).get("modified_by"),
        "committed_at": _iso(latest.get("commitTime")) or (listed or {}).get("last_modified"),
        "counts": counts,
        "stages": stages,
        "parameters": parameters,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================================
# Routes
# ============================================================================

def _check_id(pipeline_id: str) -> str:
    if not PIPELINE_ID_RE.match(pipeline_id or ""):
        raise HTTPException(status_code=400, detail="That isn't a valid pipeline id")
    return pipeline_id


def _extractor_status(cfg) -> Dict[str, Any]:
    if not cfg["script_path"]:
        return {"ready": False, "reason": "No extractor script configured (set streamsets_extractor.script_path or scripts.script_dir)"}
    if not os.path.isfile(cfg["script_path"]):
        return {"ready": False, "reason": f"The extractor script wasn't found at {cfg['script_path']} - "
                                          "set streamsets_extractor.script_path in dq_config.json"}
    if not (os.path.isfile(cfg["python_path"]) or shutil.which(cfg["python_path"])):
        return {"ready": False, "reason": "The Python interpreter for the extractor wasn't found on the server"}
    return {"ready": True, "reason": None}


@streamsets_router.get("/health")
def health(auth: dict = Depends(require_permission(PERM))):
    cfg = _settings()
    return {
        "configured": True,
        "server": urlparse(cfg["url"]).netloc,
        "extractor": _extractor_status(cfg),
        "max_per_run": cfg["max_per_run"],
        "resources": importlib.util.find_spec("paramiko") is not None,
    }


@streamsets_router.get("/pipelines")
def list_pipelines(refresh: bool = Query(False), auth: dict = Depends(require_permission(PERM))):
    try:
        return _pipelines(refresh)
    except _NotFound:
        raise HTTPException(status_code=502, detail="StreamSets has no pipeline store at that URL")


@streamsets_router.get("/pipelines/{pipeline_id}")
def pipeline_detail(pipeline_id: str, refresh: bool = Query(False), auth: dict = Depends(require_permission(PERM))):
    _check_id(pipeline_id)
    try:
        return _detail(pipeline_id, refresh)
    except _NotFound:
        raise HTTPException(status_code=404, detail="That pipeline wasn't found in StreamSets - it may have been deleted")


# ============================================================================
# runtime:loadResource() files - read from the Data Collectors on request
# ============================================================================

def _resource_refs(detail: Dict[str, Any]) -> Dict[str, bool]:
    """Every resource file the pipeline loads -> True when any use of it should stay hidden
    (loaded as restricted, used by a secret setting/parameter, or named like a secret)."""
    refs: Dict[str, bool] = {}

    def scan(text, secret):
        for path, restricted in LOAD_RESOURCE_RE.findall(text or ""):
            refs[path] = refs.get(path, False) or secret or restricted.lower() == "true" or bool(SECRET_RE.search(path))

    for s in _flatten(detail["stages"]):
        for c in s["config"]:
            scan(c["value"], c["secret"] or _is_secret(c["name"]))
    for p in detail["parameters"]:
        scan(p["value"], _is_secret(p["name"]))
    return refs


def _sdc_hosts() -> List[str]:
    with _cache_lock:
        hit = _sdc_cache.get("hosts")
        if hit and time.time() - hit[0] < RESOURCE_TTL:
            return hit[1]
    sdcs = _get("jobrunner/rest/v1/sdcs") or []
    hosts = [h for h in dict.fromkeys(urlparse(s.get("httpUrl") or "").hostname
                                      for s in sdcs if isinstance(s, dict)) if h]
    with _cache_lock:
        _sdc_cache["hosts"] = (time.time(), hosts)
    return hosts


def _read_resource(path: str):
    """Same approach as Final_pipeline_extractor.py: SFTP to each Data Collector until one has the file."""
    try:
        import paramiko
    except ImportError:
        raise HTTPException(status_code=503, detail="Showing resource file values needs the 'paramiko' package on the SmartHub server")
    cfg, raw = _settings(), _config or {}
    user = raw.get("ssh_username") or cfg["username"]
    password = raw.get("ssh_password") or cfg["password"]
    port = int(raw.get("ssh_port") or 22)
    base = (raw.get("resources_dir") or DEFAULT_RESOURCES_DIR).rstrip("/")
    tried = []
    for host in _sdc_hosts():
        tried.append(host)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())   # as the extractor script does
        try:
            ssh.connect(host, port, user, password, timeout=10, banner_timeout=10, auth_timeout=10,
                        allow_agent=False, look_for_keys=False)
            with ssh.open_sftp() as sftp, sftp.open(f"{base}/{path}", "r") as f:
                data = f.read(MAX_RESOURCE_BYTES + 1)
        except (OSError, paramiko.SSHException) as e:   # not on this host, or the host can't be reached
            logger.info("Resource %s not read from %s: %s", path, host, type(e).__name__)
            continue
        finally:
            ssh.close()
        text = data[:MAX_RESOURCE_BYTES].decode("utf-8", "replace").strip()
        return text + ("\n… (truncated)" if len(data) > MAX_RESOURCE_BYTES else ""), host, tried
    return None, None, tried


@streamsets_router.get("/resource")
def resource_value(pipeline_id: str = Query(...), path: str = Query(...), auth: dict = Depends(require_permission(PERM))):
    """The contents of a resource file a pipeline loads with runtime:loadResource(). Only files the
    pipeline actually references can be read, and secret ones are never sent to the browser."""
    _check_id(pipeline_id)
    if not RESOURCE_PATH_RE.match(path) or ".." in path.split("/"):
        raise HTTPException(status_code=400, detail="That isn't a valid resource file path")
    try:
        detail = _detail(pipeline_id, False)
    except _NotFound:
        raise HTTPException(status_code=404, detail="That pipeline wasn't found in StreamSets - it may have been deleted")
    refs = _resource_refs(detail)
    if path not in refs:
        raise HTTPException(status_code=404, detail="This pipeline doesn't load that resource file")
    if refs[path]:
        return {"path": path, "secret": True, "value": None, "host": None}
    with _cache_lock:
        hit = _resource_cache.get(path)
    if hit and time.time() - hit[0] < RESOURCE_TTL:
        return {"path": path, "secret": False, "value": hit[1], "host": hit[2]}
    value, host, tried = _read_resource(path)
    logger.info("%s looked up StreamSets resource %s for %s: %s", auth["user"]["email"], path, pipeline_id,
                f"found on {host}" if value is not None else "not found")
    if value is None:
        where = ", ".join(tried) if tried else "no Data Collectors are registered"
        raise HTTPException(status_code=404, detail=f"Couldn't find {path} on the Data Collectors ({where})")
    with _cache_lock:
        _resource_cache[path] = (time.time(), value, host)
    return {"path": path, "secret": False, "value": value, "host": host}


def _raw_stages(pdef: Dict[str, Any], depth: int = 0):
    """Every stage in the definition as StreamSets stores it, fragments included (same walk as _build_stages)."""
    inline = {f.get("fragmentInstanceId"): f for f in pdef.get("fragments") or []
              if isinstance(f, dict) and f.get("fragmentInstanceId")}
    for s in pdef.get("stages") or []:
        if not isinstance(s, dict):
            continue
        yield s
        ui = s.get("uiInfo") or {}
        if not ui.get("fragmentGroupStage") or depth >= MAX_FRAGMENT_DEPTH:
            continue
        inner = inline.get(ui.get("fragmentInstanceId"))
        if not (inner or {}).get("stages") and ui.get("pipelineCommitId"):
            try:
                inner = _definition(_get_commit(ui["pipelineCommitId"]))
            except (_NotFound, HTTPException):
                inner = None
        if isinstance(inner, dict):
            yield from _raw_stages(inner, depth + 1)


@streamsets_router.get("/secret")
def secret_value(
    response: Response,
    pipeline_id: str = Query(...),
    kind: str = Query(..., pattern="^(param|config|resource)$"),
    name: str = Query(..., min_length=1, max_length=300),
    stage: Optional[str] = Query(None, max_length=300),
    auth: dict = Depends(require_permission(PERM, SECRET_PERM)),
):
    """The real value behind a hidden secret. Only for people holding viewStreamsetsSecrets;
    each reveal is logged (who / what - never the value) and never cached by the browser."""
    _check_id(pipeline_id)
    response.headers["Cache-Control"] = "no-store"
    try:
        latest = _latest(pipeline_id, False)
        pdef = _definition(_get_commit(latest["commitId"]))
    except _NotFound:
        raise HTTPException(status_code=404, detail="That pipeline wasn't found in StreamSets - it may have been deleted")

    value, host = None, None
    if kind == "param":
        constants = _constants(pdef)
        if name not in constants:
            raise HTTPException(status_code=404, detail="This pipeline has no parameter with that name")
        value = constants[name]
    elif kind == "config":
        if not stage:
            raise HTTPException(status_code=400, detail="Say which stage the setting belongs to")
        raw = next((s for s in _raw_stages(pdef) if s.get("instanceName") == stage), None)
        item = next((c for c in (raw or {}).get("configuration") or []
                     if isinstance(c, dict) and c.get("name") == name), None)
        if item is None:
            raise HTTPException(status_code=404, detail="That setting wasn't found on this stage")
        value = _display(item.get("value"))
    else:
        if not RESOURCE_PATH_RE.match(name) or ".." in name.split("/"):
            raise HTTPException(status_code=400, detail="That isn't a valid resource file path")
        if name not in _resource_refs(_detail(pipeline_id, False)):
            raise HTTPException(status_code=404, detail="This pipeline doesn't load that resource file")
        value, host, tried = _read_resource(name)   # secrets are read fresh, never cached server-side
        if value is None:
            where = ", ".join(tried) if tried else "no Data Collectors are registered"
            raise HTTPException(status_code=404, detail=f"Couldn't find {name} on the Data Collectors ({where})")

    # fail closed: no audit row, no value
    try:
        write_audit_event(action="streamsets_secret_viewed", performed_by=auth["user"]["email"], target=pipeline_id,
                          details=f"Viewed {kind} {stage + '/' if stage else ''}{name}")
    except Exception:
        logger.exception("Couldn't audit a StreamSets secret reveal")
        raise HTTPException(status_code=503, detail="Couldn't record the audit entry, so the value wasn't shown - try again")
    return {"kind": kind, "name": name, "stage": stage, "value": value, "host": host}


def _sse(event: Dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str, ensure_ascii=False)}\n\n"


def _report_name(names: List[str]) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if len(names) == 1:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", names[0]).strip("_")[:80] or "pipeline"
        return f"{safe}_{stamp}.json"
    return f"STREAMSETS_PIPELINE_REPORT_{stamp}.json"


@streamsets_router.get("/extract")
async def extract(
    pipeline_id: List[str] = Query(..., description="Pipeline ids to extract, repeat the parameter for each"),
    auth: dict = Depends(require_permission(PERM)),
):
    """Run the extractor for the chosen pipelines and stream its output. Events (data JSON):
    status, log, progress {pipeline_id, state, error}, report {filename, report}, error, done {ok, failed}."""
    cfg = _settings()
    ids = list(dict.fromkeys(_check_id(p) for p in pipeline_id))
    if not ids:
        raise HTTPException(status_code=400, detail="Pick at least one pipeline")
    if len(ids) > cfg["max_per_run"]:
        raise HTTPException(status_code=400, detail=f"Pick at most {cfg['max_per_run']} pipelines per run")
    status = _extractor_status(cfg)
    if not status["ready"]:
        raise HTTPException(status_code=503, detail=status["reason"])

    known = {p["id"]: p["name"] for p in (await asyncio.to_thread(_pipelines))["pipelines"]}
    if any(i not in known for i in ids):   # maybe created a minute ago - look again before refusing
        known = {p["id"]: p["name"] for p in (await asyncio.to_thread(_pipelines, True))["pipelines"]}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise HTTPException(status_code=404, detail=f"Not found in StreamSets: {', '.join(unknown)}")

    user = (auth.get("user") or {}).get("email") or "unknown"
    names = {i: known[i] for i in ids}
    by_name = {v: k for k, v in names.items()}

    semaphore = _semaphore   # a config reload swaps the module's one; release the one we took

    async def stream():
        if not semaphore.acquire(blocking=False):
            yield _sse({"type": "error", "message": "The extractor is busy with other runs - try again in a minute"})
            yield _sse({"type": "done", "ok": False, "returncode": -1, "failed": [{"pipeline_id": i, "error": "Not started"} for i in ids]})
            return
        workdir = tempfile.mkdtemp(prefix="ssx_")
        output = os.path.join(workdir, "report.json")
        process = None
        states: Dict[str, Dict[str, Any]] = {i: {"state": "queued", "error": None} for i in ids}
        started = time.monotonic()
        try:
            logger.info("StreamSets extraction by %s: %s", user, ", ".join(names.values()))
            yield _sse({"type": "status", "message": f"Starting extraction of {len(ids)} pipeline(s)",
                        "pipelines": [{"id": i, "name": names[i]} for i in ids]})
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
                   "STREAMSETS_URL": cfg["url"], "STREAMSETS_USERNAME": cfg["username"],
                   "STREAMSETS_PASSWORD": cfg["password"], "STREAMSETS_OUTPUT_DIR": workdir,
                   "STREAMSETS_REQUEST_TIMEOUT": str(cfg["timeout"]),
                   "STREAMSETS_THREAD_POOL_SIZE": str(cfg["thread_pool_size"])}
            # Popen + a reader thread rather than asyncio subprocesses: those don't work on
            # Windows' SelectorEventLoop (uvicorn --reload) - same reason as the IT validation runner.
            process = subprocess.Popen([cfg["python_path"], cfg["script_path"], "--pipeline", *ids, "--output", output],
                                       cwd=os.path.dirname(cfg["script_path"]) or None, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            loop = asyncio.get_running_loop()
            lines: asyncio.Queue = asyncio.Queue()

            def pump():
                try:
                    for raw in iter(process.stdout.readline, b""):
                        loop.call_soon_threadsafe(lines.put_nowait, raw)
                except (ValueError, OSError):
                    pass
                finally:
                    try:
                        loop.call_soon_threadsafe(lines.put_nowait, None)
                    except RuntimeError:
                        pass   # the request is already gone
            threading.Thread(target=pump, daemon=True).start()

            timed_out = False
            while True:
                remaining = cfg["run_timeout"] - (time.monotonic() - started)
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    raw = await asyncio.wait_for(lines.get(), timeout=min(HEARTBEAT_SECONDS, remaining))
                except asyncio.TimeoutError:
                    yield ": ping\n\n"   # keeps proxies from closing a quiet stream
                    continue
                if raw is None:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                yield _sse({"type": "log", "message": line})
                change = _progress(line, by_name, names)
                if change:
                    pid, state, error = change
                    if states[pid]["state"] not in ("done", "failed") or state == "failed":
                        states[pid] = {"state": state, "error": error}
                        yield _sse({"type": "progress", "pipeline_id": pid, "state": state, "error": error})

            if timed_out:
                process.kill()
                yield _sse({"type": "error", "message": f"Extraction stopped after {cfg['run_timeout'] // 60} minutes"})
            returncode = await asyncio.to_thread(process.wait)

            report = None
            if os.path.isfile(output):
                size = os.path.getsize(output)
                if size > MAX_REPORT_BYTES:
                    yield _sse({"type": "error", "message": f"The report is too large to send to the browser ({size // (1024 * 1024)} MB) - extract fewer pipelines at a time"})
                else:
                    try:
                        with open(output, encoding="utf-8") as f:
                            report = json.load(f)
                    except (OSError, ValueError) as e:
                        yield _sse({"type": "error", "message": f"The extractor wrote a report that can't be read: {e}"})

            extracted = {(r.get("metadata") or {}).get("pipeline_id") for r in report or [] if isinstance(r, dict)}
            failed = []
            for pid in ids:
                if pid in extracted:
                    if states[pid]["state"] != "done":
                        yield _sse({"type": "progress", "pipeline_id": pid, "state": "done", "error": None})
                    continue
                error = states[pid]["error"] or ("Timed out" if timed_out else "Not extracted - see the log")
                failed.append({"pipeline_id": pid, "name": names[pid], "error": error})
                if states[pid]["state"] != "failed":
                    yield _sse({"type": "progress", "pipeline_id": pid, "state": "failed", "error": error})

            if report:
                yield _sse({"type": "report", "filename": _report_name([names[i] for i in ids if i in extracted]),
                            "report": report})
            elif returncode != 0 and not timed_out:
                yield _sse({"type": "error", "message": f"The extractor exited with code {returncode} - see the log"})
            logger.info("StreamSets extraction by %s finished: %d ok, %d failed (exit %s)",
                        user, len(extracted & set(ids)), len(failed), returncode)
            yield _sse({"type": "done", "ok": not failed and bool(report), "returncode": returncode, "failed": failed,
                        "seconds": round(time.monotonic() - started, 1)})
        except FileNotFoundError:
            yield _sse({"type": "error", "message": "The Python interpreter or extractor script wasn't found on the server"})
            yield _sse({"type": "done", "ok": False, "returncode": -1, "failed": []})
        except Exception as e:   # noqa: BLE001 - the stream must always end with "done"
            logger.exception("StreamSets extraction crashed")
            yield _sse({"type": "error", "message": f"Extraction failed: {type(e).__name__}: {e}"})
            yield _sse({"type": "done", "ok": False, "returncode": -1, "failed": []})
        finally:
            # Runs on success, on error and when the browser goes away mid-run.
            if process and process.poll() is None:
                process.terminate()
                threading.Thread(target=_reap, args=(process, workdir), daemon=True).start()
                logger.info("StreamSets extraction by %s cancelled", user)
            else:
                shutil.rmtree(workdir, ignore_errors=True)
            semaphore.release()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


def _reap(process: subprocess.Popen, workdir: str):
    """Off the event loop: give a cancelled run 5s to exit, then kill it and clean up its folder."""
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    shutil.rmtree(workdir, ignore_errors=True)


def _progress(line: str, by_name: Dict[str, str], names: Dict[str, str]):
    """Turn the script's per-pipeline lines into (pipeline_id, state, error)."""
    text = line.strip()
    for marker, state in (("🔄 Processing: ", "running"), ("✅ Successfully extracted: ", "done")):
        if text.startswith(marker):
            pid = by_name.get(text[len(marker):].strip())
            return (pid, state, None) if pid else None
    if text.startswith("❌ Failed to extract "):
        rest = text[len("❌ Failed to extract "):]
        for name, pid in by_name.items():
            if rest.startswith(name + ":") or rest == name:
                return pid, "failed", rest[len(name) + 1:].strip() or "Failed"
    if text.startswith("❌ Pipeline not found: "):
        key = text[len("❌ Pipeline not found: "):].strip()
        pid = key if key in names else by_name.get(key)
        return (pid, "failed", "Not found in StreamSets") if pid else None
    return None