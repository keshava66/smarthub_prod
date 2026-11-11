from __future__ import annotations

import csv
import io
import json
import logging
import os
import tempfile
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

import document_preview
from auth import require_permission

logger = logging.getLogger(__name__)

s3_router = APIRouter(prefix="/api/s3", tags=["Data Studio - S3"])

PERM = "runS3Explorer"

# Wired in from app.py via set_s3_dependencies()
_config: Optional[Dict[str, Any]] = None

# ============================================================================
# Config
# ============================================================================

DEFAULT_PAGE_SIZE = 200
MAX_PAGE_SIZE = 1000
PREVIEW_MAX_BYTES = 512 * 1024        # what we pull down for a text/table preview
PREVIEW_MAX_ROWS = 300                # rows shown in the preview grid
DOCUMENT_PREVIEW_MAX_BYTES = 32 * 1024 * 1024   # office files are read whole to parse
INLINE_PREVIEW_MAX_BYTES = 60 * 1024 * 1024     # what we'll stream to the browser to render
SEARCH_SCAN_CAP = 20_000              # keys walked before a recursive search gives up
DOWNLOAD_CHUNK_BYTES = 64 * 1024
UPLOAD_SPOOL_BYTES = 8 * 1024 * 1024     # an upload bigger than this spills to a temp file
MAX_UPLOAD_BYTES = 256 * 1024 * 1024     # refuse anything larger than this
PRESIGN_MAX_EXPIRY = 12 * 3600
SEARCH_MAX_LIMIT = 5_000              # most matches a single search will return

# Rendered by the browser itself - we just stream the bytes back inline.
PDF_EXTENSIONS = {"pdf"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg", "ico", "avif"}
INLINE_EXTENSIONS = PDF_EXTENSIONS | IMAGE_EXTENSIONS

# Content types for the inline endpoint. S3 objects are frequently stored as
# application/octet-stream, and a PDF served under that type downloads instead
# of rendering, so the extension decides what we claim.
INLINE_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "ico": "image/x-icon",
    "avif": "image/avif",
    "svg": "image/svg+xml",
}

TEXT_EXTENSIONS = {
    "txt", "log", "md", "json", "jsonl", "ndjson", "csv", "tsv", "xml", "yaml", "yml",
    "sql", "py", "js", "html", "css", "sh", "conf", "ini", "cfg", "properties", "env",
}

# Plenty of tools upload everything as text/plain or leave the type off, so a
# known-binary extension wins over whatever Content-Type claims.
BINARY_EXTENSIONS = {
    "zip", "gz", "tgz", "bz2", "xz", "7z", "rar", "tar",
    "png", "jpg", "jpeg", "gif", "bmp", "tiff", "webp", "ico", "svgz",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "mp3", "mp4", "mov", "avi", "wav", "zst", "avro", "orc", "pkl", "bin",
    "so", "dll", "exe", "class", "jar", "db", "sqlite",
}


def _clean_cfg(value):
    """Trim whitespace and stray wrapping quotes off a config string - same
    reason as the Snowflake runner: hand-pasted values pick up quotes and a
    quoted bucket name fails in a way that reads like a permissions problem."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in ("\"", "'"):
        text = text[1:-1].strip()
    return text.strip("\"'").strip()


def _cfg_value(key: str, env_var: str, default=None):
    if _config and _config.get(key) not in (None, ""):
        return _clean_cfg(_config[key])
    if os.environ.get(env_var):
        return _clean_cfg(os.environ[env_var])
    return default


def _raw_cfg_value(key: str, env_var: str, default=None):
    """Byte-for-byte lookup - secret keys are allowed to start or end with a quote."""
    if _config and _config.get(key) not in (None, ""):
        return _config[key]
    if os.environ.get(env_var):
        return os.environ[env_var]
    return default


def _cfg_list(key: str, env_var: str) -> List[str]:
    raw = _config.get(key) if _config else None
    if raw is None:
        raw = os.environ.get(env_var)
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [_clean_cfg(v) for v in raw if _clean_cfg(v)]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _cfg_accounts() -> List[Dict[str, Any]]:
    """The "accounts" list out of dq_config.json, or S3X_ACCOUNTS as JSON.

    One entry per credential set. Anything an entry leaves out falls back to the
    top-level settings, except the credentials themselves - an account either
    brings its own or it borrows the default's, never a mix of the two."""
    raw = _config.get("accounts") if _config else None
    if raw is None:
        raw = os.environ.get("S3X_ACCOUNTS")
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("s3_explorer: S3X_ACCOUNTS is not valid JSON - ignoring it")
            return []
    if isinstance(raw, dict):                       # {"dev": {...}, "prod": {...}}
        raw = [{**cfg, "name": name} for name, cfg in raw.items()]
    if not isinstance(raw, list):
        logger.warning("s3_explorer: 'accounts' should be a list - ignoring it")
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def _safe_int(value, default: int, minimum: int = 0) -> int:
    """Config numbers get typed by hand - a stray "512kb" shouldn't 500 every
    request, it should fall back to the default."""
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        logger.warning("s3_explorer: %r is not a number, using %s", value, default)
        return default
    return max(minimum, parsed)


def _truthy(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "0", "no", "off", "")


def _configured() -> bool:
    """The explorer can run off an instance role, so a region alone is enough
    to consider it switched on - we don't insist on static credentials."""
    if _config:
        return True
    return bool(
        os.environ.get("S3X_REGION")
        or os.environ.get("S3X_ACCESS_KEY_ID")
        or os.environ.get("S3X_PROFILE")
        or os.environ.get("AWS_REGION")
    )


def _settings() -> Dict[str, Any]:
    if not _configured():
        raise HTTPException(
            status_code=503,
            detail="S3 Explorer is not configured - add an 's3_explorer' section to dq_config.json",
        )
    return {
        "region": _cfg_value("region", "S3X_REGION") or os.environ.get("AWS_REGION"),
        "access_key_id": _cfg_value("access_key_id", "S3X_ACCESS_KEY_ID"),
        "secret_access_key": _raw_cfg_value("secret_access_key", "S3X_SECRET_ACCESS_KEY"),
        "session_token": _raw_cfg_value("session_token", "S3X_SESSION_TOKEN"),
        "profile": _cfg_value("profile", "S3X_PROFILE"),
        "endpoint_url": _cfg_value("endpoint_url", "S3X_ENDPOINT_URL"),
        # Empty list = every bucket the credentials can see.
        "buckets": _cfg_list("buckets", "S3X_BUCKETS"),
        # Extra credential sets, each with its own buckets - see _connections().
        "accounts": _cfg_accounts(),
        # Off by default: uploads and deletes need to be opted into per environment.
        "allow_writes": _truthy(_cfg_value("allow_writes", "S3X_ALLOW_WRITES"), False),
        "allow_presign": _truthy(_cfg_value("allow_presign", "S3X_ALLOW_PRESIGN"), True),
        "presign_expiry_seconds": _safe_int(_cfg_value("presign_expiry_seconds", "S3X_PRESIGN_EXPIRY", 900), 900, 60),
        "preview_max_bytes": _safe_int(_cfg_value("preview_max_bytes", "S3X_PREVIEW_MAX_BYTES", PREVIEW_MAX_BYTES), PREVIEW_MAX_BYTES, 1024),
        "document_preview_max_bytes": _safe_int(
            _cfg_value("document_preview_max_bytes", "S3X_DOCUMENT_PREVIEW_MAX_BYTES", DOCUMENT_PREVIEW_MAX_BYTES),
            DOCUMENT_PREVIEW_MAX_BYTES, 1024),
        "inline_preview_max_bytes": _safe_int(
            _cfg_value("inline_preview_max_bytes", "S3X_INLINE_PREVIEW_MAX_BYTES", INLINE_PREVIEW_MAX_BYTES),
            INLINE_PREVIEW_MAX_BYTES, 1024),
        "max_upload_bytes": _safe_int(_cfg_value("max_upload_bytes", "S3X_MAX_UPLOAD_BYTES", MAX_UPLOAD_BYTES), MAX_UPLOAD_BYTES, 1024),
        "search_scan_cap": _safe_int(_cfg_value("search_scan_cap", "S3X_SEARCH_SCAN_CAP", SEARCH_SCAN_CAP), SEARCH_SCAN_CAP, 100),
    }


def set_s3_dependencies(config: Optional[Dict[str, Any]]):
    """Called once from app.py with the 's3_explorer' section of dq_config.json."""
    global _config
    _config = config
    # Drop any clients built from the previous settings - on a reload the new
    # regions/credentials have to take effect.
    with _client_lock:
        _clients.clear()
    if not _configured():
        logger.warning("⚠️ s3_explorer config not found - S3 Explorer endpoints will be unavailable")
    else:
        names = [c["name"] for c in _connections()]
        logger.info("S3 Explorer configured (region=%s, accounts=%s)",
                    _settings().get("region") or "default", ", ".join(names))


# ============================================================================
# Connections
#
# One deployment can hold buckets in more than one account, each with its own
# credentials. Every credential set is a "connection": the top-level settings
# are the connection named "default", and each entry under "accounts" adds
# another. A bucket is routed to a connection by that connection's bucket
# allow-list, so nothing else in the module has to know which keys were used.
# ============================================================================

DEFAULT_CONNECTION = "default"


def _base_connection(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": DEFAULT_CONNECTION,
        "region": cfg["region"],
        "access_key_id": cfg["access_key_id"],
        "secret_access_key": cfg["secret_access_key"],
        "session_token": cfg["session_token"],
        "profile": cfg["profile"],
        "endpoint_url": cfg["endpoint_url"],
        "buckets": cfg["buckets"],
        "allow_writes": cfg["allow_writes"],
        "allow_presign": cfg["allow_presign"],
        "presign_expiry_seconds": cfg["presign_expiry_seconds"],
    }


def _named_connection(raw: Dict[str, Any], index: int, base: Dict[str, Any]) -> Dict[str, Any]:
    def value(key, default=None):
        got = raw.get(key)
        return default if got in (None, "") else _clean_cfg(got)

    def secret(key, default=None):
        got = raw.get(key)
        return default if got in (None, "") else got

    def flag(key, default):
        return default if raw.get(key) is None else _truthy(raw.get(key), default)

    buckets = raw.get("buckets") or raw.get("bucket") or []
    if isinstance(buckets, str):
        buckets = [b.strip() for b in buckets.split(",") if b.strip()]
    buckets = [_clean_cfg(b) for b in buckets if _clean_cfg(b)]

    # Credentials are taken as a set: an account that names any of its own is
    # used exactly as written. Mixing one account's key with another's secret is
    # the kind of mistake that reads as a permissions problem for a whole day.
    own_credentials = bool(raw.get("access_key_id") or raw.get("profile"))

    return {
        "name": value("name") or value("label") or f"account{index}",
        "region": value("region", base["region"]),
        "access_key_id": value("access_key_id") if own_credentials else base["access_key_id"],
        "secret_access_key": secret("secret_access_key") if own_credentials else base["secret_access_key"],
        "session_token": secret("session_token") if own_credentials else base["session_token"],
        "profile": value("profile") if own_credentials else base["profile"],
        "endpoint_url": value("endpoint_url", base["endpoint_url"]),
        "buckets": buckets,
        "allow_writes": flag("allow_writes", base["allow_writes"]),
        "allow_presign": flag("allow_presign", base["allow_presign"]),
        "presign_expiry_seconds": _safe_int(
            raw.get("presign_expiry_seconds") or base["presign_expiry_seconds"],
            base["presign_expiry_seconds"], 60),
    }


def _connections() -> List[Dict[str, Any]]:
    cfg = _settings()
    base = _base_connection(cfg)
    accounts = cfg["accounts"]

    out: List[Dict[str, Any]] = []
    # With accounts configured, the top-level settings only count as a
    # connection of their own if they actually carry credentials - otherwise
    # "default" would quietly become an instance-role catch-all listing
    # everything the host can see. They still supply the defaults every account
    # falls back to (region, endpoint, the caps, the write flags).
    if not accounts or base["access_key_id"] or base["profile"]:
        out.append(base)
    elif base["buckets"]:
        logger.warning(
            "s3_explorer: the top-level 'buckets' list is ignored - it has no credentials of "
            "its own and 'accounts' is configured. Move those buckets into the account that owns them."
        )

    seen = {c["name"] for c in out}
    for index, raw in enumerate(accounts, start=1):
        conn = _named_connection(raw, index, base)
        while conn["name"] in seen:                     # names are the API's handle
            conn["name"] = f"{conn['name']}-{index}"
        seen.add(conn["name"])
        out.append(conn)

    if not out:
        raise HTTPException(status_code=503, detail="S3 Explorer has no usable credentials configured")
    return out


def _connection_for(bucket: str, account: Optional[str] = None) -> Dict[str, Any]:
    """Which credentials this bucket is read with.

    A named account wins outright; otherwise the bucket is matched against each
    connection's allow-list, and a connection with no allow-list is the fallback
    for anything unclaimed."""
    conns = _connections()

    if account:
        match = next((c for c in conns if c["name"] == account), None)
        if match is None:
            raise HTTPException(status_code=404, detail=f"No S3 account named '{account}' is configured")
        if match["buckets"] and bucket not in match["buckets"]:
            raise HTTPException(
                status_code=403,
                detail=f"Bucket '{bucket}' is not in the allow-list for account '{account}'",
            )
        return match

    for conn in conns:
        if bucket in conn["buckets"]:
            return conn
    for conn in conns:
        if not conn["buckets"]:
            return conn

    raise HTTPException(status_code=403, detail=f"Bucket '{bucket}' is not in the configured allow-list")


# ============================================================================
# Client
# ============================================================================

_clients: Dict[str, Any] = {}
_client_lock = threading.Lock()


def _build_client(conn: Dict[str, Any]):
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError as exc:  # pragma: no cover - depends on the deployment image
        raise HTTPException(
            status_code=503,
            detail="boto3 is not installed on the API host - add it to backend/requirements.txt",
        ) from exc

    session_kwargs: Dict[str, Any] = {}
    if conn["profile"]:
        session_kwargs["profile_name"] = conn["profile"]
    if conn["region"]:
        session_kwargs["region_name"] = conn["region"]
    if conn["access_key_id"] and conn["secret_access_key"]:
        session_kwargs["aws_access_key_id"] = conn["access_key_id"]
        session_kwargs["aws_secret_access_key"] = conn["secret_access_key"]
        if conn["session_token"]:
            session_kwargs["aws_session_token"] = conn["session_token"]

    session = boto3.session.Session(**session_kwargs)
    return session.client(
        "s3",
        endpoint_url=conn["endpoint_url"] or None,
        config=BotoConfig(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=60,
            signature_version="s3v4",
        ),
    )


def _s3(conn: Optional[Dict[str, Any]] = None):
    """One client per connection, built once. boto3 clients are thread-safe
    after construction, so the lock only guards the first build."""
    if conn is None:
        conn = _connections()[0]
    name = conn["name"]
    client = _clients.get(name)
    if client is None:
        with _client_lock:
            client = _clients.get(name)
            if client is None:
                client = _build_client(conn)
                _clients[name] = client
    return client


def _client_error(exc) -> HTTPException:
    """Turn a botocore ClientError into something the UI can show verbatim."""
    response = getattr(exc, "response", None) or {}
    error = response.get("Error") or {}
    code = error.get("Code", "")
    message = error.get("Message") or str(exc)
    status = int((response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 500)
    if code in ("AccessDenied", "AllAccessDisabled"):
        status = 403
    elif code in ("NoSuchBucket", "NoSuchKey", "404"):
        status = 404
    return HTTPException(status_code=status, detail=f"{code or 'S3 error'}: {message}")


def _call(fn, *args, **kwargs):
    """Every boto call goes through here so ClientErrors never surface as a 500."""
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(status_code=503, detail="boto3 is not installed on the API host") from exc
    try:
        return fn(*args, **kwargs)
    except ClientError as exc:
        raise _client_error(exc)
    except BotoCoreError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach S3: {exc}")


# ============================================================================
# Helpers
# ============================================================================

def _check_bucket(bucket: str) -> str:
    bucket = (bucket or "").strip()
    if not bucket:
        raise HTTPException(status_code=400, detail="A bucket is required")
    return bucket


def _target(bucket: str, account: Optional[str] = None):
    """Every bucket-scoped endpoint starts here: the checked bucket name plus
    the connection whose credentials reach it."""
    bucket = _check_bucket(bucket)
    return bucket, _connection_for(bucket, account)


def _check_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="An object key is required")
    if key.startswith("/"):
        key = key.lstrip("/")
    return key


def _require_writes(conn: Dict[str, Any]):
    if not conn["allow_writes"]:
        raise HTTPException(
            status_code=403,
            detail=f"The S3 Explorer is read-only for account '{conn['name']}'",
        )


def _iso(value) -> Optional[str]:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _extension(key: str) -> str:
    name = key.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _basename(key: str) -> str:
    return key.rstrip("/").rsplit("/", 1)[-1]


def _content_disposition(name: str, mode: str = "attachment") -> str:
    """Headers are latin-1 only, and a quote in the name ends the filename
    early. Send an ASCII-safe fallback plus the RFC 5987 form that carries the
    real (possibly non-ASCII) name."""
    from urllib.parse import quote

    ascii_name = name.encode("ascii", "ignore").decode("ascii").replace('"', "").strip()
    ascii_name = ascii_name or "download"
    return f"{mode}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


# ============================================================================
# Endpoints
# ============================================================================

@s3_router.get("/health")
def health(auth: dict = Depends(require_permission(PERM))):
    cfg = _settings()
    conns = _connections()
    return {
        "configured": True,
        "region": cfg["region"],
        "endpoint_url": cfg["endpoint_url"],
        # Kept for callers that predate multiple accounts: true when any
        # connection allows it. Per-account flags are in "accounts" below.
        "allow_writes": any(c["allow_writes"] for c in conns),
        "allow_presign": any(c["allow_presign"] for c in conns),
        "bucket_allow_list": cfg["buckets"],
        "accounts": [
            {
                "name": c["name"],
                "region": c["region"],
                "endpoint_url": c["endpoint_url"],
                "buckets": c["buckets"],
                "allow_writes": c["allow_writes"],
                "allow_presign": c["allow_presign"],
            }
            for c in conns
        ],
        "max_page_size": MAX_PAGE_SIZE,
        "preview_max_bytes": cfg["preview_max_bytes"],
        "document_preview_max_bytes": cfg["document_preview_max_bytes"],
        "inline_preview_max_bytes": cfg["inline_preview_max_bytes"],
        "max_upload_bytes": cfg["max_upload_bytes"],
        "search_scan_cap": cfg["search_scan_cap"],
        "search_max_limit": SEARCH_MAX_LIMIT,
    }


@s3_router.get("/buckets")
def list_buckets(auth: dict = Depends(require_permission(PERM))):
    """Every bucket the explorer can reach, across every configured account.

    A connection with an allow-list is reported from config alone - plenty of
    service accounts can read a named bucket without being allowed to run
    ListAllMyBuckets - and only a connection without one is enumerated."""
    conns = _connections()
    rows: List[Dict[str, Any]] = []
    filtered = True
    errors: List[Dict[str, str]] = []

    for conn in conns:
        common = {
            "account": conn["name"],
            "region": conn["region"],
            "allow_writes": conn["allow_writes"],
            "allow_presign": conn["allow_presign"],
        }
        if conn["buckets"]:
            rows.extend({**common, "name": b, "created_at": None} for b in conn["buckets"])
            continue

        filtered = False
        # One account being unable to enumerate must not empty the whole
        # sidebar - report it next to the buckets that did come back.
        try:
            data = _call(_s3(conn).list_buckets)
        except HTTPException as exc:
            logger.warning("s3_explorer: listing buckets for '%s' failed: %s", conn["name"], exc.detail)
            errors.append({"account": conn["name"], "message": str(exc.detail)})
            continue
        rows.extend(
            {**common, "name": b["Name"], "created_at": _iso(b.get("CreationDate"))}
            for b in data.get("Buckets", [])
        )

    # Same bucket reachable through two accounts stays as two rows - they can
    # hold different objects, and the account is part of the address.
    rows.sort(key=lambda b: (b["name"], b["account"]))
    return {
        "buckets": rows,
        "filtered": filtered,
        "multi_account": len(conns) > 1,
        "errors": errors,
    }


@s3_router.get("/objects")
def list_objects(
    bucket: str = Query(...),
    prefix: str = Query(""),
    token: Optional[str] = Query(None, description="ContinuationToken from a previous page"),
    page_size: int = Query(DEFAULT_PAGE_SIZE),
    account: Optional[str] = Query(None, description="Which configured S3 account the bucket belongs to"),
    auth: dict = Depends(require_permission(PERM)),
):
    """One folder level: common prefixes first, then the objects sitting in it."""
    bucket, conn = _target(bucket, account)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    prefix = (prefix or "").lstrip("/")

    params: Dict[str, Any] = {
        "Bucket": bucket,
        "Prefix": prefix,
        "Delimiter": "/",
        "MaxKeys": page_size,
    }
    if token:
        params["ContinuationToken"] = token

    data = _call(_s3(conn).list_objects_v2, **params)

    folders = [
        {"name": _basename(cp["Prefix"]), "prefix": cp["Prefix"]}
        for cp in data.get("CommonPrefixes", [])
    ]
    files = []
    for obj in data.get("Contents", []):
        key = obj["Key"]
        # The folder marker itself (a zero-byte key ending in "/") isn't a file.
        if key == prefix or key.endswith("/"):
            continue
        files.append({
            "key": key,
            "name": _basename(key),
            "size": obj.get("Size"),
            "last_modified": _iso(obj.get("LastModified")),
            "storage_class": obj.get("StorageClass"),
            "etag": (obj.get("ETag") or "").strip('"'),
            "extension": _extension(key),
        })

    return {
        "bucket": bucket,
        "prefix": prefix,
        "folders": folders,
        "files": files,
        "next_token": data.get("NextContinuationToken"),
        "truncated": bool(data.get("IsTruncated")),
    }


def _clamp_scan_cap(requested: Optional[int], cfg: Dict[str, Any]) -> int:
    """The caller picks how far a search should go, but never past the cap this
    environment was configured with - that one exists to protect the S3 bill."""
    ceiling = cfg["search_scan_cap"]
    if requested is None:
        return ceiling
    return max(100, min(int(requested), ceiling))


def _match_row(obj: Dict[str, Any]) -> Dict[str, Any]:
    key = obj["Key"]
    return {
        "key": key,
        "name": _basename(key),
        "size": obj.get("Size"),
        "last_modified": _iso(obj.get("LastModified")),
        "storage_class": obj.get("StorageClass"),
        "extension": _extension(key),
    }


def _scan(conn: Dict[str, Any], bucket: str, prefix: str, needle: str, limit: int, scan_cap: int):
    """Walk every key under the prefix, yielding one result per S3 page.

    S3 has no server-side search, so a search is a full listing with the
    filtering done here. Yields (page_no, matches_on_this_page, scanned_total,
    matched_total, last_key, stopped) so both the plain and the streaming
    endpoint can share exactly the same walk and the same stop rules."""
    paginator = _s3(conn).get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

    scanned = 0
    matched = 0
    page_no = 0

    for page in pages:
        page_no += 1
        batch: List[Dict[str, Any]] = []
        last_key = None
        stopped = None

        for obj in page.get("Contents", []):
            scanned += 1
            key = obj["Key"]
            last_key = key
            if key.endswith("/"):
                continue
            if needle in key.lower():
                matched += 1
                batch.append(_match_row(obj))
                if matched >= limit:
                    stopped = "limit"
                    break

        if not stopped and scanned >= scan_cap:
            stopped = "cap"

        yield page_no, batch, scanned, matched, last_key, stopped

        if stopped:
            return


@s3_router.get("/search")
def search_objects(
    bucket: str = Query(...),
    prefix: str = Query(""),
    q: str = Query(..., min_length=1),
    limit: int = Query(200),
    scan_cap: Optional[int] = Query(None, description="Stop after this many keys; capped by the server setting"),
    account: Optional[str] = Query(None, description="Which configured S3 account the bucket belongs to"),
    auth: dict = Depends(require_permission(PERM)),
):
    """Substring match on the key, walking everything under the prefix.

    The whole scan runs before anything comes back - use /search/stream for a
    long one, which reports as it goes."""
    bucket, conn = _target(bucket, account)
    cfg = _settings()
    limit = max(1, min(limit, SEARCH_MAX_LIMIT))
    scan_cap = _clamp_scan_cap(scan_cap, cfg)
    needle = q.strip().lower()
    prefix = (prefix or "").lstrip("/")

    matches: List[Dict[str, Any]] = []
    scanned = 0
    stopped = None
    try:
        for _page, batch, scanned, _matched, _last, stopped in _scan(
            conn, bucket, prefix, needle, limit, scan_cap
        ):
            matches.extend(batch)
    except Exception as exc:  # botocore raises lazily, while the pages iterate
        if hasattr(exc, "response"):
            raise _client_error(exc)
        raise HTTPException(status_code=502, detail=str(exc))

    return {
        "bucket": bucket, "prefix": prefix, "query": q, "files": matches,
        "scanned": scanned, "partial": stopped is not None, "stopped_because": stopped,
    }


def _sse(event: str, payload: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


@s3_router.get("/search/stream")
def search_objects_stream(
    bucket: str = Query(...),
    prefix: str = Query(""),
    q: str = Query(..., min_length=1),
    limit: int = Query(500),
    scan_cap: Optional[int] = Query(None, description="Stop after this many keys; capped by the server setting"),
    account: Optional[str] = Query(None, description="Which configured S3 account the bucket belongs to"),
    auth: dict = Depends(require_permission(PERM)),
):
    """The same scan as /search, reported as it happens.

    A recursive listing of a big prefix takes a while and gives no feedback of
    its own, so this streams what it is doing - one progress event per S3 page
    plus matches as they are found - and the UI shows it as a running log
    instead of a spinner that might be stuck."""
    bucket, conn = _target(bucket, account)
    cfg = _settings()
    limit = max(1, min(limit, SEARCH_MAX_LIMIT))
    needle = q.strip().lower()
    root = (prefix or "").lstrip("/")
    scan_cap = _clamp_scan_cap(scan_cap, cfg)

    def events():
        started = time.time()
        scanned = matched = pages = 0
        stopped = None
        heartbeat = started

        yield _sse("start", {
            "bucket": bucket, "prefix": root, "query": q,
            "limit": limit, "scan_cap": scan_cap,
        })

        try:
            for pages, batch, scanned, matched, last_key, stopped in _scan(
                conn, bucket, root, needle, limit, scan_cap
            ):
                if batch:
                    yield _sse("matches", {"files": batch})

                now = time.time()
                # A page with nothing in it is still worth reporting, but only
                # every quarter second - a bucket of tiny pages would otherwise
                # spend more time writing progress than listing.
                if batch or stopped or pages == 1 or now - heartbeat >= 0.25:
                    heartbeat = now
                    yield _sse("progress", {
                        "page": pages, "scanned": scanned, "matched": matched,
                        "last_key": last_key,
                        "elapsed_ms": int((now - started) * 1000),
                    })

            yield _sse("done", {
                "scanned": scanned, "matched": matched, "pages": pages,
                "partial": stopped is not None, "stopped_because": stopped,
                "elapsed_ms": int((time.time() - started) * 1000),
            })
        except GeneratorExit:
            # The browser hung up (Stop, tab closed) - stop scanning, quietly.
            logger.info("S3 search stream closed by the client after %d keys", scanned)
            raise
        except Exception as exc:  # botocore raises while the pages are iterated
            detail = _client_error(exc).detail if hasattr(exc, "response") else str(exc)
            logger.warning("S3 search stream failed for %s/%s: %s", bucket, root, detail)
            yield _sse("failed", {
                "message": str(detail), "scanned": scanned, "matched": matched,
                "elapsed_ms": int((time.time() - started) * 1000),
            })

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx buffers event streams into uselessness without this.
            "X-Accel-Buffering": "no",
        },
    )


@s3_router.get("/object/meta")
def object_meta(
    bucket: str = Query(...),
    key: str = Query(...),
    account: Optional[str] = Query(None, description="Which configured S3 account the bucket belongs to"),
    auth: dict = Depends(require_permission(PERM)),
):
    (bucket, conn), key = _target(bucket, account), _check_key(key)
    head = _call(_s3(conn).head_object, Bucket=bucket, Key=key)
    return {
        "bucket": bucket,
        "account": conn["name"],
        "key": key,
        "name": _basename(key),
        "size": head.get("ContentLength"),
        "content_type": head.get("ContentType"),
        "last_modified": _iso(head.get("LastModified")),
        "etag": (head.get("ETag") or "").strip('"'),
        "storage_class": head.get("StorageClass") or "STANDARD",
        "server_side_encryption": head.get("ServerSideEncryption"),
        "version_id": head.get("VersionId"),
        "metadata": head.get("Metadata") or {},
    }


def _preview_table_from_delimited(text: str, delimiter: str) -> Dict[str, Any]:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = []
    for i, row in enumerate(reader):
        if i > PREVIEW_MAX_ROWS:
            break
        rows.append(row)
    if not rows:
        return {"mode": "text", "text": text}
    header = rows[0]
    body = rows[1:]
    width = max(len(header), *(len(r) for r in body)) if body else len(header)
    columns = [header[i] if i < len(header) else f"col_{i + 1}" for i in range(width)]
    return {
        "mode": "table",
        "columns": columns,
        "rows": [[r[i] if i < len(r) else None for i in range(width)] for r in body],
    }


def _preview_table_from_records(records: List[Any]) -> Dict[str, Any]:
    columns: List[str] = []
    for record in records:
        if isinstance(record, dict):
            for col in record:
                if col not in columns:
                    columns.append(col)
    if not columns:
        return {"mode": "text", "text": json.dumps(records[:PREVIEW_MAX_ROWS], indent=2, default=str)}
    rows = []
    for record in records[:PREVIEW_MAX_ROWS]:
        if not isinstance(record, dict):
            continue
        rows.append([
            record.get(c) if isinstance(record.get(c), (str, int, float, bool, type(None)))
            else json.dumps(record.get(c), default=str)
            for c in columns
        ])
    return {"mode": "table", "columns": columns, "rows": rows}


def _inline_payload(base: Dict[str, Any], size: int, limit: int, kind: Optional[str] = None) -> Dict[str, Any]:
    """A PDF or an image is handed straight to the browser, which renders it
    far better than anything we could reconstruct server-side. The pane fetches
    `raw_url` itself so the call still carries the user's bearer token."""
    if kind is None:
        kind = "image" if (base.get("extension") or "") in IMAGE_EXTENSIONS else "pdf"
    if size > limit:
        return {**base, "mode": "unsupported", "truncated": False,
                "message": f"This file is {size:,} bytes, past the {limit:,}-byte inline preview limit. "
                           "Download it to open it locally."}
    return {
        **base,
        "mode": kind,
        "truncated": False,
        "raw_url": "/api/s3/object/raw?" + urlencode({
            "bucket": base["bucket"], "key": base["key"], "account": base.get("account") or "",
        }),
    }


@s3_router.get("/object/preview")
def object_preview(
    bucket: str = Query(...),
    key: str = Query(...),
    account: Optional[str] = Query(None, description="Which configured S3 account the bucket belongs to"),
    auth: dict = Depends(require_permission(PERM)),
):
    """First slice of an object, parsed into something the pane can render.

    Text and delimited files are read with a Range request, so opening a 4 GB
    log costs the same as opening a 4 KB one. Three kinds of file have to be
    read whole instead: parquet, office documents (docx/doc/xlsx/xls/pptx), and
    anything the browser renders itself (PDFs and images) - those are capped by
    size rather than sliced, because half a zip container parses as nothing."""
    (bucket, conn), key = _target(bucket, account), _check_key(key)
    cfg = _settings()
    limit = cfg["preview_max_bytes"]
    ext = _extension(key)

    head = _call(_s3(conn).head_object, Bucket=bucket, Key=key)
    size = head.get("ContentLength") or 0
    content_type = head.get("ContentType") or ""

    base = {
        "bucket": bucket, "account": conn["name"], "key": key, "size": size,
        "content_type": content_type, "extension": ext, "truncated": size > limit,
    }

    if size == 0:
        return {**base, "mode": "empty", "truncated": False}

    if ext == "parquet":
        if size > limit:
            return {**base, "mode": "unsupported",
                    "message": f"Parquet has to be read whole and this file is {size:,} bytes. Download it instead."}
        body = _call(_s3(conn).get_object, Bucket=bucket, Key=key)["Body"].read()
        try:
            import pandas as pd
            frame = pd.read_parquet(io.BytesIO(body))
        except ImportError:
            return {**base, "mode": "unsupported", "message": "pandas/pyarrow are not installed on the API host"}
        except Exception as exc:
            return {**base, "mode": "unsupported", "message": f"Could not read the parquet file: {exc}"}
        head_frame = frame.head(PREVIEW_MAX_ROWS)
        return {
            **base,
            "mode": "table",
            "columns": [str(c) for c in head_frame.columns],
            "rows": head_frame.astype(object).where(head_frame.notna(), None).values.tolist(),
            "truncated": len(frame) > PREVIEW_MAX_ROWS,
        }

    if ext in INLINE_EXTENSIONS:
        return _inline_payload(base, size, cfg["inline_preview_max_bytes"])

    if ext in document_preview.SUPPORTED_EXTENSIONS:
        doc_limit = cfg["document_preview_max_bytes"]
        if size > doc_limit:
            return {**base, "mode": "unsupported", "truncated": False,
                    "message": f"This document is {size:,} bytes, past the {doc_limit:,}-byte preview limit. "
                               "Download it to open it locally."}
        data = _call(_s3(conn).get_object, Bucket=bucket, Key=key)["Body"].read()
        parsed = document_preview.preview(data, ext)
        # A file named .doc that is really a PDF - happens constantly with
        # regulatory exports, so render it rather than calling it unsupported.
        if parsed.get("mode") == "inline":
            return _inline_payload(base, size, cfg["inline_preview_max_bytes"],
                                   kind=parsed.get("inline_kind", "pdf"))
        return {**base, **parsed, "truncated": bool(parsed.get("truncated"))}

    is_texty = ext in TEXT_EXTENSIONS or (
        ext not in BINARY_EXTENSIONS and (
            content_type.startswith("text/")
            or content_type in ("application/json", "application/xml", "application/x-ndjson")
        )
    )
    if not is_texty:
        return {**base, "mode": "binary",
                "message": "No preview for this file type — download it to open it locally."}

    obj = _call(_s3(conn).get_object, Bucket=bucket, Key=key, Range=f"bytes=0-{max(limit - 1, 0)}")
    raw = obj["Body"].read()

    # Last line of defence: a NUL byte in the first slice means this is not
    # text, whatever the extension and Content-Type said. Without this a
    # mislabelled binary renders as a screenful of replacement characters.
    if b"\x00" in raw[:8192]:
        return {**base, "mode": "binary",
                "message": "This looks like a binary file — download it to open it locally."}

    text = raw.decode("utf-8", errors="replace")

    # A ranged read almost always slices the last line in half - drop it so the
    # preview never shows a half-parsed row.
    if size > limit and "\n" in text:
        text = text[: text.rindex("\n")]

    if ext in ("csv", "tsv"):
        return {**base, **_preview_table_from_delimited(text, "\t" if ext == "tsv" else ",")}

    if ext in ("jsonl", "ndjson"):
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                break
        if records:
            return {**base, **_preview_table_from_records(records)}

    if ext == "json":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {**base, "mode": "text", "text": text}
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return {**base, **_preview_table_from_records(parsed)}
        return {**base, "mode": "text", "text": json.dumps(parsed, indent=2, default=str)[:limit]}

    return {**base, "mode": "text", "text": text}


@s3_router.get("/object/download")
def download_object(
    bucket: str = Query(...),
    key: str = Query(...),
    account: Optional[str] = Query(None, description="Which configured S3 account the bucket belongs to"),
    auth: dict = Depends(require_permission(PERM)),
):
    """Streamed through the API so the browser never needs AWS credentials."""
    (bucket, conn), key = _target(bucket, account), _check_key(key)
    obj = _call(_s3(conn).get_object, Bucket=bucket, Key=key)
    body = obj["Body"]

    def chunks():
        try:
            for chunk in body.iter_chunks(chunk_size=DOWNLOAD_CHUNK_BYTES):
                yield chunk
        finally:
            body.close()

    headers = {"Content-Disposition": _content_disposition(_basename(key))}
    if obj.get("ContentLength") is not None:
        headers["Content-Length"] = str(obj["ContentLength"])
    return StreamingResponse(
        chunks(),
        media_type=obj.get("ContentType") or "application/octet-stream",
        headers=headers,
    )


@s3_router.get("/object/raw")
def raw_object(
    bucket: str = Query(...),
    key: str = Query(...),
    account: Optional[str] = Query(None, description="Which configured S3 account the bucket belongs to"),
    auth: dict = Depends(require_permission(PERM)),
):
    """The same bytes as /object/download, but typed and marked inline so the
    browser renders them in the preview pane instead of saving them.

    Only the formats the pane actually embeds are served this way - everything
    else goes through /object/download, which is explicit about being a file."""
    (bucket, conn), key = _target(bucket, account), _check_key(key)
    ext = _extension(key)
    cfg = _settings()

    head = _call(_s3(conn).head_object, Bucket=bucket, Key=key)
    size = head.get("ContentLength") or 0
    content_type = INLINE_CONTENT_TYPES.get(ext)

    if content_type is None:
        # An extension we don't embed can still be a PDF underneath (see the
        # preview endpoint) - check the magic number before refusing.
        sample = _call(_s3(conn).get_object, Bucket=bucket, Key=key, Range="bytes=0-7")["Body"].read()
        if document_preview.sniff(sample) != "pdf":
            raise HTTPException(status_code=415, detail="This file type is not served inline - use the download endpoint")
        content_type = "application/pdf"

    if size > cfg["inline_preview_max_bytes"]:
        raise HTTPException(status_code=413, detail="File is too large to preview inline - download it instead")

    obj = _call(_s3(conn).get_object, Bucket=bucket, Key=key)
    body = obj["Body"]

    def chunks():
        try:
            for chunk in body.iter_chunks(chunk_size=DOWNLOAD_CHUNK_BYTES):
                yield chunk
        finally:
            body.close()

    headers = {
        "Content-Disposition": _content_disposition(_basename(key), "inline"),
        # A landing bucket holds files from anywhere; SVGs in particular can
        # carry script, so nothing served here is allowed to run or frame out.
        "Content-Security-Policy": "default-src 'none'; img-src 'self' data: blob:; object-src 'none'; script-src 'none'",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=60",
    }
    if obj.get("ContentLength") is not None:
        headers["Content-Length"] = str(obj["ContentLength"])
    return StreamingResponse(chunks(), media_type=content_type, headers=headers)


@s3_router.post("/object/presign")
def presign_object(
    payload: Dict[str, Any] = Body(...),
    auth: dict = Depends(require_permission(PERM)),
):
    """A short-lived direct link, for when a file is too big to proxy."""
    bucket, conn = _target(payload.get("bucket"), payload.get("account"))
    if not conn["allow_presign"]:
        raise HTTPException(
            status_code=403,
            detail=f"Pre-signed links are disabled for account '{conn['name']}'",
        )

    key = _check_key(payload.get("key"))
    expires = int(payload.get("expires_in") or conn["presign_expiry_seconds"])
    expires = max(60, min(expires, PRESIGN_MAX_EXPIRY))

    url = _call(
        _s3(conn).generate_presigned_url,
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires,
    )
    return {"url": url, "expires_in": expires, "expires_at": time.time() + expires}


@s3_router.post("/object/upload")
async def upload_object(
    request: Request,
    bucket: str = Query(...),
    prefix: str = Query(""),
    filename: str = Query(...),
    content_type: Optional[str] = Query(None),
    account: Optional[str] = Query(None, description="Which configured S3 account the bucket belongs to"),
    auth: dict = Depends(require_permission(PERM)),
):
    """The file arrives as the raw request body, not as multipart form data.

    A single file per call doesn't need multipart framing, and this way the API
    doesn't pull in python-multipart. The body spools to disk past 8 MB so a
    large upload doesn't sit in memory."""
    bucket, conn = _target(bucket, account)
    _require_writes(conn)
    folder = (prefix or "").lstrip("/")
    if folder and not folder.endswith("/"):
        folder += "/"
    name = _basename(filename or "upload.bin")
    if not name:
        raise HTTPException(status_code=400, detail="A filename is required")
    key = f"{folder}{name}"

    max_bytes = _settings()["max_upload_bytes"]
    spool = tempfile.SpooledTemporaryFile(max_size=UPLOAD_SPOOL_BYTES)
    try:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            # Checked as it arrives, not from Content-Length, so a lying or
            # missing header can't fill the API host's disk.
            if size > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"That file is larger than the {max_bytes // (1024 * 1024)} MB upload limit",
                )
            spool.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="The request body was empty")
        spool.seek(0)
        _call(
            _s3(conn).upload_fileobj,
            spool,
            bucket,
            key,
            ExtraArgs={"ContentType": content_type} if content_type else None,
        )
    finally:
        spool.close()

    logger.info("S3 Explorer: %s uploaded s3://%s/%s", (auth.get("user") or {}).get("email"), bucket, key)
    return {"bucket": bucket, "account": conn["name"], "key": key, "size": size, "uploaded": True}


@s3_router.post("/object/folder")
def create_folder(
    payload: Dict[str, Any] = Body(...),
    auth: dict = Depends(require_permission(PERM)),
):
    """S3 has no folders - this writes the zero-byte marker key the console uses."""
    bucket, conn = _target(payload.get("bucket"), payload.get("account"))
    _require_writes(conn)
    parent = (payload.get("prefix") or "").lstrip("/")
    name = (payload.get("name") or "").strip().strip("/")
    if not name:
        raise HTTPException(status_code=400, detail="A folder name is required")
    if "/" in name:
        raise HTTPException(status_code=400, detail="Folder names can't contain '/'")
    if parent and not parent.endswith("/"):
        parent += "/"

    key = f"{parent}{name}/"
    _call(_s3(conn).put_object, Bucket=bucket, Key=key, Body=b"")
    return {"bucket": bucket, "account": conn["name"], "prefix": key, "created": True}


@s3_router.post("/object/delete")
def delete_objects(
    payload: Dict[str, Any] = Body(...),
    auth: dict = Depends(require_permission(PERM)),
):
    """Deletes up to 1000 keys in one call - S3's own batch limit."""
    bucket, conn = _target(payload.get("bucket"), payload.get("account"))
    _require_writes(conn)
    keys = [_check_key(k) for k in (payload.get("keys") or []) if k]
    if not keys:
        raise HTTPException(status_code=400, detail="No keys given to delete")
    if len(keys) > 1000:
        raise HTTPException(status_code=400, detail="Delete at most 1000 objects at a time")

    result = _call(
        _s3(conn).delete_objects,
        Bucket=bucket,
        Delete={"Objects": [{"Key": k} for k in keys], "Quiet": False},
    )
    deleted = [d["Key"] for d in result.get("Deleted", [])]
    errors = [
        {"key": e.get("Key"), "message": f"{e.get('Code')}: {e.get('Message')}"}
        for e in result.get("Errors", [])
    ]
    logger.info("S3 Explorer: %s deleted %d object(s) from %s", (auth.get("user") or {}).get("email"), len(deleted), bucket)
    return {"bucket": bucket, "account": conn["name"], "deleted": deleted, "errors": errors}