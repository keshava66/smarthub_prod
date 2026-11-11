"""Ingestion Lab: run the ingestion steps defined in ingestion_lab.yaml from the portal.

Each step is a folder plus the command you would type there. It runs on the
portal server itself or on another server over SSH ('servers' in the config,
with per-server vars such as the folder and python path). The page fills in the
step's inputs, the server checks them and starts the command in its own process
group; it keeps running whether or not anyone is watching. People can pick
another folder to run from, but only inside the step's folder_roots.

Runs and their output live in Postgres (V005 migration), same model as
the IT validation runs: the portal process that owns a run writes its output in
small batches and sends NOTIFY ingestion_log; every portal process LISTENs, so
watchers anywhere get new lines straight away. Stop requests travel the same way
on ingestion_stop.

Remote steps write their output to a file on their server and the portal follows
that file, recording how far its saved log goes. A dropped connection or a portal
restart doesn't stop the step: the owner reconnects, or another portal process
picks the run up and carries on from that offset, without losing or repeating
a line.

Inputs never go through a shell unless the step itself is a shell line, and then
they are only handed over as environment variables (IL_<NAME>), never pasted in.
"""
from __future__ import annotations

import asyncio
import base64
import codecs
import copy
import glob as globmod
import hashlib
import json
import logging
import os
import posixpath
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

import db
from audit import write_audit_event
from auth import require_permission

logger = logging.getLogger(__name__)

ingestion_router = APIRouter(prefix="/api/ingestion-lab", tags=["Ingestion Lab"])

VIEW_PERM = "viewIngestionLab"
RUN_PERM = "runIngestionLab"

FLUSH = 0.1          # seconds between log writes while the command is printing
HEARTBEAT = 2.0      # heartbeat + stop check when the command is quiet
FALLBACK = 1.0       # watchers re-read this often if a NOTIFY never arrives
PING = 15.0          # seconds of silence before an SSE comment so proxies keep the stream open
STALE = "60 seconds" # no heartbeat for this long = the owning server died
DRAIN = 2.0          # after the command exits, wait this long for children still holding its output
READ_CHUNK = 65536
MAX_LINE = 8000      # longer lines are cut, the rest of the line is dropped
BACKLOG = 20_000      # lines waiting to be written before the reader pauses (database healthy)
MAX_PENDING = 200_000  # lines held in memory while the database is unreachable
WRITE_BATCH = 20_000   # most lines written per database round trip
STREAM_BATCH = 5000
DOWNLOAD_BATCH = 20_000
CLEANUP_EVERY = 3600.0  # seconds between clearing runs older than keep_days
MASK = "********"

DEFAULTS = {
    "max_concurrent_runs": 4,     # per server a step runs on, across every portal process
    "keep_days": 0,               # 0 = keep every run forever
    "max_log_lines": 20_000,      # per run; with log_keep "last" the oldest lines past this are trimmed
}

STEP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
INPUT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]{0,39})\}")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
SENSITIVE_ENV_RE = re.compile(r"DSN|SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|PRIVATE_?KEY|(^|_)KEY$|CREDENTIAL|"
                              r"CONN(ECTION)?_?STR|^SMARTHUB_", re.I)
URL_CREDS_RE = re.compile(r"://[^/\s:@]*:[^/\s@]+@")   # redis://:pw@host, postgres://u:pw@host
INPUT_TYPES = ("text", "secret", "select", "number", "date", "flag", "profile")
PROFILE_MODES = ("documents", "extracted", "existing")   # main.py: plain ingest, --rechunk-extracted, --embed-existing
PROFILE_KEYS = {"name", "version", "chunking", "metadata", "embedding", "output", "existing_chunks", "extracted_content"}
PROFILE_MAX = 128 * 1024 - 64   # IL_<NAME>=<json> must fit Linux's 128 KiB limit on one env entry
FILE_MARK = "\x00il-file:"   # in argv: where a profile input's file goes, filled in when the run starts
FINAL = ("success", "failed", "stopped", "timed_out", "lost")
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
TEMPLATES_RE = re.compile(r"^[A-Za-z0-9_.*?/-]{1,200}$")   # a glob that is safe to leave unquoted in bash
MAX_TEMPLATES = 100
STOP_SIGNALS = ("TERM", "INT")
IS_WINDOWS = os.name == "nt"
KILL = getattr(signal, "SIGKILL", "KILL")   # Windows has no SIGKILL; _signal_group maps it to taskkill /F


KEEP_HEAD = 50       # with log_keep "last", the first lines (header, start-up) are never trimmed
SERVER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
LOCAL_ID = "local"
SSH_TIMEOUT = 15          # seconds to connect + log in
RECONNECT_MAX = 30        # longest wait between reconnect attempts to a server
ADOPT_EVERY = 15.0        # seconds between looks for remote runs whose portal server went away
GIVE_UP = 30 * 60         # seconds a remote run's server can stay unreachable before it's marked lost
CHECK_TTL = 30            # seconds a server connection check is reused
AUTO_PYTHON = "@python"   # as a step's program: the virtualenv python of the folder it runs in
VENV_DIRS = (".venv", "venv", "env")
VENV_LEVELS = 3           # the run folder and up to 2 folders above it
def _new_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


OWNER = _new_owner()   # this portal process


def _after_fork_in_child():
    # A process manager that forks after import (gunicorn --preload) must not share an owner token.
    global OWNER, _listener_started, _background_loop
    OWNER = _new_owner()
    _listener_started = False
    _background_loop = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


class _NotOwner(Exception):
    """Another portal process has taken the run over; this one must stop writing to it."""


def _under(path: str, root: str) -> bool:
    sep = "/" if root.startswith("/") else os.sep
    root = root.rstrip("/\\") or root
    return path == root or path.startswith(root + sep)


def _is_abs(path: str) -> bool:
    """Absolute on this OS, or a Linux-style absolute path, so a config written for the Linux
    server still loads on a Windows dev machine (Python 3.13 there no longer counts '/home/x')."""
    return path.startswith("/") or os.path.isabs(path)


class ConfigError(ValueError):
    pass


# ============================================================================
# Config
# ============================================================================

class InputSpec:
    def __init__(self, raw: Dict[str, Any], where: str):
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: each input must be an object")
        self.name = str(raw.get("name") or "")
        if not INPUT_NAME_RE.match(self.name):
            raise ConfigError(f"{where}: input name {self.name!r} must be lowercase letters, digits and _")
        where = f"{where}.{self.name}"
        self.type = raw.get("type") or "text"
        if self.type not in INPUT_TYPES:
            raise ConfigError(f"{where}: type must be one of {', '.join(INPUT_TYPES)}")
        self.label = str(raw.get("label") or self.name.replace("_", " ").capitalize())
        self.help = str(raw.get("help") or "")
        self.placeholder = str(raw.get("placeholder") or "")
        self.required = bool(raw.get("required", self.type != "flag"))
        self.default = raw.get("default")
        self.allow_dash = bool(raw.get("allow_dash", False))
        self.advanced = bool(raw.get("advanced", False))   # shown under "More options" on the page
        # type profile: an ingestion processing profile built on the page and handed over as JSON.
        # profile_mode picks which settings the page asks for; templates is a glob of profile
        # files in the run folder the page can start from (e.g. profiles/*.json).
        self.profile_mode = str(raw.get("profile_mode") or "documents")
        if self.profile_mode not in PROFILE_MODES:
            raise ConfigError(f"{where}: profile_mode must be one of {', '.join(PROFILE_MODES)}")
        self.templates = raw.get("templates")
        if self.templates is not None:
            g = self.templates
            if (not isinstance(g, str) or not TEMPLATES_RE.match(g) or g.startswith("/") or ".." in g
                    or self.type != "profile"):
                raise ConfigError(f"{where}: templates must be a relative file pattern like profiles/*.json "
                                  "on a profile input")
        self.max_length = int(raw.get("max_length") or (PROFILE_MAX if self.type == "profile" else 256))
        if self.max_length < 1:
            raise ConfigError(f"{where}: max_length must be at least 1")
        self.flag = str(raw.get("flag") or f"--{self.name.replace('_', '-')}")
        self.min = self._bound(raw.get("min"), where, "min")
        self.max = self._bound(raw.get("max"), where, "max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ConfigError(f"{where}: min is bigger than max")
        self.integer = bool(raw.get("integer", False))
        self.pattern = None
        if raw.get("pattern"):
            try:
                self.pattern = re.compile(str(raw["pattern"]))
            except re.error as e:
                raise ConfigError(f"{where}: pattern is not a valid regex ({e})")
        self.options: List[Dict[str, str]] = []
        if self.type == "select":
            opts = raw.get("options")
            if not isinstance(opts, list) or not opts:
                raise ConfigError(f"{where}: a select needs a non-empty options list")
            for o in opts:
                if isinstance(o, dict):
                    value = str(o.get("value", ""))
                    self.options.append({"value": value, "label": str(o.get("label") or value)})
                else:
                    self.options.append({"value": str(o), "label": str(o)})
            values = [o["value"] for o in self.options]
            if len(set(values)) != len(values):
                raise ConfigError(f"{where}: options must be unique")
        if self.default is not None:
            try:
                self.validate(self.default, check_required=False)
            except ValueError as e:
                raise ConfigError(f"{where}: default is not valid ({e})")

    @staticmethod
    def _bound(value: Any, where: str, key: str):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value \
                or value in (float("inf"), float("-inf")):
            raise ConfigError(f"{where}: {key} must be a number")
        return value

    def validate(self, value: Any, check_required: bool = True) -> str:
        """Return the value as the string handed to the command ('' = not given)."""
        if self.type == "flag":
            if value in (None, ""):
                return ""
            if not isinstance(value, bool):
                raise ValueError("must be true or false")
            return "1" if value else ""
        if self.type == "profile":
            return self._profile(value, check_required)
        if value is None:
            value = ""
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("has the wrong type")
        text = str(value).strip() if self.type != "secret" else str(value)
        if text == "":
            if check_required and self.required:
                raise ValueError("is required")
            return ""
        if len(text) > self.max_length:
            raise ValueError(f"must be at most {self.max_length} characters")
        if CONTROL_RE.search(text):
            raise ValueError("contains control characters")
        if self.type == "select":
            if text not in {o["value"] for o in self.options}:
                raise ValueError("is not one of the allowed options")
            return text
        if self.type == "number":
            if re.fullmatch(r"[+-]?[0-9]+", text):   # whole numbers stay exact, however long
                whole = int(text)
                if self.min is not None and whole < self.min:
                    raise ValueError(f"must be at least {self.min}")
                if self.max is not None and whole > self.max:
                    raise ValueError(f"must be at most {self.max}")
                return str(whole)
            try:
                num = float(text)
            except ValueError:
                raise ValueError("must be a number")
            if num != num or num in (float("inf"), float("-inf")):
                raise ValueError("must be a number")
            if self.integer and not num.is_integer():
                raise ValueError("must be a whole number")
            if self.min is not None and num < float(self.min):
                raise ValueError(f"must be at least {self.min}")
            if self.max is not None and num > float(self.max):
                raise ValueError(f"must be at most {self.max}")
            return str(int(num)) if self.integer or num.is_integer() else repr(num)
        if self.type == "date":
            try:
                return date.fromisoformat(text).isoformat()
            except ValueError:
                raise ValueError("must be a date (YYYY-MM-DD)")
        # text / secret
        if text.startswith("-") and not self.allow_dash:
            raise ValueError("can't start with '-'")
        if self.pattern and not self.pattern.fullmatch(text):
            raise ValueError("isn't in the expected format")
        return text

    def _profile(self, value: Any, check_required: bool) -> str:
        """A profile (an object, or its JSON text) as compact JSON. The shape is checked here;
        the settings themselves are checked by the ingestion script, which knows them."""
        if value in (None, ""):
            if check_required and self.required:
                raise ValueError("is required")
            return ""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError("isn't valid JSON")
        if not isinstance(value, dict):
            raise ValueError("must be a JSON object")
        unknown = set(value) - PROFILE_KEYS
        if unknown:
            raise ValueError(f"has unknown settings: {', '.join(sorted(unknown))}")
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        if len(text.encode("utf-8")) > min(self.max_length, PROFILE_MAX):
            raise ValueError("is too big")
        return text

    def public(self) -> Dict[str, Any]:
        out = {"name": self.name, "label": self.label, "type": self.type, "required": self.required,
               "help": self.help, "placeholder": self.placeholder, "advanced": self.advanced}
        if self.type == "select":
            out["options"] = self.options
        if self.type == "number":
            out.update(min=self.min, max=self.max, integer=self.integer)
        if self.type in ("text", "secret"):
            out["max_length"] = self.max_length
            out["allow_dash"] = self.allow_dash
            if self.pattern:
                out["pattern"] = self.pattern.pattern
        if self.type == "profile":
            out["profile_mode"] = self.profile_mode
            if self.templates:
                out["templates"] = self.templates
        if self.type == "flag":
            out["flag"] = self.flag
        if self.type != "secret" and self.default is not None:
            out["default"] = self.default
        return out


class Step:
    def __init__(self, raw: Dict[str, Any], index: int):
        where = f"ingestion_lab.steps[{index}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: each step must be an object")
        self.id = str(raw.get("id") or "")
        if not STEP_ID_RE.match(self.id):
            raise ConfigError(f"{where}: id {self.id!r} must be lowercase letters, digits, _ or -")
        where = f"step '{self.id}'"
        self.name = str(raw.get("name") or self.id)
        self.description = str(raw.get("description") or "")
        self.server: Optional["Server"] = None   # set when the config is loaded; None = this server
        # No workdir: whoever runs the step picks the folder (needs folder_choice and folder_roots).
        self.workdir = str(raw.get("workdir") or "")
        if self.workdir and not _is_abs(self.workdir):
            raise ConfigError(f"{where}: workdir must be an absolute path")
        if not self.workdir and (not raw.get("folder_roots") or raw.get("folder_choice") is False):
            raise ConfigError(f"{where}: set workdir (an absolute path), or a root on the server "
                              "so people pick the folder")
        self.inputs = [InputSpec(i, where) for i in (raw.get("inputs") or [])]
        names = [i.name for i in self.inputs]
        if len(set(names)) != len(names):
            raise ConfigError(f"{where}: two inputs share a name")
        self.by_name = {i.name: i for i in self.inputs}
        command, shell = raw.get("command"), raw.get("shell")
        if bool(command) == bool(shell):
            raise ConfigError(f"{where}: set exactly one of 'command' (a list) or 'shell' (a string)")
        self.command: List[Any] = []
        self.shell = ""
        if command:
            if not isinstance(command, list):
                raise ConfigError(f"{where}: command must be a list, e.g. [\"python\", \"run.py\", \"--env\", \"{{env}}\"]")
            for part in command:
                group = part if isinstance(part, list) else [part]
                if not group or not all(isinstance(p, str) for p in group):
                    raise ConfigError(f"{where}: command items must be strings or lists of strings")
                for p in group:
                    for ref in PLACEHOLDER_RE.findall(p):
                        if ref not in self.by_name:
                            raise ConfigError(f"{where}: command uses {{{ref}}} but there is no input named {ref}")
                self.command.append(part)
            for part in self.command:
                for p in (part if isinstance(part, list) else [part]):
                    for ref in PLACEHOLDER_RE.findall(p):
                        if self.by_name[ref].type == "profile" and not PLACEHOLDER_RE.fullmatch(p):
                            raise ConfigError(f"{where}: {{{ref}}} is a profile (passed as a file path), "
                                              "so it must be a command item of its own, e.g. [\"--profile\", \"{{%s}}\"]" % ref)
            first = self.command[0]
            if isinstance(first, list) or PLACEHOLDER_RE.search(first):
                raise ConfigError(f"{where}: the program (first command item) must be fixed")
        else:
            if not isinstance(shell, str) or not shell.strip():
                raise ConfigError(f"{where}: shell must be a non-empty string")
            if any(f"{{{n}}}" in shell for n in names):
                raise ConfigError(f"{where}: a shell line can't use {{placeholders}}; read the inputs from $IL_<NAME> instead")
            self.shell = shell
        self.login_shell = bool(raw.get("login_shell", False))
        env = raw.get("env") or {}
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, (str, int, float)) for k, v in env.items()):
            raise ConfigError(f"{where}: env must be an object of strings")
        bad = [k for k in env if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)]
        if bad:
            raise ConfigError(f"{where}: env names must be letters, digits and _ ({', '.join(bad)})")
        self.env = {k: str(v) for k, v in env.items()}
        self.inherit_env = bool(raw.get("inherit_env", True))
        # true: one run at a time on each server; "all_servers": one at a time anywhere
        # (for steps that write to a database every server shares); false: no limit.
        scope = raw.get("one_at_a_time", True)
        if scope not in (True, False, "all_servers"):
            raise ConfigError(f"{where}: one_at_a_time must be true, false or \"all_servers\"")
        self.exclusive = bool(scope)
        self.lock_everywhere = scope == "all_servers"
        self.timeout = max(0, int(float(raw.get("timeout_minutes", 240)) * 60))
        self.stop_grace = max(1, int(raw.get("stop_grace_seconds", 15)))
        sig = str(raw.get("stop_signal") or "TERM").upper().removeprefix("SIG")
        if sig not in STOP_SIGNALS:
            raise ConfigError(f"{where}: stop_signal must be one of {', '.join(STOP_SIGNALS)}")
        self.stop_signal = sig
        self._pidfile_raw, self._log_raw = raw.get("pidfile"), raw.get("log_file")
        self.pidfile = self._path(self._pidfile_raw, where, "pidfile")
        self.log_file = self._path(self._log_raw, where, "log_file")
        if self.log_file and not self.exclusive:
            # remote runs follow this file by byte offset: a second run truncating it corrupts the first
            raise ConfigError(f"{where}: log_file is overwritten by every run, so it needs one_at_a_time "
                              "(true or all_servers)")
        # Letting people pick the folder: only under these roots (symlinks resolved), so the
        # command can't be pointed at whatever happens to be lying around elsewhere.
        self.folder_choice = bool(raw.get("folder_choice", not self.workdir))   # no workdir: always picked
        roots = raw.get("folder_roots")
        if roots is None:
            roots = [posixpath.dirname(self.workdir) if self.workdir.startswith("/") else os.path.dirname(self.workdir)]
        if not isinstance(roots, list) or not roots or not all(isinstance(r, str) and _is_abs(r) for r in roots):
            raise ConfigError(f"{where}: folder_roots must be a list of absolute paths")
        self.folder_roots = [posixpath.normpath(r) if r.startswith("/") else os.path.normpath(r) for r in roots]
        if self.folder_choice and any(r in ("/", "\\") or len(r.strip("/\\")) == 0 for r in self.folder_roots):
            raise ConfigError(f"{where}: folder_roots can't be the whole disk")
        self.folder_marker = str(raw.get("folder_marker") or "") or None
        if self.folder_marker and ("/" in self.folder_marker or self.folder_marker in (".", "..")):
            raise ConfigError(f"{where}: folder_marker must be a file name, like main.py")
        self.nofile = raw.get("nofile_limit")
        if self.nofile is not None:
            if not isinstance(self.nofile, int) or self.nofile < 64:
                raise ConfigError(f"{where}: nofile_limit must be a whole number of at least 64")
        self.max_log_lines = raw.get("max_log_lines")
        if self.max_log_lines is not None and (not isinstance(self.max_log_lines, int) or self.max_log_lines < 1000):
            raise ConfigError(f"{where}: max_log_lines must be a whole number of at least 1000")
        self.log_keep = raw.get("log_keep") or "last"   # the start and the latest output, where errors show
        if self.log_keep not in ("first", "last"):
            raise ConfigError(f"{where}: log_keep must be 'first' or 'last'")
        self.presets: List[Dict[str, Any]] = []
        for i, pr in enumerate(raw.get("presets") or []):
            if not isinstance(pr, dict) or not str(pr.get("name") or "").strip() or not isinstance(pr.get("inputs", {}), dict):
                raise ConfigError(f"{where}: presets[{i}] needs a name and an inputs object")
            given = pr.get("inputs") or {}
            unknown = set(given) - set(self.by_name)
            if unknown:
                raise ConfigError(f"{where}: preset '{pr['name']}' sets unknown input(s) {', '.join(sorted(unknown))}")
            for name, value in given.items():
                spec = self.by_name[name]
                if spec.type == "secret":
                    raise ConfigError(f"{where}: preset '{pr['name']}' can't hold a secret value")
                try:
                    spec.validate(value, check_required=False)
                except ValueError as e:
                    raise ConfigError(f"{where}: preset '{pr['name']}': {spec.label} {e}")
            self.presets.append({"name": str(pr["name"]).strip(), "description": str(pr.get("description") or ""),
                                 "inputs": given})

    def _path(self, value, where, key) -> Optional[str]:
        if not value:
            return None
        value = str(value)
        if _is_abs(value):
            return posixpath.normpath(value) if value.startswith("/") else os.path.normpath(value)
        if self.workdir.startswith("/"):
            return posixpath.normpath(posixpath.join(self.workdir, value))
        return os.path.normpath(os.path.join(self.workdir, value))

    def with_workdir(self, folder: str) -> "Step":
        """This step run from another folder: relative pidfile / log_file move with it."""
        clone = copy.copy(self)
        clone.workdir = folder
        clone.pidfile = clone._path(self._pidfile_raw, "", "pidfile")
        clone.log_file = clone._path(self._log_raw, "", "log_file")
        return clone

    def clean_folder(self, folder: Any) -> str:
        """Normalise a folder someone typed or picked. Raises ValueError with a message.
        (Symlinks are checked where the folder is: here for this server, in the launch
        script for a remote one.)"""
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError("Pick a folder")
        folder = folder.strip()
        if len(folder) > 1024 or CONTROL_RE.search(folder):
            raise ValueError("That folder path isn't valid")
        if not _is_abs(folder):
            raise ValueError("Use a full path, starting with /")
        norm = posixpath.normpath(folder) if folder.startswith("/") else os.path.normpath(folder)
        if not any(_under(norm, r) for r in self.folder_roots):
            raise ValueError(f"Only folders under {', '.join(self.folder_roots)} can be used")
        return norm

    # -- building a run --------------------------------------------------------

    def resolve(self, given: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, str]]:
        """Check every input. Returns (values, problems); values hold '' for not given."""
        if not isinstance(given, dict):
            return {}, {"_": "inputs must be an object"}
        values, problems = {}, {}
        for unknown in set(given) - set(self.by_name):
            problems[unknown] = "is not an input of this step"
        for spec in self.inputs:
            raw = given.get(spec.name)
            if (raw is None or raw == "") and spec.default is not None and spec.type != "secret":
                raw = spec.default
            try:
                values[spec.name] = spec.validate(raw)
            except ValueError as e:
                problems[spec.name] = f"{spec.label} {e}"
        return values, problems

    def argv(self, values: Dict[str, str], masked: bool = False,
             files: Optional[Dict[str, str]] = None) -> List[str]:
        """The command with inputs put in. A list item is a group that is left out
        when any input it uses is empty (e.g. ["--table", "{table}"]). A profile input
        becomes the path of the file its JSON is written to: files[name] when known,
        else a FILE_MARK the launcher swaps for that path (masked: a readable stand-in)."""
        def fill(text: str) -> str:
            def one(m):
                spec = self.by_name[m.group(1)]
                value = values.get(spec.name, "")
                if spec.type == "flag":
                    value = spec.flag if value else ""
                if spec.type == "profile" and value:
                    if files and spec.name in files:
                        return files[spec.name]
                    return f"<{spec.name}.json>" if masked else FILE_MARK + spec.name
                if masked and spec.type == "secret" and value:
                    return MASK
                return value
            return PLACEHOLDER_RE.sub(one, text)

        out: List[str] = []
        for part in self.command:
            group = part if isinstance(part, list) else [part]
            refs = [r for p in group for r in PLACEHOLDER_RE.findall(p)]
            if any(not values.get(r) for r in refs):
                if isinstance(part, list) or all(PLACEHOLDER_RE.fullmatch(p) for p in group):
                    continue   # optional group / lone placeholder with nothing in it
            out.extend(fill(p) for p in group)
        return out

    def env_inputs(self, values: Dict[str, str]) -> Dict[str, str]:
        return {f"IL_{k.upper()}": v for k, v in values.items()}

    def display(self, values: Dict[str, str]) -> str:
        if self.command:
            return shlex.join(self.argv(values, masked=True))
        return self.shell

    def masked_inputs(self, values: Dict[str, str]) -> Dict[str, Any]:
        out = {}
        for spec in self.inputs:
            v = values.get(spec.name, "")
            if spec.type == "flag":
                out[spec.name] = bool(v)
            elif spec.type == "secret":
                out[spec.name] = MASK if v else ""
            else:
                out[spec.name] = v
        return out

    def public(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "description": self.description, "workdir": self.workdir,
                "mode": "command" if self.command else "shell",
                "command": self.command if self.command else self.shell,
                "inputs": [i.public() for i in self.inputs], "one_at_a_time": self.exclusive,
                "one_at_a_time_everywhere": self.lock_everywhere,
                "timeout_minutes": self.timeout // 60 if self.timeout else 0,
                "graceful_stop": self.stop_signal == "INT", "stop_grace_seconds": self.stop_grace,
                "log_keep": self.log_keep, "log_file": self.log_file,
                "folder_choice": self.folder_choice, "folder_roots": self.folder_roots if self.folder_choice else [],
                "folder_marker": self.folder_marker,
                "presets": self.presets}


class Server:
    """Where steps run: this portal server itself, or another machine reached over SSH."""

    def __init__(self, raw: Dict[str, Any], index: int):
        where = f"ingestion_lab.servers[{index}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: each server must be an object")
        self.id = str(raw.get("id") or "")
        if not SERVER_ID_RE.match(self.id):
            raise ConfigError(f"{where}: id {self.id!r} must be lowercase letters, digits, _ or -")
        where = f"server '{self.id}'"
        self.name = str(raw.get("name") or self.id)
        self.local = bool(raw.get("local", False))
        self.vars = {}
        for k, v in (raw.get("vars") or {}).items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k)) or not isinstance(v, (str, int, float)):
                raise ConfigError(f"{where}: vars must map simple names to strings")
            self.vars[str(k)] = str(v)
        self.host = str(raw.get("host") or "")
        self.port = int(raw.get("port") or 22)
        self.username = str(raw.get("username") or "")
        self.password = raw.get("password")
        if raw.get("password_env"):
            self.password = os.environ.get(str(raw["password_env"])) or self.password
        self.key_file = str(raw["key_file"]) if raw.get("key_file") else None
        self.host_key = str(raw.get("host_key") or "").strip() or None
        if self.host_key and not self.host_key.startswith("SHA256:"):
            self.host_key = "SHA256:" + self.host_key
        self.known_hosts = str(raw["known_hosts"]) if raw.get("known_hosts") else None
        self.spool_dir = str(raw.get("spool_dir") or ".smarthub-ingestion")
        # The folder(s) people may run steps from on this server (a path, or a list of them).
        root = raw.get("root")
        roots = root if isinstance(root, list) else [root] if root else []
        if not all(isinstance(r, str) and _is_abs(r) and r.strip("/\\") for r in roots):
            raise ConfigError(f"{where}: root must be an absolute folder (or a list of them), not the whole disk")
        self.roots = [posixpath.normpath(r) if r.startswith("/") else os.path.normpath(r) for r in roots]
        if not self.local:
            if not self.host or not self.username:
                raise ConfigError(f"{where}: a remote server needs host and username (or set \"local\": true)")
            if not self.password and not self.key_file:
                raise ConfigError(f"{where}: set password (or password_env / key_file) for SSH")
            if not (1 <= self.port <= 65535):
                raise ConfigError(f"{where}: port must be 1-65535")

    def public(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "local": self.local,
                "host": socket.gethostname() if self.local else self.host}


def _default_server() -> Server:
    return Server({"id": LOCAL_ID, "name": f"This server ({socket.gethostname()})", "local": True}, 0)


def _substitute(raw: Dict[str, Any], server: Server) -> Dict[str, Any]:
    """The step as it looks on one server: ${name} replaced from that server's vars.
    Raises KeyError(name) when the server doesn't define one."""
    variables = {"server": server.id, **({"root": server.roots[0]} if server.roots else {}), **server.vars}

    def sub(text):
        if not isinstance(text, str):
            return text
        def one(m):
            if m.group(1) not in variables:
                raise KeyError(m.group(1))
            return variables[m.group(1)]
        return VAR_RE.sub(one, text)

    out = copy.deepcopy(raw)
    for key in ("workdir", "shell", "pidfile", "log_file"):
        if key in out:
            out[key] = sub(out[key])
    if isinstance(out.get("folder_roots"), list):
        out["folder_roots"] = [sub(r) for r in out["folder_roots"]]
    elif "folder_roots" not in out and server.roots:
        out["folder_roots"] = list(server.roots)   # the server's root, unless the step sets its own
    if isinstance(out.get("command"), list):
        out["command"] = [[sub(p) for p in part] if isinstance(part, list) else sub(part) for part in out["command"]]
    if isinstance(out.get("env"), dict):
        out["env"] = {k: sub(v) for k, v in out["env"].items()}
    return out


class StepGroup:
    """One configured step and its variant on each server it can run on."""

    def __init__(self, base: Step, variants: Dict[str, Step]):
        self.base = base
        self.id = base.id
        self.variants = variants

    def public(self, servers: Dict[str, Server]) -> Dict[str, Any]:
        out = self.base.public()
        out["servers"] = {sid: {"workdir": v.workdir, "folder_roots": v.folder_roots if v.folder_choice else [],
                                "command": v.command if v.command else v.shell,
                                "log_file": v.log_file, "pidfile": v.pidfile}
                          for sid, v in self.variants.items() if sid in servers}
        return out


def _read_config(path: str) -> Dict[str, Any]:
    """The Lab's own config file. YAML (so it can carry comments); plain JSON is valid YAML too."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        import yaml
    except ImportError:   # ponytail: JSON still works without PyYAML installed
        data = json.loads(text)
    else:
        data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("the top level must be a mapping (servers:, steps: ...)")
    return data


class _Config:
    """The Lab's config file, re-read when it changes so new or edited steps show up
    without restarting the server."""

    def __init__(self):
        self.lock = threading.Lock()
        self.path: Optional[str] = None
        self.mtime: Optional[float] = None
        self.checked = 0.0
        self.settings = dict(DEFAULTS)
        self.servers: Dict[str, Server] = {LOCAL_ID: _default_server()}
        self.steps: Dict[str, StepGroup] = {}
        self.errors: List[str] = []
        self.configured = False

    def load(self, section: Optional[Dict[str, Any]]):
        settings = dict(DEFAULTS)
        steps: Dict[str, StepGroup] = {}
        servers: Dict[str, Server] = {}
        errors: List[str] = []
        configured = isinstance(section, dict)
        if configured:
            raw_servers = section.get("servers")
            if raw_servers is None:
                servers[LOCAL_ID] = _default_server()
            elif not isinstance(raw_servers, list) or not raw_servers:
                errors.append("ingestion_lab.servers must be a non-empty list")
            else:
                for i, raw in enumerate(raw_servers):
                    try:
                        srv = Server(raw, i)
                    except ConfigError as e:
                        errors.append(str(e))
                        continue
                    except (ValueError, TypeError, AttributeError) as e:   # e.g. "port": "abc"
                        errors.append(f"ingestion_lab.servers[{i}]: a value has the wrong type ({e})")
                        continue
                    if srv.id in servers:
                        errors.append(f"two servers share the id '{srv.id}'")
                        continue
                    servers[srv.id] = srv
            for key in DEFAULTS:
                if section.get(key) is not None:
                    try:
                        settings[key] = max(0 if key == "keep_days" else 1, int(section[key]))
                    except (TypeError, ValueError):
                        errors.append(f"ingestion_lab.{key} must be a whole number")
            raw_steps = section.get("steps") or []
            if not isinstance(raw_steps, list):
                errors.append("ingestion_lab.steps must be a list")
                raw_steps = []
            # Lab-wide folder settings every step inherits unless it sets its own.
            inherited = {k: section[k] for k in ("folder_choice", "folder_roots", "folder_marker") if k in section}
            for i, raw in enumerate(raw_steps):
                if isinstance(raw, dict) and inherited:
                    raw = {**inherited, **raw}
                group = self._step_group(raw, i, servers, errors)
                if group is None:
                    continue
                if group.id in steps:
                    errors.append(f"two steps share the id '{group.id}'")
                    continue
                steps[group.id] = group
        for e in errors:
            logger.warning("Ingestion Lab config: %s", e)
        if not servers:
            servers[LOCAL_ID] = _default_server()
        with self.lock:
            self.settings, self.steps, self.errors, self.configured = settings, steps, errors, configured
            self.servers = servers

    @staticmethod
    def _step_group(raw, index, servers: Dict[str, Server], errors: List[str]) -> Optional[StepGroup]:
        if not isinstance(raw, dict):
            errors.append(f"ingestion_lab.steps[{index}]: each step must be an object")
            return None
        sid = str(raw.get("id") or f"steps[{index}]")
        wanted = raw.get("servers")
        if wanted is not None and (not isinstance(wanted, list) or not all(isinstance(w, str) for w in wanted)):
            errors.append(f"step '{sid}': servers must be a list of server ids")
            return None
        if wanted:
            unknown = [w for w in wanted if w not in servers]
            if unknown:
                errors.append(f"step '{sid}': unknown server(s) {', '.join(unknown)}")
                return None
        variants: Dict[str, Step] = {}
        missing: Dict[str, str] = {}
        for srv in servers.values():
            if wanted and srv.id not in wanted:
                continue
            try:
                resolved = _substitute(raw, srv)
            except KeyError as e:
                missing[srv.id] = e.args[0]
                continue
            try:
                step = Step(resolved, index)
            except ConfigError as e:
                errors.append(f"{e} (on server '{srv.id}')" if len(servers) > 1 else str(e))
                return None
            except (ValueError, TypeError, AttributeError) as e:   # e.g. "timeout_minutes": "abc"
                errors.append(f"step '{sid}': a value has the wrong type ({e})")
                return None
            step.server = srv
            variants[srv.id] = step
        for srv_id, var in missing.items():
            if wanted:   # asked for explicitly, so a missing variable is a mistake
                errors.append(f"step '{sid}': server '{srv_id}' has no ${{{var}}} in its vars")
        if not variants:
            if missing and not wanted:
                errors.append(f"step '{sid}': no server defines " +
                              ", ".join(sorted({f"${{{v}}}" for v in missing.values()})))
            elif not missing:
                errors.append(f"step '{sid}': no servers to run on")
            return None
        return StepGroup(next(iter(variants.values())), variants)

    def refresh(self):
        if not self.path:
            return
        now = time.monotonic()
        if now - self.checked < 2:
            return
        self.checked = now
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            return
        if mtime == self.mtime:
            return
        try:
            section = _read_config(self.path)
        except Exception as e:  # noqa: BLE001 - a typo in the file must not take the page down
            logger.warning("Ingestion Lab: couldn't read %s (%s); keeping the steps already loaded", self.path, e)
            self.mtime = mtime
            with self.lock:
                self.errors = [f"{os.path.basename(self.path)} couldn't be read, so the last good version is "
                               f"still in use: {e}"]
            return
        self.mtime = mtime
        self.load(section)
        logger.info("Ingestion Lab: reloaded %d step(s) on %d server(s) from %s", len(self.steps), len(self.servers), self.path)

    def snapshot(self):
        self.refresh()
        with self.lock:
            return self.settings, self.steps, self.errors, self.configured

    def servers_snapshot(self) -> Dict[str, Server]:
        self.refresh()
        with self.lock:
            return dict(self.servers)


_cfg = _Config()


def _cfg_name() -> str:
    return os.path.basename(_cfg.path or CONFIG_FILE)


CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ingestion_lab.yaml")


def set_ingestion_lab_dependencies(config_path: Optional[str] = None,
                                   legacy_section: Optional[Dict[str, Any]] = None):
    """Called once at startup. The Lab has its own config file: config_path, else
    $INGESTION_LAB_CONFIG, else backend/ingestion_lab.yaml. An old 'ingestion_lab' section
    from dq_config.json is only used until that file exists (it's picked up without a restart)."""
    path = config_path or os.environ.get("INGESTION_LAB_CONFIG") or CONFIG_FILE
    _cfg.path, _cfg.mtime, _cfg.checked = path, None, 0.0
    if os.path.exists(path):
        _cfg.refresh()
    elif legacy_section is not None:
        _cfg.load(legacy_section)
        logger.warning("Ingestion Lab: using the 'ingestion_lab' section of dq_config.json. "
                       "Move it to %s; the old section is ignored once that file exists.", path)
    else:
        _cfg.load(None)
    if not _cfg.configured:
        logger.warning("⚠️ %s not found - Ingestion Lab has no steps to run", path)
    else:
        logger.info("Ingestion Lab: %d step(s) configured on %d server(s)", len(_cfg.steps), len(_cfg.servers))


# ============================================================================
# Wake-ups (LISTEN/NOTIFY)
# ============================================================================

class _Wake:
    """Waiters grab .ev before reading, so a wake that lands mid-read still counts."""
    __slots__ = ("ev", "waiters")

    def __init__(self):
        self.ev = asyncio.Event()
        self.waiters = 0

    def fire(self):
        old, self.ev = self.ev, asyncio.Event()
        old.set()


_LOG_WAKE: Dict[str, _Wake] = {}
_STOP_WAKE: Dict[str, _Wake] = {}
_listener_started = False
_ACTIVE: Dict[str, "_Run"] = {}   # runs this server owns
_TASKS: set = set()


def _wake(table: Dict[str, _Wake], run_id: str) -> _Wake:
    w = table.get(run_id)
    if w is None:
        w = table[run_id] = _Wake()
    return w


_listen_loop = None


def _start_listener():
    """One LISTEN thread per process; it wakes whichever event loop is current."""
    global _listener_started, _listen_loop
    _listen_loop = asyncio.get_running_loop()
    if _listener_started:
        return
    _listener_started = True

    def on_notify(channel, run_id):
        tables = (_LOG_WAKE,) if channel == "ingestion_log" else (_STOP_WAKE, _LOG_WAKE)  # watchers show "Stopping…"
        for table in tables:
            w = table.get(run_id)
            if w:
                w.fire()

    def listen_forever():
        backoff = 1
        while True:
            conn = None
            try:
                conn = db.open_connection(application_name="smarthub-ingestion-listen")
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute("LISTEN ingestion_log; LISTEN ingestion_stop;")
                backoff, idle = 1, 0.0
                while True:
                    if select.select([conn], [], [], 5)[0]:
                        conn.poll()
                        while conn.notifies:
                            n = conn.notifies.pop(0)
                            try:
                                _listen_loop.call_soon_threadsafe(on_notify, n.channel, n.payload)
                            except RuntimeError:
                                pass   # that loop has closed; the next request installs the new one
                        idle = 0.0
                    else:
                        idle += 5
                        if idle >= 30:
                            with conn.cursor() as cur:
                                cur.execute("SELECT 1")
                            idle = 0.0
            except Exception as e:  # noqa: BLE001 - keep listening whatever happens
                logger.warning("Ingestion listener: %s; reconnecting in %ss (watchers fall back to polling)", e, backoff)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    threading.Thread(target=listen_forever, name="ingestion-listener", daemon=True).start()


def _db(fn, *args, read_only=False):
    def job():
        with db.transaction(read_only=read_only) as conn:
            return fn(conn, *args)
    return asyncio.to_thread(job)


_FOLLOWABLE = "(remote AND pid IS NOT NULL)"   # still running on its server; another portal can pick it up


def _reap(conn):
    conn.execute(f"""UPDATE ingestion_runs SET status = 'lost', finished_at = now()
                     WHERE finished_at IS NULL AND heartbeat_at < now() - interval '{STALE}' AND NOT {_FOLLOWABLE}""")


def _user(auth: dict) -> str:
    user = auth.get("user") or {}
    name = re.sub(r"\s*\[[^\]]*\]", "", user.get("name") or "").strip()
    return name or user.get("email") or "a user"


def _email(auth: dict) -> str:
    return (auth.get("user") or {}).get("email") or ""


def _now():
    return datetime.now(timezone.utc)


# ============================================================================
# Running a command
# ============================================================================

class _Log:
    """Output buffered in memory (filled from the reader thread), written in batches.

    While the database keeps up nothing is ever dropped: once BACKLOG lines are
    waiting, the reader stops pulling from the pipe until a write catches up, so
    the command just waits on its own output. Only when writes are failing (the
    database is down) does output past MAX_PENDING get counted and skipped, so the
    command isn't held up by an outage."""

    def __init__(self, run_id: str, max_lines: int, keep: str = "last"):
        self.run_id = run_id
        self.max_lines = max_lines
        self.keep = keep         # "first": stop saving at max_lines; "last": trim the oldest instead
        self.trimmed_to = 0      # with keep "last": rows up to this seq (after the head) are gone
        self.file = None         # optional plain-text copy of everything (step's log_file)
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.pending: List[tuple] = []
        self.seq = 0
        self.stored = 0          # output lines kept (counts against max_lines)
        self.over = 0            # output lines past max_lines
        self.dropped = 0         # lines skipped while the database was unreachable, not yet reported
        self.skipped = 0         # all lines ever skipped that way
        self.truncated = False
        self.db_ok = True
        self.closing = False
        self.seen_offset: Optional[int] = None   # remote steps: bytes of the server's output file read so far
        self.secrets: List[str] = []             # secret input values, masked if the command prints them

    def add(self, kind: str, message: str = "", offset: Optional[int] = None):
        for secret in self.secrets:
            if secret in message:
                message = message.replace(secret, MASK)
        with self.cond:
            if offset is not None:
                self.seen_offset = offset
            if self.file:
                try:
                    self.file.write(("-" * 60 if kind == "separator" else message) + "\n")
                except (OSError, ValueError):
                    self.file = None
            if kind == "output":
                if self.keep == "first" and self.stored >= self.max_lines:
                    if not self.truncated:
                        self.truncated = True
                        self._push("error", f"Output passed {self.max_lines:,} lines. The rest isn't saved, "
                                            "the command keeps running.")
                    self.over += 1
                    return
                while len(self.pending) >= BACKLOG and self.db_ok and not self.closing:
                    self.cond.wait(1.0)
                if len(self.pending) >= MAX_PENDING:
                    self.dropped += 1
                    self.skipped += 1
                    return
                self.stored += 1
            self._push(kind, message, offset)

    def _push(self, kind, message, offset=None):
        self.seq += 1
        self.pending.append((self.seq, _now(), kind, message, offset))

    @property
    def backlog(self) -> int:
        return len(self.pending)

    def release(self):
        with self.cond:
            self.closing = True
            self.cond.notify_all()

    def close_file(self):
        with self.cond:
            f, self.file = self.file, None
        if f:
            try:
                f.close()
            except OSError:
                pass

    def flush(self, conn) -> Tuple[Optional[str], bool]:
        """Write buffered lines + heartbeat and wake every watcher. Returns (who asked to stop, force)."""
        with self.cond:
            if self.dropped:
                self._push("error", f"{self.dropped:,} lines were skipped while the database was unreachable.")
                self.dropped = 0
            rows, self.pending = self.pending[:WRITE_BATCH], self.pending[WRITE_BATCH:]
            stored, truncated = self.stored + self.over + self.skipped, self.truncated or self.skipped > 0
            # How far into a remote output file the saved log now reaches. With lines still
            # waiting, only as far as the last one written; otherwise everything seen.
            if self.pending:
                offset = max((r[4] for r in rows if r[4] is not None), default=None)
            else:
                offset = self.seen_offset
            trim_to = 0
            if self.keep == "last" and rows:
                newest = rows[-1][0]
                # trim in steps of a tenth so it's one cheap DELETE now and then, not every write
                if newest - KEEP_HEAD - self.trimmed_to > self.max_lines * 1.1:
                    trim_to = newest - self.max_lines
            if self.file:
                try:
                    self.file.flush()
                except (OSError, ValueError):
                    self.file = None
        try:
            # Ownership first, in the same transaction as the lines: a portal that has been
            # taken over must not add lines the new owner is reading again from the file.
            row = conn.execute("""UPDATE ingestion_runs SET heartbeat_at = now(), log_lines = ?, log_truncated = ?,
                                         remote_offset = GREATEST(remote_offset, COALESCE(?, 0))
                                  WHERE id = ? AND (owner = ? OR owner IS NULL)
                                  RETURNING stop_requested_by, force_stop_requested""",
                               (stored, truncated or bool(trim_to), offset, self.run_id, OWNER)).fetchone()
            if row is None:
                exists = conn.execute("SELECT 1 FROM ingestion_runs WHERE id = ?", (self.run_id,)).fetchone()
                if exists:
                    raise _NotOwner()
            if rows:
                # one statement for the whole batch: far faster than a row at a time
                seqs, tss, kinds, msgs = (list(col) for col in list(zip(*rows))[:4])
                conn.execute("""INSERT INTO ingestion_run_logs (run_id, seq, ts, type, message)
                                SELECT ?, u.seq, u.ts, u.type, u.message
                                FROM unnest(?::int[], ?::timestamptz[], ?::text[], ?::text[]) AS u(seq, ts, type, message)""",
                             (self.run_id, seqs, tss, kinds, msgs))
                conn.execute("SELECT pg_notify('ingestion_log', ?)", (self.run_id,))
            if trim_to:
                conn.execute("DELETE FROM ingestion_run_logs WHERE run_id = ? AND seq > ? AND seq <= ?",
                             (self.run_id, KEEP_HEAD, trim_to))
        except _NotOwner:
            with self.cond:
                self.pending = []          # the new owner re-reads them from the file
                self.closing = True
                self.cond.notify_all()
            raise
        except Exception:
            with self.cond:
                self.pending = rows + self.pending
                self.db_ok = False
                self.cond.notify_all()
            raise
        with self.cond:
            self.db_ok = True
            if trim_to:
                self.trimmed_to = trim_to
                self.truncated = True
            self.cond.notify_all()
        if not row:
            return None, False
        return row["stop_requested_by"], bool(row["force_stop_requested"])


def _clean(line: str) -> str:
    line = line.rstrip("\r")
    if "\r" in line:   # progress bars redraw with \r: keep what's on screen at the end
        line = next((seg for seg in reversed(line.split("\r")) if seg.strip()), "")
    line = ANSI_RE.sub("", line)
    line = CONTROL_RE.sub("", line.replace("\t", "    ")).rstrip()
    if len(line) > MAX_LINE:
        line = line[:MAX_LINE] + " …[line cut]"
    return line


def _signal_group(process: subprocess.Popen, sig):
    """Signal the command and everything it started."""
    try:
        if not IS_WINDOWS:
            os.killpg(process.pid, sig)
        elif sig == KILL:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        elif process.poll() is None:
            # the closest Windows has to Ctrl-C for a process in its own group
            process.send_signal(signal.CTRL_BREAK_EVENT)
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
        pass


# ============================================================================
# Other servers, over SSH
# ============================================================================

class SshError(Exception):
    """A connection problem worth showing to the person as is."""


def _fingerprint(key) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


def _ssh_connect(server: Server):
    """Log in to a server. Host keys are checked: pinned in the config (host_key), or in
    known_hosts; an unknown key is refused with its fingerprint so it can be pinned."""
    try:
        import paramiko
    except ImportError:
        raise SshError("Running steps on other servers needs the paramiko package on the portal server "
                       "(pip install paramiko).")

    class Pinned(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            fp = _fingerprint(key)
            if server.host_key and fp == server.host_key:
                client.get_host_keys().add(hostname, key.get_name(), key)
                return
            raise SshError(f"{server.name}'s host key isn't trusted yet. If this is the right machine, add "
                           f"\"host_key\": \"{fp}\" to server '{server.id}' in {_cfg_name()}.")

    client = paramiko.SSHClient()
    try:
        if server.known_hosts:
            client.load_host_keys(os.path.expanduser(server.known_hosts))
        else:
            client.load_system_host_keys()
    except (OSError, IOError):
        pass
    client.set_missing_host_key_policy(Pinned())
    try:
        client.connect(server.host, port=server.port, username=server.username, password=server.password or None,
                       key_filename=server.key_file, timeout=SSH_TIMEOUT, banner_timeout=SSH_TIMEOUT,
                       auth_timeout=SSH_TIMEOUT, allow_agent=False, look_for_keys=False)
    except SshError:
        client.close()
        raise
    except paramiko.BadHostKeyException as e:
        client.close()
        raise SshError(f"{server.name}'s host key has changed (now {_fingerprint(e.key)}). If that's expected, "
                       f"update host_key for server '{server.id}'; otherwise someone may be in the middle.")
    except paramiko.AuthenticationException:
        client.close()
        raise SshError(f"SSH login to {server.name} as {server.username} was refused: check the username and password.")
    except (socket.timeout, TimeoutError):
        client.close()
        raise SshError(f"{server.name} ({server.host}:{server.port}) didn't answer within {SSH_TIMEOUT}s.")
    except (OSError, paramiko.SSHException) as e:
        client.close()
        raise SshError(f"Couldn't connect to {server.name} ({server.host}:{server.port}): {e}")
    transport = client.get_transport()
    if server.host_key and _fingerprint(transport.get_remote_server_key()) != server.host_key:
        client.close()
        raise SshError(f"{server.name}'s host key doesn't match the host_key in {_cfg_name()}.")
    transport.set_keepalive(20)
    return client


class _Ssh:
    """One server's SSH connection, reconnected on demand. Thread-safe."""

    def __init__(self, server: Server):
        self.server = server
        self.client = None
        self.lock = threading.Lock()

    def _live(self):
        with self.lock:
            t = self.client.get_transport() if self.client else None
            if t is None or not t.is_active():
                if self.client:
                    self.client.close()
                self.client = _ssh_connect(self.server)
            return self.client

    def open(self, command: str, stdin: Optional[str] = None):
        chan = self._live().get_transport().open_session(timeout=SSH_TIMEOUT)
        chan.set_combine_stderr(True)
        chan.exec_command(command)
        if stdin is not None:
            chan.sendall(stdin.encode("utf-8"))
            chan.shutdown_write()
        return chan

    def run(self, command: str, stdin: Optional[str] = None, timeout: float = 60) -> Tuple[int, str]:
        chan = self.open(command, stdin)
        chan.settimeout(timeout)
        out = b""
        try:
            while True:
                data = chan.recv(READ_CHUNK)
                if not data:
                    break
                out += data
            return chan.recv_exit_status(), out.decode("utf-8", "replace")
        finally:
            chan.close()

    def signal_group(self, pid: int, sig) -> Optional[str]:
        """Signal a remote process group. Returns an error message, or None."""
        name = {signal.SIGINT: "INT", signal.SIGTERM: "TERM"}.get(sig, "KILL")
        last = None
        for attempt in range(3):
            try:
                self.run(f"kill -{name} -- -{int(pid)} 2>/dev/null || kill -{name} {int(pid)} 2>/dev/null; true", timeout=20)
                return None
            except Exception as e:  # noqa: BLE001
                last = str(e)
                time.sleep(1 + attempt)   # _live() reconnects if the connection itself died
        return last

    def close(self):
        with self.lock:
            if self.client:
                self.client.close()
            self.client = None


_SSH_POOL: Dict[tuple, _Ssh] = {}
_SSH_POOL_LOCK = threading.Lock()


def _ssh_for(server: Server) -> _Ssh:
    """One shared, kept-alive connection per server (and per set of credentials): runs, stops
    and connection checks open channels on it instead of logging in each time."""
    key = (server.host, server.port, server.username,
           hashlib.sha256((server.password or "").encode()).hexdigest(), server.key_file, server.host_key, server.known_hosts)
    with _SSH_POOL_LOCK:
        ssh = _SSH_POOL.get(key)
        if ssh is None:
            ssh = _SSH_POOL[key] = _Ssh(server)
        return ssh


class _RemoteProcess:
    """Stands in for Popen while a step runs on another server."""

    def __init__(self, ssh: _Ssh, pid: int, log: "_Log"):
        self.ssh, self.pid, self.log = ssh, pid, log
        self.returncode: Optional[int] = None

    def poll(self):
        return self.returncode

    def signal(self, sig):
        def go():
            err = self.ssh.signal_group(self.pid, sig)
            if err:
                self.log.add("error", f"Couldn't reach {self.ssh.server.name} to stop the step: {err}")
        threading.Thread(target=go, daemon=True).start()


def _q(text: str) -> str:
    return shlex.quote(text)


def _alive_sh(pid_expr: str) -> str:
    """Shell test: process is running (a zombie waiting to be reaped doesn't count)."""
    return f'{{ s=$(ps -o stat= -p {pid_expr} 2>/dev/null) && [ -n "$s" ] && [ "${{s#Z}}" = "$s" ]; }}'



def _launch_script(run_id: str, step: Step, values: Dict[str, str], argv: List[str]) -> str:
    """bash, sent on stdin (so nothing secret shows in `ps`). Starts the step detached, with its
    output going to a file on that server, and prints its pid. The step keeps running if
    the SSH connection or the portal goes away."""
    srv = step.server
    env = {**step.env, **step.env_inputs(values), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
           "IL_RUN_ID": run_id, "IL_STEP_ID": step.id}
    exports = "\n".join(f"export {k}={_q(v)}" for k, v in env.items())
    spool = _q(step.log_file) if step.log_file else f'"$IL_DIR/{run_id}.log"'
    nofile = (f'ulimit -n {int(step.nofile)} 2>/dev/null || echo "Could not raise the open-file limit to {int(step.nofile)} '
              f'(hard limit $(ulimit -Hn)); carrying on with $(ulimit -n)." >> "$IL_SPOOL"') if step.nofile else ":"
    pid_check = ""
    if step.pidfile:
        prog = posixpath.basename(_program(step)) if step.command else "bash"
        pid_check = f"""
export IL_PIDFILE={_q(step.pidfile)}
if [ -f "$IL_PIDFILE" ]; then
  op=$(tr -dc 0-9 < "$IL_PIDFILE" 2>/dev/null)
  if [ -n "$op" ] && {_alive_sh('"$op"')} && tr '\\0' ' ' < "/proc/$op/cmdline" 2>/dev/null | grep -qF -- {_q(prog)}; then
    echo {_q(step.name)}" is already running outside the portal on $(hostname) (PID $op, from $IL_PIDFILE). Stop that one first, or wait for it to finish."
    exit 98
  fi
fi"""
    auto = bool(argv) and argv[0] == AUTO_PYTHON
    find_py = ""
    if auto:   # checked before the run too; this covers a virtualenv removed in between
        find_py = f"""
IL_PY=""; d=$(pwd -P); n=0
while [ $n -lt {VENV_LEVELS} ] && [ -z "$IL_PY" ]; do
  for v in {" ".join(VENV_DIRS)}; do if [ -x "$d/$v/bin/python" ]; then IL_PY="$d/$v/bin/python"; break; fi; done
  [ "$d" = "/" ] && break; d=$(dirname -- "$d"); n=$((n+1))
done
[ -n "$IL_PY" ] || {{ echo "No virtualenv (.venv, venv or env) in $(pwd -P) or the 2 folders above it, so nothing was started. Create one from the Ingestion Lab page."; exit 94; }}
IL_PY_FROM="virtualenv in $(dirname -- "$(dirname -- "$(dirname -- "$IL_PY")")")"
"""
    program = '"$IL_PY"' if auto else _q(argv[0])

    def arg(a: str) -> str:
        if a.startswith(FILE_MARK):
            return f'"$IL_DIR/$IL_RUN_ID-{a[len(FILE_MARK):]}.json"'
        return _q(a)
    profiles = "\n".join(
        f'(umask 077; printf "%s" "$IL_{s.name.upper()}" > "$IL_DIR/$IL_RUN_ID-{s.name}.json") || '
        f'{{ echo "Couldn\'t write the profile to $IL_DIR on $(hostname)."; exit 96; }}'
        for s in step.inputs if s.type == "profile" and values.get(s.name))
    runner = ("trap ':' INT TERM\n"            # trapped, not ignored: the step itself still gets Ctrl-C
              f"{nofile}\n"
              '"$@" < /dev/null >> "$IL_SPOOL" 2>&1\n'
              'rc=$?\n'
              'echo "$rc" > "$IL_DIR/$IL_RUN_ID.exit"\n'
              'if [ -n "${IL_PIDFILE:-}" ] && [ "$(cat "$IL_PIDFILE" 2>/dev/null)" = "$$" ]; then rm -f -- "$IL_PIDFILE"; fi\n')
    return f"""set -u
{exports}
cd ~ 2>/dev/null
mkdir -p -- {_q(srv.spool_dir)} 2>/dev/null && IL_DIR=$(cd -- {_q(srv.spool_dir)} && pwd) || {{ printf "Couldn't create %s in $HOME on $(hostname).\\n" {_q(srv.spool_dir)}; exit 96; }}
export IL_DIR
trap 'rm -f -- "$IL_DIR/$IL_RUN_ID"-*.json' EXIT   # step never started: don't leave its profile behind
{profiles}
cd -- {_q(step.workdir)} 2>/dev/null || {{ printf "Folder %s doesn't exist on $(hostname), or $(id -un) can't open it.\\n" {_q(step.workdir)}; exit 97; }}
{_root_check_sh(step) if step.folder_choice else ""}
{find_py}
export IL_SPOOL={spool}
{pid_check}
mkdir -p -- "$(dirname -- "$IL_SPOOL")" 2>/dev/null
: > "$IL_SPOOL" 2>/dev/null || {{ echo "Can't write $IL_SPOOL on $(hostname)."; exit 96; }}
rm -f -- "$IL_DIR/$IL_RUN_ID.exit"
STD=""
command -v stdbuf >/dev/null 2>&1 && STD="stdbuf -oL -eL"
setsid bash -c {_q(runner)} il-run $STD {" ".join([program] + [arg(a) for a in argv[1:]])} < /dev/null > /dev/null 2>&1 &
pid=$!
trap - EXIT
if [ -n "${{IL_PIDFILE:-}}" ]; then echo "$pid" > "$IL_PIDFILE" || echo "Couldn't write $IL_PIDFILE on $(hostname)."; fi
echo "__IL_PID__ $pid"
echo "__IL_SPOOL__ $IL_SPOOL"
echo "__IL_DIR__ $IL_DIR"
{'echo "__IL_PY__ $IL_PY ($IL_PY_FROM)"' if auto else ""}
"""


def _root_check_sh(step: Step, fail: str = "exit 95") -> str:
    """Shell: the current folder, symlinks resolved, must be inside one of the step's roots."""
    roots = " ".join(_q(r) for r in step.folder_roots)
    allowed = _q(", ".join(step.folder_roots))
    return f"""IL_REAL=$(pwd -P); IL_OK=
for r in {roots}; do rr=$(cd -- "$r" 2>/dev/null && pwd -P) || continue; case "$IL_REAL/" in "$rr"/*) IL_OK=1;; esac; done
[ -n "$IL_OK" ] || {{ printf "%sThat folder is really %s, which is outside the allowed folders (%s).\\n" "${{IL_ERR_TAG:-}}" "$IL_REAL" {allowed}; {fail}; }}"""


def _status_command(pid: int, rdir: str, run_id: str) -> str:
    exitf = _q(f"{rdir}/{run_id}.exit")
    return (f'if [ -f {exitf} ]; then echo "__IL_EXIT__ $(cat {exitf})"; '
            f'elif {_alive_sh(str(int(pid)))}; then echo __IL_RUNNING__; else echo __IL_GONE__; fi')


class _Run:
    def __init__(self, run_id: str, step: Step, values: Dict[str, str], log: _Log):
        self.id = run_id
        self.step = step
        self.values = values
        self.log = log
        self.process: Optional[subprocess.Popen] = None
        self.stopped_by: Optional[str] = None
        self.timed_out = False
        self.ending = False
        self.pending_end: Optional[tuple] = None
        self.server_stopping = False
        self.killed = False
        self.detach = False      # remote step: let go of it (portal shutting down), it keeps running there
        self.taken_over = False  # another portal process owns the run now: hands off entirely
        self.python: Optional[str] = None   # what @python resolved to (local steps)
        self.files: Dict[str, str] = {}      # profile input -> the file its JSON goes in
        self.email = ""                      # who started it, for the audit trail

    @property
    def remote(self) -> bool:
        return self.step.server is not None and not self.step.server.local

    def _signal(self, sig):
        if isinstance(self.process, _RemoteProcess):
            self.process.signal(sig)
        else:
            _signal_group(self.process, sig)

    def build(self) -> Tuple[List[str], Dict[str, str]]:
        step = self.step
        if step.inherit_env:
            env = {k: v for k, v in os.environ.items()
                   if not SENSITIVE_ENV_RE.search(k)
                   and (k.lower().endswith("_proxy") or not URL_CREDS_RE.search(v))}   # steps need the proxy
        else:
            env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ") if k in os.environ}
        env.update(step.env)
        env.update(step.env_inputs(self.values))
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "IL_RUN_ID": self.id, "IL_STEP_ID": step.id})
        if step.command:
            argv = step.argv(self.values, files=self.files)
            if argv[0] == AUTO_PYTHON:
                if not self.python:
                    raise FileNotFoundError(2, "No virtualenv for @python", "a virtualenv python")
                argv[0] = self.python
        elif IS_WINDOWS:
            argv = ["cmd.exe", "/d", "/c", step.shell]
        else:
            argv = ["/bin/bash", "-lc" if step.login_shell else "-c", step.shell]
        if IS_WINDOWS:
            return argv, env   # no stdbuf / ulimit there
        stdbuf = shutil.which("stdbuf", path=env.get("PATH"))
        if stdbuf:   # line-buffer C programs too, so output shows up as it happens
            argv = [stdbuf, "-oL", "-eL", *argv]
        if step.nofile:   # same as running `ulimit -n N` first; exec keeps the pid
            argv = ["/bin/sh", "-c",
                    'ulimit -n "$0" 2>/dev/null || echo "Could not raise the open-file limit to $0 '
                    '(this user\'s hard limit is $(ulimit -Hn)); carrying on with $(ulimit -n)." >&2; exec "$@"',
                    str(step.nofile), *argv]
        return argv, env

    def build_remote_argv(self) -> List[str]:
        step = self.step
        if step.command:
            return step.argv(self.values)
        return ["bash", "-lc" if step.login_shell else "-c", step.shell]

    def end(self, reason: str, stopped_by: Optional[str] = None, timed_out: bool = False):
        """Terminate the whole process group, then kill it if it ignores us."""
        if self.ending:
            return
        if self.process is None:   # asked before the command started: act on it right after
            self.pending_end = (reason, stopped_by, timed_out)
            return
        if self.process.poll() is not None:
            return
        self.ending, self.stopped_by, self.timed_out = True, stopped_by, timed_out
        self.log.add("error", reason)
        first = signal.SIGINT if self.step.stop_signal == "INT" else signal.SIGTERM
        self._signal(first)
        timer = threading.Timer(self.step.stop_grace, self.kill_now)
        timer.daemon = True
        timer.start()

    def kill_now(self, by: Optional[str] = None):
        """No more waiting: SIGKILL the whole group."""
        if self.killed or not self.process:
            return
        if not by and isinstance(self.process, _RemoteProcess) and self.process.poll() is not None:
            return   # ended within the grace period; its pid may belong to something else by now
        self.killed = True
        if by:
            self.log.add("error", f"Force-stopped by {by}.")
            if not self.ending:
                self.ending, self.stopped_by = True, by
        elif self.process.poll() is None:
            self.log.add("error", f"Still running {self.step.stop_grace}s after the stop request, so it was force-stopped.")
        self._signal(KILL)


class _Detached(Exception):
    pass


class _Done(Exception):
    pass


async def _run_task(run: _Run, adopt: Optional[Dict[str, Any]] = None, elapsed: float = 0.0):
    """Runs the command to completion, whoever is watching. Local steps run here; remote
    steps run on their server and this follows them (adopt = picking up a run that
    another portal server, or this one before a restart, was following)."""
    loop = asyncio.get_running_loop()
    log, step = run.log, run.step
    exited = loop.create_future()
    stop_wake = _wake(_STOP_WAKE, run.id)
    started = loop.time() - elapsed
    rc = -1
    finishing = False   # set once the command is done: the ticker ends after its current write
    detached = False
    forced_status: Optional[str] = None

    async def flush():
        try:
            asked, force = await _db(log.flush)
        except _NotOwner:
            if not run.taken_over:
                run.taken_over = True
                run.detach = True
                logger.warning("Ingestion run %s: another portal server took it over; letting go", run.id)
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("Ingestion run %s: couldn't write log, will retry: %s", run.id, e)
            return
        w = _LOG_WAKE.get(run.id)
        if w:
            w.fire()   # watchers on this server don't wait for the NOTIFY round trip
        if asked and not run.ending:
            logger.info("Ingestion run %s (%s) stopped by %s", run.id, step.id, asked)
            run.end(_stop_reason(step, asked), stopped_by=asked)
        if force and asked and not run.killed:
            run.kill_now(by=asked)

    async def ticker():
        await flush()   # header lines and first output go out at once
        last = loop.time()
        while not finishing:
            ev = stop_wake.ev
            try:
                await asyncio.wait_for(ev.wait(), 0 if log.backlog >= WRITE_BATCH // 2 else FLUSH)
                woke = True
            except asyncio.TimeoutError:
                woke = False
            now = loop.time()
            if step.timeout and not run.ending and now - started > step.timeout:
                run.end(f"Stopped: the step ran past its time limit of {_duration(step.timeout)}.", timed_out=True)
            if woke or log.pending or now - last >= HEARTBEAT:
                last = now
                await flush()

    def read_output(process: subprocess.Popen):
        fd = process.stdout.fileno()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf, quiet = "", 0.0
        try:
            while True:
                # Windows can't select() on a pipe: plain blocking reads until the pipe closes
                ready = True if IS_WINDOWS else select.select([fd], [], [], 0.25)[0]
                if ready:
                    chunk = os.read(fd, READ_CHUNK)
                    if not chunk:
                        break
                    quiet = 0.0
                    buf += decoder.decode(chunk)
                    *lines, buf = buf.split("\n")
                    for line in lines:
                        line = _clean(line)
                        if line:
                            log.add("output", line)
                    if len(buf) > MAX_LINE * 4:   # no newline in sight: don't let it grow forever
                        line = _clean(buf)
                        buf = ""
                        if line:
                            log.add("output", line)
                elif process.poll() is not None:
                    # The command is done; something it started may still hold the output open.
                    quiet += 0.25
                    if quiet >= DRAIN:
                        break
            buf += decoder.decode(b"", final=True)
            if buf:
                line = _clean(buf)
                if line:
                    log.add("output", line)
        except Exception as e:  # noqa: BLE001
            log.add("error", f"Lost the command's output: {type(e).__name__}: {e}")
        finally:
            try:
                process.stdout.close()
            except Exception:
                pass
        code = process.wait()
        loop.call_soon_threadsafe(lambda: exited.done() or exited.set_result(code))

    def follow(ssh: _Ssh, pid: int, spool: str, at: List[int], contact: List[float], final: bool = False,
               rdir: Optional[str] = None, snapshot: bool = False):
        """Read the server's output file from at[0], moving at[0] past every whole line handed
        to the log (so a dropped connection resumes exactly there). Live mode follows until the
        step's runner exits; raises if the connection drops. snapshot reads what is there now
        and leaves a half-written last line for next time (servers whose tail has no --pid)."""
        offset = at[0]
        cmd = (f"tail -c +{offset + 1} -- {_q(spool)} 2>/dev/null" if final or snapshot else
               f"tail -c +{offset + 1} -F -s 0.3 --pid={int(pid)} -- {_q(spool)} 2>/dev/null")
        chan = ssh.open(cmd)
        buf, pos = b"", offset
        idle, done_early = 0.0, False
        try:
            while True:
                if run.detach:
                    raise _Detached()
                if not select.select([chan], [], [], 0.25)[0]:
                    t = chan.get_transport()
                    if chan.closed or t is None or not t.is_active():
                        raise SshError("connection closed")
                    idle += 0.25
                    if not final and not snapshot and rdir and idle >= 5:
                        # tail --pid can't tell a finished-but-unreaped runner from a live one
                        idle = 0.0
                        _, out = ssh.run(f"test -f {_q(f'{rdir}/{run.id}.exit')} && echo done || true", timeout=20)
                        contact[0] = time.monotonic()
                        if "done" in out:
                            done_early = True
                            break
                    continue
                idle = 0.0
                data = chan.recv(READ_CHUNK)
                if not data:
                    break
                contact[0] = time.monotonic()
                buf += data
                while True:
                    i = buf.find(b"\n")
                    if i < 0:
                        break
                    line, buf = buf[:i], buf[i + 1:]
                    pos += i + 1
                    text = _clean(line.decode("utf-8", "replace"))
                    if text:
                        log.add("output", text, pos)
                    else:
                        log.seen_offset = pos
                    at[0] = pos
                if len(buf) > MAX_LINE * 4:
                    pos += len(buf)
                    text = _clean(buf.decode("utf-8", "replace"))
                    buf = b""
                    if text:
                        log.add("output", text, pos)
                    at[0] = pos
            if not final and not snapshot and not done_early:
                # EOF can arrive a moment before the exit status; a dead connection ends with none
                chan.recv_exit_status()
                t = chan.get_transport()
                if t is None or not t.is_active() or chan.exit_status == -1:
                    raise SshError("connection closed")
        finally:
            chan.close()
        if final and buf:
            pos += len(buf)
            text = _clean(buf.decode("utf-8", "replace"))
            if text:
                log.add("output", text, pos)
            at[0] = pos

    def drive_remote():
        """Blocking, on its own thread: launch (or pick up) the step on its server and follow
        it to the end, reconnecting as often as needed. Returns (outcome, exit code)."""
        srv = step.server
        ssh = _ssh_for(srv)
        contact = [time.monotonic()]
        outcome = ("failed", 1)
        spool = rdir = None
        try:
            if adopt:
                pid, spool, rdir = adopt["pid"], adopt["spool"], adopt["dir"]
                at = [adopt["offset"]]
                run.process = _RemoteProcess(ssh, pid, log)
                loop.call_soon_threadsafe(started_remote)
            else:
                argv = run.build_remote_argv()
                try:
                    code, out = ssh.run("bash -ls" if step.login_shell else "bash -s",
                                        stdin=_launch_script(run.id, step, run.values, argv), timeout=90)
                except SshError as e:
                    log.add("error", str(e))
                    return outcome
                except Exception as e:  # noqa: BLE001
                    log.add("error", f"Couldn't start the step on {srv.name}: {type(e).__name__}: {e}")
                    return outcome
                pid = None
                for line in out.splitlines():
                    if line.startswith("__IL_PID__ "):
                        pid = int(line.split()[1])
                    elif line.startswith("__IL_SPOOL__ "):
                        spool = line[len("__IL_SPOOL__ "):]
                    elif line.startswith("__IL_DIR__ "):
                        rdir = line[len("__IL_DIR__ "):]
                    elif line.startswith("__IL_PY__ "):
                        found = line[len("__IL_PY__ "):]
                        if found.split(" (", 1)[0] != run.python:   # the header already says which one
                            log.add("info", f"Python:   {found}")
                    elif line.strip():
                        log.add("output" if pid else "error", _clean(line))
                if not (pid and spool and rdir):
                    outcome = ("failed", code or 1)
                    return outcome
                at = [0]
                for attempt in range(10):   # whoever picks this run up later needs these
                    try:
                        with db.transaction() as c:
                            c.execute("UPDATE ingestion_runs SET pid = ?, remote_spool = ?, remote_dir = ? WHERE id = ?",
                                      (pid, spool, rdir, run.id))
                        break
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Ingestion run %s: couldn't save remote pid (%s); retrying", run.id, e)
                        time.sleep(1 + attempt)
                run.process = _RemoteProcess(ssh, pid, log)
                loop.call_soon_threadsafe(started_remote)
            outage, backoff = False, 2.0
            polling = False   # this server's tail can't follow a pid: read the file once a second instead
            while True:
                if run.detach:
                    outcome = ("detached", None)
                    return outcome
                try:
                    began = time.monotonic()
                    follow(ssh, pid, spool, at, contact, rdir=rdir, snapshot=polling)
                    code, out = ssh.run(_status_command(pid, rdir, run.id), timeout=30)
                    contact[0] = time.monotonic()
                    if outage:
                        log.add("info", f"Reconnected to {srv.name}.")
                        outage, backoff = False, 2.0
                    if "__IL_EXIT__" in out or "__IL_GONE__" in out:
                        follow(ssh, pid, spool, at, contact, final=True)
                        if "__IL_EXIT__" in out:
                            word = out.split("__IL_EXIT__", 1)[1].split()
                            outcome = ("exited", int(word[0]) if word and word[0].lstrip("-").isdigit() else -1)
                        else:
                            outcome = ("gone", None)
                        return outcome
                    if time.monotonic() - began < 1.0:
                        # following stopped straight away (a tail without --pid): poll, at a steady pace
                        polling = True
                        waited = 0.0
                        while waited < 1.0 and not run.detach:
                            time.sleep(0.25)
                            waited += 0.25
                except _Detached:
                    outcome = ("detached", None)
                    return outcome
                except Exception as e:  # noqa: BLE001
                    if run.detach:
                        outcome = ("detached", None)
                        return outcome
                    if not outage:
                        outage = True
                        log.add("error", f"Lost the connection to {srv.name} ({e}). The step keeps running there; reconnecting…")
                    if time.monotonic() - contact[0] > GIVE_UP:
                        outcome = ("unreachable", None)
                        return outcome
                    waited = 0.0
                    while waited < backoff and not run.detach:
                        time.sleep(0.25)
                        waited += 0.25
                    backoff = min(backoff * 2, RECONNECT_MAX)
        finally:
            if isinstance(run.process, _RemoteProcess) and outcome[0] in ("exited", "gone"):
                run.process.returncode = outcome[1] if outcome[0] == "exited" else -1
            if outcome[0] in ("exited", "gone") and rdir:
                junk = [f"{rdir}/{run.id}.exit"] + ([] if step.log_file or not spool else [spool])
                junk += [f"{rdir}/{run.id}-{s.name}.json" for s in step.inputs if s.type == "profile"]
                try:
                    ssh.run("rm -f -- " + " ".join(_q(j) for j in junk), timeout=20)
                except Exception:  # noqa: BLE001
                    pass
            loop.call_soon_threadsafe(lambda: exited.done() or exited.set_result(outcome))

    def started_remote():
        if run.pending_end:
            run.end(*run.pending_end)

    tick = None
    cancelled = False
    wrote_pid = None
    try:
        if run.remote:
            tick = asyncio.ensure_future(ticker())
            threading.Thread(target=drive_remote, name=f"ingestion-{run.id[:8]}", daemon=True).start()
            kind, code = await exited
            srv_name = step.server.name
            if kind == "detached":
                detached = True
                raise _Detached()
            if kind == "exited":
                rc = code
            else:
                rc = -9 if run.killed else -1
                if kind == "gone" and not (run.stopped_by or run.timed_out):   # we killed it: not lost
                    forced_status = "lost"
                    log.add("error", f"The step's process on {srv_name} ended without leaving an exit code "
                                     "(stopped from outside the portal, or the server restarted).")
                elif kind == "unreachable":
                    forced_status = "lost"
                    log.add("error", f"Couldn't reach {srv_name} for {GIVE_UP // 60} minutes, so the portal stopped "
                                     "following this run. It may still be running there.")
                elif kind == "failed":
                    rc = code if code is not None else 1
                    forced_status = "failed"
            log.add("separator")
            took = _duration(loop.time() - started)
            if forced_status == "lost":
                pass
            elif forced_status == "failed":
                log.add("error", f"{step.name} couldn't be started on {srv_name}.")
            elif run.stopped_by and run.killed:
                log.add("error", f"{step.name} force-stopped after {took}.")
            elif run.stopped_by:
                log.add("error", f"{step.name} stopped by {run.stopped_by} after {took}.")
            elif run.timed_out:
                log.add("error", f"{step.name} timed out after {took}.")
            elif rc == 0:
                log.add("success", f"{step.name} finished successfully on {srv_name} in {took}.")
            else:
                log.add("error", f"{step.name} failed with exit code {rc} after {took}.")
            raise _Done()
        if step.log_file:
            try:
                os.makedirs(os.path.dirname(step.log_file), exist_ok=True)
                log.file = open(step.log_file, "w", encoding="utf-8", buffering=1 << 16)
                log.add("info", f"Copy of this output: {step.log_file}")
            except OSError as e:
                log.add("error", f"Couldn't open {step.log_file} for a copy of the output ({e.strerror}); carrying on without it.")
        for name, path in run.files.items():   # the profiles built on the page, readable only by this user
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(run.values[name])
        argv, env = run.build()
        popen_kw = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS
                    else {"start_new_session": True})
        run.process = subprocess.Popen(argv, cwd=step.workdir, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **popen_kw)
        tick = asyncio.ensure_future(ticker())
        if step.pidfile:
            try:
                _write_pidfile(step.pidfile, run.process.pid)
                wrote_pid = run.process.pid
            except OSError as e:
                log.add("error", f"Couldn't write {step.pidfile} ({e.strerror}); scripts that check it won't see this run.")
        threading.Thread(target=read_output, args=(run.process,), name=f"ingestion-{run.id[:8]}", daemon=True).start()
        if run.pending_end:
            run.end(*run.pending_end)
        try:
            await _db(lambda c: c.execute("UPDATE ingestion_runs SET pid = ? WHERE id = ?", (run.process.pid, run.id)))
        except Exception:  # noqa: BLE001 - only informational
            pass
        rc = await exited
        log.add("separator")
        took = _duration(loop.time() - started)
        if run.server_stopping:
            log.add("error", f"{step.name} stopped after {took} because the portal server restarted.")
        elif run.stopped_by and run.killed:
            log.add("error", f"{step.name} force-stopped after {took}.")
        elif run.stopped_by:
            log.add("error", f"{step.name} stopped by {run.stopped_by} after {took}.")
        elif run.timed_out:
            log.add("error", f"{step.name} timed out after {took}.")
        elif rc == 0:
            log.add("success", f"{step.name} finished successfully in {took}.")
        else:
            log.add("error", f"{step.name} failed with exit code {rc} after {took}.")
    except _Done:
        pass
    except _Detached:
        detached = True
    except asyncio.CancelledError:
        if run.remote:
            # Portal going down: a remote step carries on and gets picked up again.
            cancelled = detached = True
            run.detach = True
            raise
        # The server is going away under us: don't leave the command running untracked.
        cancelled = True
        run.log.release()
        msg = "The portal server stopped, so this step was stopped. Start it again when ready."
        if run.server_stopping:
            pass   # shutdown() already ended it and said why
        elif run.process and run.process.poll() is None and not run.ending:
            run.end(msg)   # TERM the group now, KILL it after the grace period
        else:
            log.add("error", msg)
    except FileNotFoundError as e:
        log.add("error", f"Couldn't start the command: {e.filename or e} was not found on the server.")
    except PermissionError as e:
        log.add("error", f"Couldn't start the command: permission denied ({e.filename or e}).")
    except Exception as e:  # noqa: BLE001
        logger.exception("Ingestion run %s crashed", run.id)
        log.add("error", f"Error running the step: {type(e).__name__}: {e}")
        if run.process and run.process.poll() is None:
            _signal_group(run.process, KILL)
    finally:
        # Let a write that's in flight land first: two overlapping writes could commit
        # out of order and a live viewer would skip the earlier lines.
        finishing = True
        stop_wake.fire()
        if tick:
            try:
                await asyncio.wait_for(asyncio.shield(tick), 30)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                tick.cancel()
        _ACTIVE.pop(run.id, None)
        _STOP_WAKE.pop(run.id, None)
        if detached or run.taken_over:
            if run.taken_over:
                return
            # Let go: mark it stale right away so the next portal server to look takes it over.
            def release(conn):
                log.flush(conn)
                conn.execute("""UPDATE ingestion_runs SET heartbeat_at = now() - interval '1 day'
                                WHERE id = ? AND finished_at IS NULL AND owner = ?""", (run.id, OWNER))
            try:
                await _db(release)
            except BaseException as e:  # noqa: BLE001
                logger.warning("Ingestion run %s: couldn't hand the run over (%s); it'll be picked up when it goes stale", run.id, e)
            logger.info("Ingestion run %s (%s) on %s: let go; it keeps running there", run.id, step.id, step.server.id)
            return
        if wrote_pid:
            _clear_pidfile(step.pidfile, wrote_pid)
        if not run.remote:
            for path in run.files.values():
                try:
                    os.remove(path)
                except OSError:
                    pass
        log.close_file()
        status = forced_status or ("lost" if cancelled or run.server_stopping else "stopped" if run.stopped_by
                                   else "timed_out" if run.timed_out else "success" if rc == 0 else "failed")

        def close(conn):
            while log.backlog > WRITE_BATCH:
                log.flush(conn)
            conn.execute("""UPDATE ingestion_runs SET status = ?, returncode = ?, finished_at = COALESCE(finished_at, now())
                            WHERE id = ? AND (owner = ? OR owner IS NULL)""", (status, rc, run.id, OWNER))
            log.flush(conn)   # last lines in the same commit as the final status
            # always wake watchers on other servers, even when every line was already written
            conn.execute("SELECT pg_notify('ingestion_log', ?)", (run.id,))
        for attempt in range(120):   # about ten minutes, retried often so the real status lands soon after an outage
            try:
                await _db(close)
                break
            except _NotOwner:
                break
            except Exception as e:  # noqa: BLE001
                if attempt % 12 == 0:
                    logger.warning("Ingestion run %s: couldn't save final status (%s); retrying", run.id, e)
                await asyncio.sleep(min(2 * (attempt + 1), 5))
        w = _LOG_WAKE.get(run.id)
        if w:
            w.fire()
        logger.info("Ingestion run %s (%s) finished: %s (exit %s)", run.id, step.id, status, rc)
        _audit("ingestion_run_finished", run.email, step.id,
               {"run_id": run.id, "server": step.server.id, "status": status, "returncode": rc,
                "seconds": int(loop.time() - started), "stopped_by": run.stopped_by})
        if cancelled:
            raise asyncio.CancelledError


def _stop_reason(step: Step, who: str) -> str:
    if step.stop_signal == "INT":
        return (f"Stop requested by {who}. Sent Ctrl-C so in-flight work can finish; "
                f"it's forced after {_duration(step.stop_grace)} if still running.")
    return f"Stopped by {who}."


def _pid_alive_windows(pid: int) -> bool:
    # os.kill(pid, 0) would terminate the process on Windows, so ask the OS instead
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5   # access denied: exists, someone else's
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259   # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int, program: Optional[str]) -> bool:
    """True if pid is a live process (and, when we know the program, actually that program,
    so a recycled pid left in a stale pidfile doesn't count)."""
    if IS_WINDOWS:
        try:
            return _pid_alive_windows(pid)
        except (OSError, AttributeError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass   # someone else's process, but alive
    try:
        with open(f"/proc/{pid}/stat") as f:
            if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                return False
        if program:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
            return os.path.basename(program) in cmd
    except (OSError, IndexError):
        pass
    return True


def _find_venv(folder: str) -> Tuple[Optional[str], List[str]]:
    """For @python on this server: (the virtualenv's python or None, every path looked at).
    Looks for .venv, venv and env in the run folder and up to 2 folders above it. No fallback:
    a step must never run on some other python by accident."""
    exe = ("Scripts", "python.exe") if IS_WINDOWS else ("bin", "python")
    d = os.path.realpath(folder)
    looked = []
    for _ in range(VENV_LEVELS):
        for name in VENV_DIRS:
            path = os.path.join(d, name, *exe)   # not realpath'd: a venv python must keep its own path
            looked.append(path)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path, looked
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None, looked


def _probe_sh(step: Step, discover: bool) -> str:
    """bash run in a step's folder: the virtualenv lookup @python does, and with discover, the
    Python 3 interpreters on the server a new virtualenv could be made with."""
    out = f"""set -u
cd -- {_q(step.workdir)} 2>/dev/null || {{ printf "__IL_ERR__ %s doesn't exist on $(hostname), or $(id -un) can't open it.\\n" {_q(step.workdir)}; exit 0; }}
IL_ERR_TAG="__IL_ERR__ "
{_root_check_sh(step, fail="exit 0") if step.folder_choice else ""}
d=$(pwd -P); n=0
while [ $n -lt {VENV_LEVELS} ]; do
  for v in {" ".join(VENV_DIRS)}; do
    echo "__IL_LOOK__ $d/$v/bin/python"
    if [ -x "$d/$v/bin/python" ]; then echo "__IL_VENV__ $d/$v/bin/python"; break 2; fi
  done
  [ "$d" = "/" ] && break; d=$(dirname -- "$d"); n=$((n+1))
done
"""
    if discover:
        out += r"""[ -f requirements.txt ] && echo __IL_REQ__
T=""; command -v timeout >/dev/null 2>&1 && T="timeout 10"
seen=" "
for dir in $(printf %s "$PATH" | tr ':' ' ') /usr/bin /usr/local/bin /opt/*/bin /opt/*/*/bin; do
  for p in "$dir"/python3 "$dir"/python3.[0-9]*; do
    case "$p" in *-config|*-dbg) continue;; esac
    [ -f "$p" ] && [ -x "$p" ] || continue
    r=$(readlink -f -- "$p" 2>/dev/null || printf %s "$p")
    case "$seen" in *" $r "*) continue;; esac
    seen="$seen$r "
    v=$($T "$p" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null) || continue
    if $T "$p" -c 'import venv, ensurepip' >/dev/null 2>&1; then ok=1; else ok=0; fi
    echo "__IL_PYTHON__ $ok $v $p"
  done
done
"""
    return out


def _parse_probe(out: str) -> Dict[str, Any]:
    info: Dict[str, Any] = {"venv": None, "looked": [], "requirements": False, "pythons": []}
    for line in out.splitlines():
        if line.startswith("__IL_ERR__ "):
            raise HTTPException(status_code=422, detail=line[len("__IL_ERR__ "):])
        tag, _, rest = line.partition(" ")
        if tag == "__IL_LOOK__":
            info["looked"].append(rest)
        elif tag == "__IL_VENV__":
            info["venv"] = rest
        elif tag == "__IL_REQ__":
            info["requirements"] = True
        elif tag == "__IL_PYTHON__":
            ok, version, path = rest.split(" ", 2)
            info["pythons"].append({"path": path, "version": version, "can_create": ok == "1"})
    def key(p):
        return tuple(int(x) for x in p["version"].split(".") if x.isdigit())
    info["pythons"].sort(key=key, reverse=True)
    return info


async def _probe(step: Step, discover: bool) -> Dict[str, Any]:
    """Run _probe_sh on the step's server (this one, or over SSH)."""
    script = _probe_sh(step, discover)
    srv = step.server
    if srv.local:
        if IS_WINDOWS:
            venv, looked = _find_venv(step.workdir)
            return {"venv": venv, "looked": looked, "requirements": False, "pythons": []}

        def local():
            return subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True,
                                  timeout=120, cwd="/").stdout
        return _parse_probe(await asyncio.to_thread(local))

    def remote():
        try:
            return _ssh_for(srv).run("bash -s", stdin=script, timeout=120)[1]
        except SshError as e:
            raise HTTPException(status_code=502, detail=str(e))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Couldn't reach {srv.name}: {e}")
    return _parse_probe(await asyncio.to_thread(remote))


def _no_venv(step: Step, looked: List[str]) -> HTTPException:
    return HTTPException(status_code=409, detail={
        "code": "no_venv", "folder": step.workdir, "looked": looked,
        "message": f"No virtualenv for {step.workdir} on {step.server.name}, so {step.name} wasn't started. "
                   f"Looked for: {', '.join(looked)}."})


VENV_STEP_ID = "venv-setup"   # a built-in step: make a virtualenv in a folder (see POST /venv)
VENV_SCRIPT = r"""set -e
echo "Creating $(pwd)/venv with $IL_PYTHON ($("$IL_PYTHON" --version 2>&1))"
"$IL_PYTHON" -m venv venv
venv/bin/python -m pip install --upgrade pip || echo "Couldn't upgrade pip (no access to the package index?); carrying on with the bundled one."
if [ -n "${IL_REQUIREMENTS:-}" ]; then venv/bin/python -m pip install -r requirements.txt; fi
echo "Virtualenv ready: $(pwd)/venv/bin/python"
"""


def _venv_step(folder: str, srv: Server) -> Step:
    step = Step({"id": VENV_STEP_ID, "name": "Create virtualenv", "workdir": folder, "shell": VENV_SCRIPT,
                 "inputs": [{"name": "python", "max_length": 1024}, {"name": "requirements", "type": "flag"}],
                 "timeout_minutes": 60, "folder_roots": [folder]}, 0)
    step.server = srv
    return step


REQS_STEP_ID = "venv-requirements"   # built-in: pip install the run folder's requirements.txt (POST /requirements)
REQS_SCRIPT = r"""set -e
echo "Installing $(pwd)/requirements.txt into $IL_PYTHON"
"$IL_PYTHON" -m pip install -r requirements.txt
echo "Requirements installed."
"""


def _reqs_step(folder: str, srv: Server) -> Step:
    step = Step({"id": REQS_STEP_ID, "name": "Install requirements", "workdir": folder, "shell": REQS_SCRIPT,
                 "inputs": [{"name": "python", "max_length": 1024}],
                 "timeout_minutes": 60, "folder_roots": [folder]}, 0)
    step.server = srv
    return step


BUILTIN_STEPS = {VENV_STEP_ID: _venv_step, REQS_STEP_ID: _reqs_step}


def _program(step: Step) -> Optional[str]:
    """What the step's process shows up as, for pidfile checks."""
    if not step.command:
        return None
    return "python" if step.command[0] == AUTO_PYTHON else step.command[0]


def _pidfile_owner(step: Step) -> Optional[int]:
    """The pid in the step's pidfile, if that process is still running."""
    if not step.pidfile:
        return None
    try:
        with open(step.pidfile) as f:
            pid = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    return pid if _pid_alive(pid, _program(step)) else None


def _write_pidfile(path: str, pid: int):
    tmp = f"{path}.{pid}.tmp"
    with open(tmp, "w") as f:
        f.write(f"{pid}\n")
    os.replace(tmp, path)


def _clear_pidfile(path: str, pid: int):
    try:
        with open(path) as f:
            if int(f.read().strip() or 0) != pid:
                return   # someone else's now
        os.remove(path)
    except (OSError, ValueError):
        pass


def _duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


# ============================================================================
# Watching a run
# ============================================================================

def _sse(evt: dict) -> str:
    return f"data: {json.dumps(evt, default=str, ensure_ascii=False)}\n\n"


def _sse_response(gen):
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})


async def _watch(run_id: str, after: int):
    _start_listener()
    wake = _wake(_LOG_WAKE, run_id)
    wake.waiters += 1
    last, quiet, stopping = after, 0.0, (False, False)
    try:
        while True:
            ev = wake.ev

            def read(conn):
                # Run state first, lines second: each statement sees its own snapshot, so
                # reading lines last guarantees that a run seen as finished comes with all its lines.
                run = conn.execute(f"""SELECT status, returncode, host, finished_at, stop_requested_by, log_lines,
                                              force_stop_requested, remote,
                                              finished_at IS NULL AND heartbeat_at < now() - interval '{STALE}'
                                                AND NOT {_FOLLOWABLE} AS stale
                                       FROM ingestion_runs WHERE id = ?""", (run_id,)).fetchone()
                rows = conn.execute("""SELECT seq, ts, type, message FROM ingestion_run_logs
                                       WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?""",
                                    (run_id, last, STREAM_BATCH)).fetchall()
                return rows, run
            try:
                rows, run = await _db(read, read_only=True)
                if run and run["stale"]:
                    await _db(_reap)
                    continue
            except Exception as e:  # noqa: BLE001
                yield _sse({"type": "error", "message": f"Lost connection to the run log: {e}"})
                yield _sse({"type": "done", "status": "unknown", "returncode": -1, "reconnect": True})
                return
            if run is None:
                yield _sse({"type": "error", "message": "Run not found."})
                yield _sse({"type": "done", "status": "unknown", "returncode": -1})
                return
            if rows:
                last = rows[-1]["seq"]
                quiet = 0.0
                yield "".join(_sse({"seq": r["seq"], "type": r["type"], "message": r["message"], "ts": r["ts"]})
                              for r in rows)
                if len(rows) == STREAM_BATCH:
                    continue
            if run["finished_at"]:
                if run["status"] == "lost" and not run["remote"]:   # remote runs say why in their log
                    yield _sse({"type": "error", "message": f"The server running this step ({run['host']}) stopped responding."})
                yield _sse({"type": "done", "status": run["status"],
                            "returncode": run["returncode"] if run["returncode"] is not None else -1,
                            "finished_at": run["finished_at"]})
                return
            state = (bool(run["stop_requested_by"]), bool(run["force_stop_requested"]))
            if state != stopping:
                stopping = state
                yield _sse({"type": "state", "stopping": state[0], "forcing": state[1], "by": run["stop_requested_by"]})
            try:
                await asyncio.wait_for(ev.wait(), FALLBACK)
            except asyncio.TimeoutError:
                quiet += FALLBACK
                if quiet >= PING:
                    quiet = 0.0
                    yield ": ping\n\n"
    finally:
        wake.waiters -= 1
        if wake.waiters <= 0 and _LOG_WAKE.get(run_id) is wake:
            _LOG_WAKE.pop(run_id, None)


# ============================================================================
# Endpoints
# ============================================================================

_RUN_COLUMNS = f"""id, step_id, step_name, inputs, command, workdir, started_by, started_by_email, started_at,
    finished_at, returncode, stop_requested_by, force_stop_requested, host, log_lines, log_truncated,
    server_id, server_name, remote, pid,
    CASE WHEN finished_at IS NULL AND heartbeat_at < now() - interval '{STALE}' AND NOT {_FOLLOWABLE}
         THEN 'lost' ELSE status END AS status,
    (finished_at IS NULL AND heartbeat_at < now() - interval '{STALE}' AND {_FOLLOWABLE}) AS reconnecting,
    EXTRACT(EPOCH FROM (COALESCE(finished_at, now()) - started_at))::int AS seconds"""


def _run_out(r) -> Dict[str, Any]:
    out = dict(r)
    try:
        out["inputs"] = json.loads(out.get("inputs") or "{}")
    except (TypeError, ValueError):
        out["inputs"] = {}
    out["stopping"] = bool(out.get("stop_requested_by")) and not out.get("finished_at")
    return out


def _check_run_id(run_id: str) -> str:
    if not RUN_ID_RE.match(run_id or ""):
        raise HTTPException(status_code=404, detail="Run not found")
    return run_id


@ingestion_router.get("/steps")
async def list_steps(auth: dict = Depends(require_permission(VIEW_PERM))):
    _start_background()
    settings, steps, errors, configured = await asyncio.to_thread(_cfg.snapshot)
    servers = _cfg.servers_snapshot()
    return {
        "configured": configured,
        "servers": [srv.public() for srv in servers.values()],
        "steps": [g.public(servers) for g in steps.values()],
        "errors": errors if auth["permissions"].get(RUN_PERM) else [],
        "can_run": bool(auth["permissions"].get(RUN_PERM)),
        "max_concurrent_runs": settings["max_concurrent_runs"],
        "config_file": _cfg_name(),
    }


@ingestion_router.get("/active")
async def active_runs(auth: dict = Depends(require_permission(VIEW_PERM))):
    """Runs in progress right now, on any server."""
    _start_background()
    def read(conn):
        _reap(conn)
        return conn.execute(f"SELECT {_RUN_COLUMNS} FROM ingestion_runs WHERE finished_at IS NULL ORDER BY started_at DESC").fetchall()
    return [_run_out(r) for r in await _db(read)]


@ingestion_router.get("/runs")
async def list_runs(
    step_id: Optional[str] = Query(None),
    server_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    mine: bool = Query(False),
    before: Optional[str] = Query(None, description="started_at of the last run on the previous page"),
    limit: int = Query(25, ge=1, le=200),
    auth: dict = Depends(require_permission(VIEW_PERM)),
):
    """Run history, newest first. Page with ?before=<started_at of the last row>."""
    where, params = [], []
    if step_id:
        where.append("step_id = ?")
        params.append(step_id)
    if server_id:
        where.append("server_id = ?")
        params.append(server_id)
    if status:
        if status not in ("running", *FINAL):
            raise HTTPException(status_code=400, detail="Unknown status")
        if status == "running":
            where.append(f"finished_at IS NULL AND (heartbeat_at >= now() - interval '{STALE}' OR {_FOLLOWABLE})")
        elif status == "lost":
            where.append(f"(status = 'lost' OR (finished_at IS NULL AND heartbeat_at < now() - interval '{STALE}' AND NOT {_FOLLOWABLE}))")
        else:
            where.append("status = ?")
            params.append(status)
    if mine:
        if _email(auth):
            where.append("lower(started_by_email) = lower(?)")
            params.append(_email(auth))
        else:
            where.append("false")
    if before:
        try:
            cursor = datetime.fromisoformat(before)
        except ValueError:
            raise HTTPException(status_code=400, detail="before must be an ISO timestamp")
        where.append("started_at < ?")
        params.append(cursor)
    sql = f"SELECT {_RUN_COLUMNS} FROM ingestion_runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit + 1)
    rows = await _db(lambda c: c.execute(sql, params).fetchall(), read_only=True)
    runs = [_run_out(r) for r in rows[:limit]]
    return {"runs": runs, "next": runs[-1]["started_at"] if len(rows) > limit else None}


@ingestion_router.get("/runs/{run_id}")
async def get_run(run_id: str, auth: dict = Depends(require_permission(VIEW_PERM))):
    _check_run_id(run_id)
    row = await _db(lambda c: c.execute(f"SELECT {_RUN_COLUMNS} FROM ingestion_runs WHERE id = ?", (run_id,)).fetchone(),
                    read_only=True)
    if row is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _run_out(row)


def _step_for(steps: Dict[str, StepGroup], step_id: Any, server_id: Any) -> Step:
    group = steps.get(str(step_id or ""))
    if group is None:
        raise HTTPException(status_code=404, detail="That step isn't configured.")
    server_id = str(server_id or "")
    if not server_id and len(group.variants) == 1:
        server_id = next(iter(group.variants))
    step = group.variants.get(server_id)
    if step is None:
        raise HTTPException(status_code=404, detail=f"{group.base.name} isn't set up to run on that server.")
    return step


def _in_folder(step: Step, chosen: Any, problems: Dict[str, str]) -> Step:
    """The step moved to the folder someone picked, if it allows that (problems['workdir'] if not)."""
    if chosen in (None, "", step.workdir):
        if not step.workdir:
            problems["workdir"] = "Pick the folder to run in"
        return step
    if not step.folder_choice:
        problems["workdir"] = "This step always runs in its own folder"
        return step
    try:
        folder = step.clean_folder(chosen)
        if step.server.local:
            real = os.path.realpath(folder)
            if not any(_under(real, os.path.realpath(r)) for r in step.folder_roots):
                raise ValueError(f"That folder is really {real}, which is outside the allowed folders")
        return step.with_workdir(folder)
    except ValueError as e:
        problems["workdir"] = str(e)
        return step


@ingestion_router.post("/runs")
async def start_run(
    body: Dict[str, Any] = Body(...),
    auth: dict = Depends(require_permission(VIEW_PERM, RUN_PERM)),
):
    """Start a step: {"step_id": "...", "server_id": "...", "inputs": {...}}. Returns {"run_id"};
    watch it on /runs/{id}/stream."""
    _start_background()
    settings, steps, _, _ = await asyncio.to_thread(_cfg.snapshot)
    step = _step_for(steps, body.get("step_id"), body.get("server_id"))
    srv = step.server
    values, problems = step.resolve(body.get("inputs") or {})
    step = _in_folder(step, body.get("workdir"), problems)
    if problems:
        raise HTTPException(status_code=422, detail={"message": "Some inputs need fixing.", "fields": problems})
    if srv.local and not os.path.isdir(step.workdir):
        raise HTTPException(status_code=409, detail=f"The working folder {step.workdir} doesn't exist on {srv.name}.")
    outside = _pidfile_owner(step) if srv.local else None
    if outside and not any(r.process and r.process.pid == outside for r in _ACTIVE.values()):
        raise HTTPException(status_code=409, detail={
            "message": f"{step.name} is already running outside the portal (PID {outside}, from {step.pidfile}). "
                       "Stop that one first, or wait for it to finish."})
    if _signature(step):
        try:
            manual = (await _outside(srv, steps, fresh=True) or {}).get(step.id)
        except Exception:  # noqa: BLE001 - can't look: the launch itself reports an unreachable server
            manual = None
        if manual:
            m = manual[0]
            raise HTTPException(status_code=409, detail={
                "code": "outside", "message": f"{step.name} is already running outside the portal on {srv.name} "
                f"(PID {m['pid']}, started by {m['user']}, in {m['folder']}). Stop that one first, or wait for it to finish."})
    python = None
    if step.command and step.command[0] == AUTO_PYTHON:
        if srv.local:
            python, looked = await asyncio.to_thread(_find_venv, step.workdir)
        else:
            found = await _probe(step, discover=False)
            python, looked = found["venv"], found["looked"]
        if not python:
            raise _no_venv(step, looked)
    lock_key = None
    if step.exclusive:
        lock_key = step.id if step.lock_everywhere else f"{step.id}@{srv.id}"
    return await _begin(step, values, auth, settings, lock_key, python)


async def _begin(step: Step, values: Dict[str, str], auth: dict, settings: Dict[str, Any],
                 lock_key: Optional[str], python: Optional[str] = None,
                 command: Optional[str] = None) -> Dict[str, str]:
    """Claim a slot for a checked step and start it."""
    srv = step.server
    run_id = uuid.uuid4().hex
    started_by, email = _user(auth), _email(auth)
    masked = step.masked_inputs(values)
    command = command or step.display(values)
    if python:
        command = shlex.join([python] + step.argv(values, masked=True)[1:])
    log = _Log(run_id, step.max_log_lines or settings["max_log_lines"], step.log_keep)
    log.secrets = sorted({v for k, v in values.items() if step.by_name[k].type == "secret" and len(v) >= 4},
                         key=len, reverse=True)
    run = _Run(run_id, step, values, log)
    run.python = python
    run.email = email
    names = [s.name for s in step.inputs if s.type == "profile" and values.get(s.name)]
    if names and step.command:   # a shell step only gets it as $IL_<NAME>
        # beside the run's other files in the spool folder, never in the repo
        run.files = {n: (os.path.join(os.path.expanduser("~"), srv.spool_dir, f"{run_id}-{n}.json") if srv.local
                         else f"~/{srv.spool_dir}/{run_id}-{n}.json") for n in names}
        command = shlex.join([python or step.argv(values)[0]] + step.argv(values, masked=True, files=run.files)[1:])
        if not srv.local:
            run.files = {}   # the launch script writes them on that server
    _ACTIVE[run_id] = run   # holds the slot while the database claim is in flight

    limit = settings["max_concurrent_runs"]

    def claim(conn):
        _reap(conn)
        # Runs per server are counted under a lock, so two portal processes can't both take the last slot.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (f"ingestion-lab:{srv.id}",))
        try:
            conn.execute("SAVEPOINT claim")
            conn.execute("""INSERT INTO ingestion_runs (id, step_id, step_name, inputs, command, workdir, lock_key,
                                                        started_by, started_by_email, host, server_id, server_name, remote, owner)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (run_id, step.id, step.name, json.dumps(masked), command, step.workdir,
                          lock_key, started_by, email, socket.gethostname(), srv.id, srv.name, not srv.local, OWNER))
            running = conn.execute(f"""SELECT count(*) AS n FROM ingestion_runs
                                       WHERE server_id = ? AND finished_at IS NULL
                                         AND (heartbeat_at >= now() - interval '{STALE}' OR {_FOLLOWABLE})""",
                                   (srv.id,)).fetchone()["n"]
            if running > limit:
                conn.execute("ROLLBACK TO SAVEPOINT claim")
                return {"busy": running - 1}
            return None
        except psycopg2.errors.UniqueViolation:
            conn.execute("ROLLBACK TO SAVEPOINT claim")
            row = conn.execute("""SELECT id, started_by, server_name FROM ingestion_runs
                                  WHERE lock_key = ? AND finished_at IS NULL""", (lock_key,)).fetchone()
            return dict(row) if row else {"id": None, "started_by": "someone", "server_name": srv.name}

    try:
        existing = await _db(claim)
    except BaseException:
        _ACTIVE.pop(run_id, None)
        raise
    if existing and "busy" in existing:
        _ACTIVE.pop(run_id, None)
        raise HTTPException(status_code=429, detail=f"{srv.name} is already running {existing['busy']} steps "
                                                    f"(the limit is {limit}). Try again when one finishes.")
    if existing:
        _ACTIVE.pop(run_id, None)
        raise HTTPException(status_code=409, detail={
            "message": f"{step.name} is already running on {existing.get('server_name') or srv.name} "
                       f"(started by {existing['started_by']}).",
            "run_id": existing["id"]})

    _start_listener()
    log.add("status", f"{step.name} started by {started_by}")
    log.add("info", f"Server:   {srv.name}" + ("" if srv.local else f" ({srv.username}@{srv.host})"))
    log.add("info", f"Folder:   {step.workdir}")
    log.add("info", f"Command:  {command}")
    if python:
        log.add("info", f"Python:   {python} (virtualenv)")
    shown = {k: v for k, v in masked.items() if v not in ("", False) and step.by_name[k].type != "profile"}
    if shown:
        log.add("info", "Inputs:   " + ", ".join(f"{k}={v}" for k, v in shown.items()))
    for k, v in masked.items():
        if v and step.by_name[k].type == "profile":   # on a line of its own: it's long
            log.add("info", f"Profile:  {v}")
    log.add("separator")
    task = asyncio.ensure_future(_run_task(run))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    _audit("ingestion_run_started", email, step.id, {"run_id": run_id, "server": srv.id, "inputs": masked})
    logger.info("Ingestion run %s (%s on %s) started by %s: %s", run_id, step.id, srv.id, email, command)
    return {"run_id": run_id}


@ingestion_router.get("/templates")
async def profile_templates(
    step_id: str = Query(...),
    server_id: str = Query(...),
    workdir: Optional[str] = Query(None, description="The folder picked on the page; the step's own when left out"),
    auth: dict = Depends(require_permission(VIEW_PERM, RUN_PERM)),
):
    """Profile files a profile input can start from, read from the run folder:
    {input name: [{"path", "profile"} or {"path", "error"}]}."""
    _, steps, _, _ = await asyncio.to_thread(_cfg.snapshot)
    step = _step_for(steps, step_id, server_id)
    problems: Dict[str, str] = {}
    step = _in_folder(step, workdir, problems)
    if problems:
        raise HTTPException(status_code=422, detail=problems["workdir"])
    specs = [s for s in step.inputs if s.templates]
    if not specs:
        return {}
    found: Dict[str, List[Tuple[str, bytes]]] = {s.name: [] for s in specs}
    if step.server.local:
        def scan():
            real = os.path.realpath(step.workdir)
            if step.folder_choice and not any(_under(real, os.path.realpath(r)) for r in step.folder_roots):
                raise HTTPException(status_code=422, detail=f"That folder is really {real}, which is outside the allowed folders.")
            for s in specs:
                for f in sorted(globmod.glob(os.path.join(step.workdir, s.templates)))[:MAX_TEMPLATES]:
                    if os.path.isfile(f) and os.path.getsize(f) <= PROFILE_MAX:
                        with open(f, "rb") as fh:
                            found[s.name].append((os.path.relpath(f, step.workdir).replace(os.sep, "/"), fh.read()))
        await asyncio.to_thread(scan)
    else:
        lines = "\n".join(
            f'for f in {s.templates}; do [ -f "$f" ] && [ "$(wc -c < "$f")" -le {PROFILE_MAX} ] && '
            f'{{ echo "__IL_F__ {s.name} $f"; base64 < "$f" | tr -d "\\n"; echo; }}; done' for s in specs)
        script = f"""set -u
cd -- {_q(step.workdir)} 2>/dev/null || {{ printf "__IL_ERR__ %s doesn't exist on $(hostname), or $(id -un) can't open it.\\n" {_q(step.workdir)}; exit 0; }}
IL_ERR_TAG="__IL_ERR__ "
{_root_check_sh(step, fail="exit 0") if step.folder_choice else ""}
{lines}
"""
        def remote():
            try:
                return _ssh_for(step.server).run("bash -s", stdin=script, timeout=30)[1]
            except SshError as e:
                raise HTTPException(status_code=502, detail=str(e))
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"Couldn't reach {step.server.name}: {e}")
        out = (await asyncio.to_thread(remote)).splitlines()
        for k, line in enumerate(out):
            if line.startswith("__IL_ERR__ "):
                raise HTTPException(status_code=422, detail=line[len("__IL_ERR__ "):])
            if line.startswith("__IL_F__ ") and k + 1 < len(out):
                name, _, path = line[len("__IL_F__ "):].partition(" ")
                if name in found and len(found[name]) < MAX_TEMPLATES:
                    try:
                        found[name].append((path, base64.b64decode(out[k + 1])))
                    except ValueError:
                        pass
    result: Dict[str, List[Dict[str, Any]]] = {}
    for name, files in found.items():
        result[name] = []
        for path, raw in sorted(files):
            try:
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
                result[name].append({"path": path, "profile": data})
            except ValueError as e:
                result[name].append({"path": path, "error": f"Can't be read as a profile: {e}"})
    return result


@ingestion_router.get("/python")
async def python_for_folder(
    step_id: str = Query(...),
    server_id: str = Query(...),
    workdir: Optional[str] = Query(None, description="The folder picked on the page; the step's own when left out"),
    auth: dict = Depends(require_permission(VIEW_PERM, RUN_PERM)),
):
    """For a step that runs @python: its virtualenv, every place looked, and the Python 3
    interpreters on that server a virtualenv could be made with."""
    _, steps, _, _ = await asyncio.to_thread(_cfg.snapshot)
    step = _step_for(steps, step_id, server_id)
    problems: Dict[str, str] = {}
    step = _in_folder(step, workdir, problems)
    if problems:
        raise HTTPException(status_code=422, detail=problems["workdir"])
    info = await _probe(step, discover=True)
    return {"folder": step.workdir, **info}


@ingestion_router.post("/venv")
async def create_venv(
    body: Dict[str, Any] = Body(...),
    auth: dict = Depends(require_permission(VIEW_PERM, RUN_PERM)),
):
    """Make a virtualenv in a step's run folder: {"step_id", "server_id", "workdir", "python",
    "requirements"}. Runs as a normal run (live output, history); returns {"run_id"}."""
    _start_background()
    settings, steps, _, _ = await asyncio.to_thread(_cfg.snapshot)
    step = _step_for(steps, body.get("step_id"), body.get("server_id"))
    problems: Dict[str, str] = {}
    step = _in_folder(step, body.get("workdir"), problems)
    if problems:
        raise HTTPException(status_code=422, detail=problems["workdir"])
    if IS_WINDOWS and step.server.local:
        raise HTTPException(status_code=409, detail="Creating a virtualenv from the page needs a Linux server.")
    info = await _probe(step, discover=True)
    if info["venv"]:
        raise HTTPException(status_code=409, detail=f"{step.workdir} already has a virtualenv: {info['venv']}")
    usable = {p["path"] for p in info["pythons"] if p["can_create"]}
    python = str(body.get("python") or "")
    if python not in usable:   # only interpreters the server itself just listed
        raise HTTPException(status_code=422, detail="Pick one of the Python versions found on the server.")
    want_req = bool(body.get("requirements")) and info["requirements"]
    venv_step = _venv_step(step.workdir, step.server)
    values, problems = venv_step.resolve({"python": python, "requirements": want_req})
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems.values()))
    shown = f"{shlex.quote(python)} -m venv venv" + (" && venv/bin/python -m pip install -r requirements.txt" if want_req else "")
    return await _begin(venv_step, values, auth, settings, f"{VENV_STEP_ID}@{step.server.id}:{step.workdir}",
                        command=shown)


@ingestion_router.post("/requirements")
async def install_requirements(
    body: Dict[str, Any] = Body(...),
    auth: dict = Depends(require_permission(VIEW_PERM, RUN_PERM)),
):
    """pip install the run folder's requirements.txt into the virtualenv @python uses there (it may sit in a
    folder above, e.g. 'NAS Scanner' using the repo's venv): {"step_id", "server_id", "workdir"}. Returns {"run_id"}."""
    _start_background()
    settings, steps, _, _ = await asyncio.to_thread(_cfg.snapshot)
    step = _step_for(steps, body.get("step_id"), body.get("server_id"))
    problems: Dict[str, str] = {}
    step = _in_folder(step, body.get("workdir"), problems)
    if problems:
        raise HTTPException(status_code=422, detail=problems["workdir"])
    if IS_WINDOWS and step.server.local:
        raise HTTPException(status_code=409, detail="Installing requirements from the page needs a Linux server.")
    info = await _probe(step, discover=True)
    if not info["venv"]:
        raise _no_venv(step, info["looked"])
    if not info["requirements"]:
        raise HTTPException(status_code=409, detail=f"There is no requirements.txt in {step.workdir}.")
    reqs = _reqs_step(step.workdir, step.server)
    values, problems = reqs.resolve({"python": info["venv"]})
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems.values()))
    return await _begin(reqs, values, auth, settings, f"{REQS_STEP_ID}@{step.server.id}:{info['venv']}",
                        command=f"{shlex.quote(info['venv'])} -m pip install -r requirements.txt")


MAX_FOLDERS = 500


@ingestion_router.get("/folders")
async def list_folders(
    step_id: str = Query(...),
    server_id: str = Query(...),
    path: Optional[str] = Query(None, description="Folder to list; the step's own folder when left out"),
    auth: dict = Depends(require_permission(VIEW_PERM, RUN_PERM)),
):
    """Subfolders a step can be run from, for the folder picker. Stays inside the step's roots."""
    _, steps, _, _ = await asyncio.to_thread(_cfg.snapshot)
    group = steps.get(step_id)
    step = group.variants.get(server_id) if group else None
    if step is None:
        raise HTTPException(status_code=404, detail="That step isn't set up on that server.")
    if not step.folder_choice:
        raise HTTPException(status_code=409, detail="This step always runs in its own folder.")
    try:
        folder = step.clean_folder(path or step.workdir or step.folder_roots[0])
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    marker = step.folder_marker
    parent = posixpath.dirname(folder) if folder.startswith("/") else os.path.dirname(folder)
    parent = parent if parent != folder and any(_under(parent, r) for r in step.folder_roots) else None

    if step.server.local:
        def scan():
            real = os.path.realpath(folder)
            if not any(_under(real, os.path.realpath(r)) for r in step.folder_roots):
                raise HTTPException(status_code=422, detail=f"That folder is really {real}, which is outside the allowed folders.")
            if not os.path.isdir(real):
                raise HTTPException(status_code=404, detail=f"{folder} doesn't exist on {step.server.name}.")
            dirs, truncated = [], False
            try:
                with os.scandir(real) as it:
                    for e in sorted(it, key=lambda e: e.name.lower()):
                        if e.name.startswith(".") or not e.is_dir(follow_symlinks=True):
                            continue
                        if len(dirs) >= MAX_FOLDERS:
                            truncated = True
                            break
                        dirs.append({"name": e.name,
                                     "marked": bool(marker and os.path.exists(os.path.join(e.path, marker)))})
            except PermissionError:
                raise HTTPException(status_code=403, detail=f"The portal can't open {folder}.")
            here = bool(marker and os.path.exists(os.path.join(real, marker)))
            return {"real": real, "dirs": dirs, "marked_here": here, "truncated": truncated}
        listing = await asyncio.to_thread(scan)
    else:
        script = f"""set -u
cd -- {_q(folder)} 2>/dev/null || {{ printf "__IL_ERR__ %s doesn't exist on $(hostname), or $(id -un) can't open it.\\n" {_q(folder)}; exit 0; }}
IL_ERR_TAG="__IL_ERR__ "
{_root_check_sh(step, fail="exit 0")}
echo "__IL_REAL__ $IL_REAL"
{f'[ -e {_q(marker)} ] && echo __IL_HERE__' if marker else ':'}
n=0
for d in */; do
  [ -d "$d" ] || continue
  d=${{d%/}}
  m=0
  {f'[ -e "$d"/{_q(marker)} ] && m=1' if marker else ':'}
  n=$((n+1)); [ $n -gt {MAX_FOLDERS} ] && {{ echo __IL_MORE__; break; }}
  echo "__IL_D__ $m $d"
done
"""
        def remote():
            try:
                return _ssh_for(step.server).run("bash -s", stdin=script, timeout=30)[1]
            except SshError as e:
                raise HTTPException(status_code=502, detail=str(e))
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=502, detail=f"Couldn't list folders on {step.server.name}: {e}")
        out = await asyncio.to_thread(remote)
        listing = {"real": None, "dirs": [], "marked_here": False, "truncated": False}
        for line in out.splitlines():
            if line.startswith("__IL_ERR__ "):
                raise HTTPException(status_code=422, detail=line[len("__IL_ERR__ "):])
            if line.startswith("__IL_REAL__ "):
                listing["real"] = line[len("__IL_REAL__ "):]
            elif line == "__IL_HERE__":
                listing["marked_here"] = True
            elif line == "__IL_MORE__":
                listing["truncated"] = True
            elif line.startswith("__IL_D__ "):
                flag, _, name = line[len("__IL_D__ "):].partition(" ")
                listing["dirs"].append({"name": name, "marked": flag == "1"})
        listing["dirs"].sort(key=lambda d: d["name"].lower())
    return {"path": folder, "parent": parent, "default": step.workdir, "roots": step.folder_roots,
            "marker": marker, **listing}


# ============================================================================
# Runs started outside the portal (someone's own nohup python main.py ...)
# ============================================================================
# Found by looking at the server's processes, so no pidfile is needed: a process of the
# server's user that runs a step's program and script in that step's folder (or under its
# roots), and wasn't started by the portal (those carry IL_RUN_ID). Linux only (/proc).

OUTSIDE_TTL = 5.0   # seconds a server's process scan is reused
_OUTSIDE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
PY_FLAGS = ("-u", "-B", "-O", "-OO", "-E", "-s", "-S", "-I", "-X", "-W")


def _canon(args: List[str]) -> List[str]:
    """python's arguments as [script, rest...]: '-m main' and 'main.py' both become 'main'."""
    args = list(args)
    while args and args[0] in PY_FLAGS:
        args = args[2:] if args[0] in ("-X", "-W") else args[1:]
    if len(args) >= 2 and args[0] == "-m":
        return [args[1], *args[2:]]
    if args and args[0].endswith(".py"):
        return [posixpath.basename(args[0])[:-3], *args[1:]]
    return args


def _signature(step: Step) -> Optional[List[str]]:
    """The fixed part of a step's command, to spot it among running processes. None: not watched
    (shell steps, and steps that may run several at once)."""
    if not step.command or not step.exclusive:
        return None
    fixed = [p for p in step.command[1:] if isinstance(p, str) and not PLACEHOLDER_RE.search(p)]
    sig = _canon(fixed)
    return sig or None


def _same_program(step: Step, argv0: str) -> bool:
    name = posixpath.basename(argv0)
    if step.command[0] == AUTO_PYTHON:
        return bool(re.fullmatch(r"python[0-9.]*", name))
    return name == posixpath.basename(step.command[0])


def _scan_sh(roots: List[str]) -> str:
    return f"""set -u
for r in {" ".join(_q(r) for r in roots)}; do rr=$(cd -- "$r" 2>/dev/null && pwd -P) && printf '__IL_R__ %s\\t%s\\n' "$r" "$rr"; done
for d in /proc/[0-9]*; do
  [ -r "$d/environ" ] || continue
  cmd=$(tr '\\0' '\\1' < "$d/cmdline" 2>/dev/null) || continue
  [ -n "$cmd" ] || continue
  tr '\\0' '\\n' < "$d/environ" 2>/dev/null | grep -q '^IL_RUN_ID=' && continue
  cwd=$(readlink -- "$d/cwd" 2>/dev/null) || continue
  s=$(cat "$d/stat" 2>/dev/null) || continue
  s=${{s##*) }}; set -- $s
  [ "$1" = Z ] && continue
  pid=${{d#/proc/}}
  info=$(ps -o etimes=,user= -p "$pid" 2>/dev/null) || continue
  printf '__IL_P__ %s\\t%s\\t%s\\t%s\\t%s\\n' "$pid" "$2" "$(echo $info)" "$cwd" "$cmd"
done
"""


def _match_outside(out: str, candidates: List[Step]) -> Dict[str, List[Dict[str, Any]]]:
    real: Dict[str, str] = {}
    procs = []
    for line in out.splitlines():
        if line.startswith("__IL_R__ "):
            given, _, rr = line[len("__IL_R__ "):].partition("\t")
            real[given] = rr
        elif line.startswith("__IL_P__ "):
            parts = line[len("__IL_P__ "):].split("\t", 4)
            if len(parts) != 5:
                continue
            pid, ppid, info, cwd, cmd = parts
            secs, _, user = info.partition(" ")
            argv = cmd.rstrip("\x01").split("\x01")
            procs.append({"pid": int(pid), "ppid": int(ppid), "seconds": int(secs) if secs.isdigit() else None,
                          "user": user, "folder": cwd, "argv": argv})
    found: Dict[str, List[Dict[str, Any]]] = {}
    hits: Dict[int, Tuple[int, str, Dict[str, Any]]] = {}
    for p in procs:
        canon = _canon(p["argv"][1:])
        for step in candidates:
            sig = _signature(step)
            if not sig or not canon or canon[0] != sig[0] or not _same_program(step, p["argv"][0]):
                continue
            if not all(t in canon[1:] for t in sig[1:]):
                continue
            if step.folder_choice:
                where = [real[r] for r in step.folder_roots if r in real]
                if not any(_under(p["folder"], r) for r in where):
                    continue
            elif p["folder"] != real.get(step.workdir, step.workdir):
                continue
            if p["pid"] not in hits or len(sig) > hits[p["pid"]][0]:   # the most specific step wins
                hits[p["pid"]] = (len(sig), step.id, p)
    pids = set(hits)
    for _, step_id, p in hits.values():
        if p["ppid"] in pids:
            continue   # a worker process of a run already listed
        found.setdefault(step_id, []).append({
            "pid": p["pid"], "user": p["user"], "seconds": p["seconds"], "folder": p["folder"],
            "command": shlex.join(p["argv"])})
    return found


async def _outside(srv: Server, steps: Dict[str, StepGroup], fresh: bool = False) -> Optional[Dict[str, Any]]:
    """{step id: [outside runs]} on that server; None when it can't be looked at (Windows)."""
    hit = _OUTSIDE.get(srv.id)
    if hit and not fresh and time.monotonic() - hit[0] < OUTSIDE_TTL:
        return hit[1]
    if srv.local and IS_WINDOWS:
        return None
    candidates = [g.variants[srv.id] for g in steps.values() if srv.id in g.variants and _signature(g.variants[srv.id])]
    if not candidates:
        return {}
    roots = sorted({r for s in candidates for r in (s.folder_roots if s.folder_choice else [s.workdir])})
    script = _scan_sh(roots)

    def scan():
        if srv.local:
            return subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, timeout=60, cwd="/").stdout
        return _ssh_for(srv).run("bash -s", stdin=script, timeout=60)[1]
    found = _match_outside(await asyncio.to_thread(scan), candidates)
    _OUTSIDE[srv.id] = (time.monotonic(), found)
    return found


@ingestion_router.get("/outside")
async def outside_runs(server_id: str = Query(...), auth: dict = Depends(require_permission(VIEW_PERM))):
    """Step runs on this server that weren't started from the portal: {"supported", "runs": {step id: [...]}}."""
    servers = await asyncio.to_thread(_cfg.servers_snapshot)
    srv = servers.get(server_id)
    if srv is None:
        raise HTTPException(status_code=404, detail="Unknown server")
    _, steps, _, _ = await asyncio.to_thread(_cfg.snapshot)
    try:
        found = await _outside(srv, steps)
    except Exception as e:  # noqa: BLE001 - a server we can't reach just shows nothing here
        return {"supported": True, "runs": {}, "error": f"Couldn't look at {srv.name}'s processes: {e}"}
    return {"supported": found is not None, "runs": found or {}}


@ingestion_router.post("/outside/stop")
async def stop_outside(body: Dict[str, Any] = Body(...), auth: dict = Depends(require_permission(VIEW_PERM, RUN_PERM))):
    """Stop a run started outside the portal: {"server_id", "step_id", "pid", "force"}. Only a pid the
    scan itself lists for that step, so this can't be pointed at any other process."""
    _, steps, _, _ = await asyncio.to_thread(_cfg.snapshot)
    step = _step_for(steps, body.get("step_id"), body.get("server_id"))
    srv = step.server
    force = bool(body.get("force"))
    try:
        pid = int(body.get("pid"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="pid must be a number")
    found = await _outside(srv, steps, fresh=True)
    if not found or not any(r["pid"] == pid for r in found.get(step.id, [])):
        raise HTTPException(status_code=409, detail=f"PID {pid} isn't running {step.name} on {srv.name} any more.")
    sig = KILL if force else (signal.SIGINT if step.stop_signal == "INT" else signal.SIGTERM)

    def go():
        if not srv.local:
            return _ssh_for(srv).signal_group(pid, sig)
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, sig)
            else:
                os.kill(pid, sig)
        except OSError as e:
            return str(e)
        return None
    err = await asyncio.to_thread(go)
    if err:
        raise HTTPException(status_code=502, detail=f"Couldn't stop PID {pid} on {srv.name}: {err}")
    _OUTSIDE.pop(srv.id, None)
    _audit("ingestion_outside_run_stopped", _email(auth), step.id, {"server": srv.id, "pid": pid, "force": force})
    logger.info("Ingestion Lab: %s sent to outside run %s (pid %s on %s) by %s",
                "KILL" if force else step.stop_signal, step.id, pid, srv.id, _email(auth))
    return {"sent": True, "force": force}


_CHECKS: Dict[str, Tuple[float, Dict[str, Any]]] = {}


@ingestion_router.get("/servers/{server_id}/check")
async def check_server(server_id: str, fresh: bool = Query(False), auth: dict = Depends(require_permission(VIEW_PERM))):
    """Can the portal log in to this server? Cached for CHECK_TTL seconds."""
    servers = await asyncio.to_thread(_cfg.servers_snapshot)
    srv = servers.get(server_id)
    if srv is None:
        raise HTTPException(status_code=404, detail="Unknown server")
    if srv.local:
        return {"ok": True, "local": True, "ms": 0}
    hit = _CHECKS.get(server_id)
    if hit and not fresh and time.monotonic() - hit[0] < CHECK_TTL:
        return hit[1]

    def probe():
        t0 = time.monotonic()
        try:
            _ssh_for(srv).run("echo ok", timeout=SSH_TIMEOUT)   # also warms the connection runs will use
            return {"ok": True, "ms": int((time.monotonic() - t0) * 1000)}
        except SshError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    result = await asyncio.to_thread(probe)
    _CHECKS[server_id] = (time.monotonic(), result)
    return result


@ingestion_router.get("/runs/{run_id}/stream")
async def stream_run(
    run_id: str,
    after: int = Query(0, ge=0, description="Resume after this log seq"),
    auth: dict = Depends(require_permission(VIEW_PERM)),
):
    """Server-sent events: log lines {seq, type, message, ts}, then {type: 'done', status, returncode}."""
    _check_run_id(run_id)
    return _sse_response(_watch(run_id, after))


@ingestion_router.post("/runs/{run_id}/stop")
async def stop_run(
    run_id: str,
    force: bool = Query(False, description="Kill right away instead of waiting for a graceful stop"),
    auth: dict = Depends(require_permission(VIEW_PERM, RUN_PERM)),
):
    """Ask the server running this step to stop it (the whole process group)."""
    _check_run_id(run_id)
    who = _user(auth)

    def ask(conn):
        _reap(conn)
        row = conn.execute("""UPDATE ingestion_runs SET stop_requested_by = COALESCE(stop_requested_by, ?),
                                                        force_stop_requested = force_stop_requested OR ?
                              WHERE id = ? AND finished_at IS NULL RETURNING id, step_id""",
                           (who, force, run_id)).fetchone()
        if row:
            conn.execute("SELECT pg_notify('ingestion_stop', ?)", (run_id,))
        return row
    row = await _db(ask)
    if row is None:
        # A remote step can outlive what the portal knows about it: signal it on its server directly.
        detail = await _db(lambda c: c.execute("SELECT step_id, server_id, pid, remote, status FROM ingestion_runs WHERE id = ?",
                                               (run_id,)).fetchone(), read_only=True)
        if detail and detail["remote"] and detail["pid"] and detail["status"] == "lost":
            result = await _stop_on_server(detail, who, force)
            _audit_stop(_email(auth), detail["step_id"], run_id, force, direct=True)
            return result
        raise HTTPException(status_code=409, detail="This run isn't running any more.")
    local = _ACTIVE.get(run_id)
    if local:   # ours: no need to wait for the NOTIFY
        if not local.ending:
            local.end(_stop_reason(local.step, who), stopped_by=who)
        if force and not local.killed:
            local.kill_now(by=who)
    _audit_stop(_email(auth), row["step_id"], run_id, force)
    logger.info("Ingestion run %s: %sstop requested by %s", run_id, "force " if force else "", who)
    return {"stopping": True, "force": force}


async def _stop_on_server(detail, who: str, force: bool):
    servers = await asyncio.to_thread(_cfg.servers_snapshot)
    srv = servers.get(detail["server_id"])
    if srv is None or srv.local:
        raise HTTPException(status_code=409, detail="That server is no longer configured.")
    _, steps, _, _ = await asyncio.to_thread(_cfg.snapshot)
    variant = steps.get(detail["step_id"]).variants.get(srv.id) if steps.get(detail["step_id"]) else None
    sig = KILL if force else (signal.SIGINT if variant and variant.stop_signal == "INT" else signal.SIGTERM)
    pid = int(detail["pid"])

    def go():
        ssh = _ssh_for(srv)
        try:
            _, out = ssh.run(f"{_alive_sh(str(pid))} && echo alive || echo gone", timeout=20)
            if "alive" not in out:
                return "gone"
            err = ssh.signal_group(pid, sig)
            return err or "sent"
        except SshError as e:
            return str(e)
    result = await asyncio.to_thread(go)
    if result == "gone":
        raise HTTPException(status_code=409, detail=f"It isn't running on {srv.name} any more.")
    if result != "sent":
        raise HTTPException(status_code=502, detail=f"Couldn't reach {srv.name}: {result}")
    logger.info("Ingestion run on %s pid %s: %s sent by %s", srv.id, pid, "KILL" if force else "stop", who)
    return {"stopping": True, "force": force, "direct": True}


def _audit(action: str, email: str, target: str, details: Dict[str, Any]):
    """Write an audit event off the event loop; auditing must never fail the run or the stop."""
    def write():
        try:
            write_audit_event(action=action, performed_by=email, target=target, details=json.dumps(details, default=str))
        except Exception as e:  # noqa: BLE001
            logger.warning("Ingestion Lab: audit %s for %s failed: %s", action, details.get("run_id"), e)
    try:
        asyncio.get_running_loop().run_in_executor(None, write)
    except RuntimeError:   # portal shutting down: the executor is gone
        write()


def _audit_stop(email: str, step_id: str, run_id: str, force: bool, direct: bool = False):
    _audit("ingestion_run_stopped", email, step_id, {"run_id": run_id, "force": force, "direct": direct})


@ingestion_router.get("/runs/{run_id}/log.txt")
async def download_log(run_id: str, auth: dict = Depends(require_permission(VIEW_PERM))):
    _check_run_id(run_id)

    run = await _db(lambda c: c.execute(f"SELECT {_RUN_COLUMNS} FROM ingestion_runs WHERE id = ?", (run_id,)).fetchone(),
                    read_only=True)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    exit_note = "" if run["returncode"] is None else f" (exit {run['returncode']})"
    where = f" on {run['server_name']}" if run["server_name"] else ""
    head = (f"{run['step_name']}{where} · started by {run['started_by']} at {run['started_at']} · "
            f"{run['status']}{exit_note}\nFolder: {run['workdir']}\nCommand: {run['command']}\n")
    if run["log_truncated"]:
        head += (f"Note: the command printed {run['log_lines']:,} lines; not all of them were saved here. "
                 "The step's log_file on the server, if it has one, holds the full output.\n")
    head += "\n"
    stamp = str(run["started_at"])[:19].replace(":", "-").replace("T", "_")
    name = f"ingestion_{run['step_id']}_{stamp}.txt"

    async def body():
        yield head
        last = 0
        while True:
            rows = await _db(lambda c, after=last: c.execute(
                "SELECT seq, ts, type, message FROM ingestion_run_logs WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (run_id, after, DOWNLOAD_BATCH)).fetchall(), read_only=True)
            if not rows:
                return
            last = rows[-1]["seq"]
            yield "".join(("-" * 60 if r["type"] == "separator" else f"{r['ts']}  {r['message']}") + "\n" for r in rows)
            if len(rows) < DOWNLOAD_BATCH:
                return
    return StreamingResponse(body(), media_type="text/plain; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


_background_loop = None


def _start_background():
    """Start this process's adopter (once per event loop)."""
    global _background_loop
    loop = asyncio.get_running_loop()
    _start_listener()
    if _background_loop is loop:
        return
    _background_loop = loop
    task = asyncio.ensure_future(_adopter())
    _TASKS.add(task)


async def _on_startup():
    _start_background()


# Picks up remote runs as soon as the portal starts, not only once someone opens the page.
ingestion_router.on_startup.append(_on_startup)


async def _adopter():
    cleaned: Optional[float] = None
    while True:
        try:
            await _adopt_once()
        except Exception as e:  # noqa: BLE001
            logger.warning("Ingestion Lab: looking for runs to pick up failed: %s", e)
        if cleaned is None or time.monotonic() - cleaned >= CLEANUP_EVERY:
            cleaned = time.monotonic()
            try:
                await _cleanup_old()
            except Exception as e:  # noqa: BLE001
                logger.warning("Ingestion Lab: clearing old runs failed: %s", e)
        await asyncio.sleep(ADOPT_EVERY)


async def _cleanup_old():
    """Runs (and their logs) older than keep_days go. keep_days 0 keeps them forever."""
    settings, _, _, _ = await asyncio.to_thread(_cfg.snapshot)
    days = int(settings["keep_days"])
    if days <= 0:
        return
    gone = await _db(lambda c: c.execute(f"""DELETE FROM ingestion_runs WHERE finished_at IS NOT NULL
                                             AND started_at < now() - interval '{days} days'""").rowcount)
    if gone:
        logger.info("Ingestion Lab: cleared %d run(s) older than %d days", gone, days)


async def _adopt_once():
    """Pick up remote runs nobody is following (their portal server restarted or died)."""
    me = socket.gethostname()

    def find(conn):
        return conn.execute(f"""SELECT id FROM ingestion_runs
                                WHERE finished_at IS NULL AND {_FOLLOWABLE}
                                  AND heartbeat_at < now() - interval '{STALE}' ORDER BY started_at""").fetchall()
    for row in await _db(find, read_only=True):
        run_id = row["id"]
        if run_id in _ACTIVE:
            continue

        def claim(conn):
            run = conn.execute(f"""UPDATE ingestion_runs SET host = ?, owner = ?, heartbeat_at = now()
                                   WHERE id = ? AND finished_at IS NULL AND heartbeat_at < now() - interval '{STALE}'
                                   RETURNING *, EXTRACT(EPOCH FROM (now() - started_at)) AS elapsed""", (me, OWNER, run_id)).fetchone()
            if not run:
                return None, None
            extra = conn.execute(f"""SELECT COALESCE(MAX(seq), 0) AS seq,
                                            COALESCE(MIN(seq) FILTER (WHERE seq > {KEEP_HEAD}), 0) AS first_after_head
                                     FROM ingestion_run_logs WHERE run_id = ?""", (run_id,)).fetchone()
            return run, extra
        run_row, extra = await _db(claim)
        if not run_row:
            continue   # someone else got it
        settings, steps, _, _ = await asyncio.to_thread(_cfg.snapshot)
        group = steps.get(run_row["step_id"])
        step = group.variants.get(run_row["server_id"]) if group else None
        if run_row["step_id"] in BUILTIN_STEPS:
            srv = _cfg.servers_snapshot().get(run_row["server_id"])
            step = BUILTIN_STEPS[run_row["step_id"]](run_row["workdir"], srv) if srv else None
        if step is None or step.server.local:
            def lost(conn):
                conn.execute("UPDATE ingestion_runs SET status = 'lost', finished_at = now() WHERE id = ?", (run_id,))
                seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM ingestion_run_logs WHERE run_id = ?",
                                   (run_id,)).fetchone()["s"]
                conn.execute("INSERT INTO ingestion_run_logs (run_id, seq, ts, type, message) VALUES (?, ?, now(), 'error', ?)",
                             (run_id, seq, f"Server '{run_row['server_id']}' or step '{run_row['step_id']}' is no longer "
                                           "configured, so this run can't be followed any more."))
                conn.execute("SELECT pg_notify('ingestion_log', ?)", (run_id,))
            await _db(lost)
            continue
        log = _Log(run_id, step.max_log_lines or settings["max_log_lines"], step.log_keep)
        log.seq = int(extra["seq"])
        log.stored = int(run_row["log_lines"] or 0)
        log.truncated = bool(run_row["log_truncated"])
        if extra["first_after_head"]:
            log.trimmed_to = max(0, int(extra["first_after_head"]) - 1)
        log.seen_offset = int(run_row["remote_offset"] or 0)
        run = _Run(run_id, step, {}, log)
        run.email = run_row["started_by_email"] or ""
        # A stop asked for while nobody followed the run may never have reached it, and its
        # grace timer died with the old owner: the first write sees the request and sends it again.
        _ACTIVE[run_id] = run
        log.add("info", f"Picked up again by portal server {me}; carrying on from where the log left off.")
        adopt = {"pid": int(run_row["pid"]), "spool": run_row["remote_spool"], "dir": run_row["remote_dir"],
                 "offset": int(run_row["remote_offset"] or 0)}
        task = asyncio.ensure_future(_run_task(run, adopt=adopt, elapsed=float(run_row["elapsed"] or 0)))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
        logger.info("Ingestion run %s (%s on %s): picked up", run_id, step.id, step.server.id)


def shutdown():
    """Server going down: stop our commands and close their runs now, instead of
    leaving them 'running' until the reaper notices."""
    for run_id, run in list(_ACTIVE.items()):
        if run.remote:
            try:
                run.detach = True
                run.log.release()
                run.log.add("info", f"Portal server restarting. {run.step.name} keeps running on {run.step.server.name} "
                                    "and will be picked up again.")
                with db.transaction() as conn:
                    run.log.flush(conn)
                    conn.execute("""UPDATE ingestion_runs SET heartbeat_at = now() - interval '1 day'
                                    WHERE id = ? AND finished_at IS NULL AND owner = ?""", (run_id, OWNER))
            except Exception as e:  # noqa: BLE001
                logger.warning("Ingestion run %s: shutdown hand-over failed: %s", run_id, e)
            continue
        try:
            run.server_stopping = True
            run.log.release()
            msg = "The portal server restarted, so this step was stopped. Start it again when ready."
            if run.process and run.process.poll() is None and not run.ending:
                run.end(msg)   # TERM the group now, KILL it after the grace period
            else:
                run.log.add("error", msg)
            with db.transaction() as conn:
                run.log.flush(conn)
                conn.execute("UPDATE ingestion_runs SET status = 'lost', finished_at = now() WHERE id = ? AND finished_at IS NULL",
                             (run_id,))
        except Exception as e:  # noqa: BLE001
            logger.warning("Ingestion run %s: shutdown cleanup failed: %s", run_id, e)
    with _SSH_POOL_LOCK:
        for ssh in _SSH_POOL.values():
            ssh.close()
        _SSH_POOL.clear()