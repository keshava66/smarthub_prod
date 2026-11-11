"""
Communication Center — send announcements, warnings and custom notices to users.

What lives here:
  • POST /api/communications/send       compose + send, now or at a set time
  • templates CRUD                      saved, reusable message bodies
  • GET  /api/communications/scheduled   what is queued, and cancelling one
  • GET  /api/communications/history     what was sent, by whom, to how many,
                                         plus CSV export, the files as they
                                         went out, and resend of a failure

Mail goes out through the local MTA, same as the scheduled report jobs do.
Rows live in the portal database (smarthub_core.email_*). Sent attachments
are kept on disk under communication_attachments/, because a message queued
for Saturday has to find its files long after the browser that uploaded them
has gone, and a resend should not ask for them again.
"""

import asyncio
import base64
import csv
import html as html_lib
import io
import json
import logging
import mimetypes
import re
import os
import shutil
import smtplib
import socket
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import db
from audit import write_audit_event
from auth import require_auth

logger = logging.getLogger(__name__)

EASTERN = pytz.timezone("America/New_York")

ATTACH_DIR = Path(__file__).parent / "communication_attachments"

communications_router = APIRouter(prefix="/api/communications", tags=["Communication Center"])


# ============================================================================
# CONFIG
# ============================================================================
# Wired in from app.py out of the "communications" section of dq_config.json.
# The defaults below are what the report wrappers already use, so the feature
# works out of the box on a host with a local MTA.
_CONFIG: Dict[str, Any] = {
    "smtp_host": "localhost",
    "smtp_port": 25,
    "smtp_timeout": 60,
    "sender": "gra-smarthub-dev-donotreply@its.jnj.com",
    "reply_to": "",
    # Empty list = any domain. Populate it to stop an admin from accidentally
    # mailing the whole outside world from a portal account.
    "allowed_domains": ["jnj.com", "its.jnj.com"],
    "max_recipients": 500,
    "max_attachment_mb": 10,
    "max_attachments": 5,
    # Copied in on every real send, on top of whatever the admin typed. Not
    # exposed by /config and not shown anywhere in the UI. A list, or one
    # comma-separated string. Test sends carry it too, so a test is a
    # faithful rehearsal of the real thing.
    "default_cc": [],
    # Blank = look in backend/assets/ then frontend/public/. Point this at
    # an absolute path if the logo lives somewhere else on the host.
    "logo_path": "",
    "logo_width": 190,
    # Sent attachments are copied under communication_attachments/ so a queued
    # send can still find them and a resend does not have to ask for them
    # again. Set store_attachments false to go back to recording only the name
    # and size; retention 0 keeps them forever.
    "store_attachments": True,
    "attachment_retention_days": 90,
    # Scheduling. Times are read as Eastern - the same clock every other
    # timestamp on this page is already in.
    "max_schedule_days": 90,
    "scheduler_poll_seconds": 30,
    # Purview sensitivity labels, the same ones Outlook shows behind the
    # shield. Left empty it falls back to DEFAULT_SENSITIVITY_LABELS below,
    # which is the set Outlook offers here. Override it only to change the
    # menu itself; the GUIDs go in "sensitivity_guids".
    "sensitivity_labels": [],
    # id -> label GUID, from whoever owns Purview or from Graph
    # /security/informationProtection/sensitivityLabels. A label with no GUID
    # still shows in the picker and is still recorded, it just goes out
    # without the msip_labels header.
    "sensitivity_guids": {},
    # id of the label pre-selected in the compose window. Blank = none.
    "default_sensitivity": "",
    # Directory (tenant) GUID the labels belong to. Goes out as SiteId; some
    # Exchange setups ignore a label without it.
    "tenant_id": "",
    # What Outlook puts in the header for these labels. Privileged means a
    # person chose the label rather than a policy guessing it, which is what
    # this page is. ContentBits is the visual marking bitmask the label is
    # configured with - copy it off a real labelled mail's msip_labels header
    # rather than guessing, and override per label below if they differ.
    "sensitivity_method": "Privileged",
    "sensitivity_content_bits": 0,
}

# Guards against a paste of an entire document into the compose box.
MAX_SUBJECT_CHARS = 300
MAX_BODY_CHARS = 50_000

# Used by the typed recipient boxes and by the standing Cc read out of the
# config file. Defined up here because _config_addresses() below runs at import
# time, before the ADDRESSES section further down would have been reached.
ADDR_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


def _config_addresses(raw: Any) -> List[str]:
    """Read an address list out of the config file.

    Accepts a list or one comma/semicolon-separated string. Anything that is
    not a plausible address is dropped with a warning rather than raised: a
    typo in dq_config.json should not take the whole feature down, and the
    admin sending an email is not the person who can fix it.

    The allowed_domains rule is deliberately NOT applied here - this list is
    written by whoever owns the config, so an off-domain distribution alias is
    assumed to be intentional.
    """
    if not raw:
        return []
    if isinstance(raw, str):
        items = re.split(r"[,;\n\r]+", raw)
    elif isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        logger.warning("Communication Center: default_cc is not a list or string, ignoring")
        return []

    out, seen = [], set()
    for item in items:
        addr = item.strip().strip("<>")
        if not addr:
            continue
        if not ADDR_RE.match(addr):
            logger.warning("Communication Center: dropping invalid default_cc entry %r", addr)
            continue
        if addr.lower() not in seen:
            seen.add(addr.lower())
            out.append(addr)
    return out


def set_communication_dependencies(section: Optional[Dict[str, Any]] = None) -> None:
    """Called once from app.py. Unknown keys are ignored, missing keys keep
    their default."""
    if not section:
        logger.info("Communication Center: no config section, using defaults")
        return
    for key in _CONFIG:
        if key in section and section[key] is not None:
            _CONFIG[key] = section[key]
    _CONFIG["default_cc"] = _config_addresses(_CONFIG.get("default_cc"))
    if _CONFIG["default_cc"]:
        logger.info("Communication Center standing Cc: %s",
                    ", ".join(_CONFIG["default_cc"]))

    _load_sensitivity_labels()

    logger.info(
        "Communication Center configured: smtp=%s:%s sender=%s domains=%s "
        "attachments=%s schedule<=%sd",
        _CONFIG["smtp_host"], _CONFIG["smtp_port"], _CONFIG["sender"],
        _CONFIG["allowed_domains"] or "any",
        "stored" if _CONFIG.get("store_attachments", True) else "not stored",
        _CONFIG.get("max_schedule_days"),
    )


# ============================================================================
# SENSITIVITY LABELS
# ============================================================================

DEFAULT_SENSITIVITY_LABELS: List[Dict[str, Any]] = [
    {"id": "general-business", "label": "General Business"},
    {"id": "restricted", "label": "Restricted", "children": [
        {"id": "restricted-none", "label": "No Encryption"},
        {"id": "restricted-encrypt", "label": "Encryption", "protection": "encrypt"},
    ]},
    {"id": "highly-restricted", "label": "Highly Restricted", "children": [
        {"id": "highly-restricted-encrypt", "label": "Encryption", "protection": "encrypt"},
        {"id": "highly-restricted-no-forward", "label": "Do Not Forward", "protection": "no-forward"},
    ]},
]

SENSITIVITY: Dict[str, Dict[str, str]] = {}
_SENSITIVITY_TREE: List[Dict[str, Any]] = []

GUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def _load_sensitivity_labels() -> None:
    SENSITIVITY.clear()
    _SENSITIVITY_TREE.clear()

    guids = _CONFIG.get("sensitivity_guids") or {}
    if not isinstance(guids, dict):
        logger.warning("Communication Center: sensitivity_guids is not a map, ignoring")
        guids = {}

    def walk(items: Any, trail: List[str]) -> List[Dict[str, Any]]:
        kept: List[Dict[str, Any]] = []
        if items is None:
            return kept
        if not isinstance(items, (list, tuple)):
            logger.warning("Communication Center: sensitivity_labels is not a list, ignoring")
            return kept
        for item in items:
            if not isinstance(item, dict):
                continue
            ident = str(item.get("id") or "").strip()
            label = str(item.get("label") or "").strip()
            if not ident or not label:
                logger.warning("Communication Center: sensitivity label needs an id and a label, dropping %r", item)
                continue
            if ident in SENSITIVITY:
                logger.warning("Communication Center: duplicate sensitivity label id %r, dropping the second", ident)
                continue

            # The GUID can sit on the label or in the sensitivity_guids map;
            # the map is the easier one to fill in once Purview hands them
            # over. Without one the label still works, it just does not stamp.
            guid = str(item.get("guid") or guids.get(ident) or "").strip().strip("{}")
            if guid and not GUID_RE.match(guid):
                logger.warning("Communication Center: sensitivity label %r has a malformed guid, ignoring it", ident)
                guid = ""

            path = trail + [label]
            children = walk(item.get("children"), path)
            SENSITIVITY[ident] = {
                "id": ident,
                "label": label,
                "guid": guid.lower(),
                "path": " \\ ".join(path),
                # encrypt / no-forward / "". Recorded and shown; actually
                # enforcing it is the transport rule's job, see
                # sensitivity_headers().
                "protection": str(item.get("protection") or "").strip(),
                # Per-label overrides for the two header fields that vary by
                # how the label was set up in Purview.
                "method": str(item.get("method") or "").strip(),
                "content_bits": item.get("content_bits"),
                # A parent with sub-labels is a menu, not a choice - Outlook
                # makes you pick one of the children.
                "selectable": not children,
            }
            node = {k: v for k, v in SENSITIVITY[ident].items() if k != "guid"}
            if children:
                node["children"] = children
            kept.append(node)
        return kept

    configured = _CONFIG.get("sensitivity_labels") or DEFAULT_SENSITIVITY_LABELS
    _SENSITIVITY_TREE.extend(walk(configured, []))

    default = str(_CONFIG.get("default_sensitivity") or "").strip()
    if default and not SENSITIVITY.get(default, {}).get("selectable"):
        logger.warning("Communication Center: default_sensitivity %r is not a selectable label", default)
        _CONFIG["default_sensitivity"] = ""

    unstamped = [v["path"] for v in SENSITIVITY.values() if v["selectable"] and not v["guid"]]
    if unstamped:
        logger.warning("Communication Center: no GUID for %s - these send unlabelled to Purview",
                       ", ".join(unstamped))
    logger.info("Communication Center sensitivity labels: %s",
                ", ".join(v["path"] for v in SENSITIVITY.values() if v["selectable"]))


def sensitivity_tree() -> List[Dict[str, Any]]:
    """What the picker draws. GUIDs stay on the server - the page only ever
    sends an id back."""
    return _SENSITIVITY_TREE


def _require_sensitivity(label_id: str) -> str:
    """Empty is fine - it means the message goes out unlabelled."""
    value = (label_id or "").strip()
    if not value:
        return value
    meta = SENSITIVITY.get(value)
    if not meta:
        raise HTTPException(status_code=400, detail=f"Unknown sensitivity label '{value}'")
    if not meta["selectable"]:
        raise HTTPException(
            status_code=400,
            detail=f"'{meta['path']}' has sub-labels - pick one of them")
    return value


def sensitivity_headers(label_id: str) -> Dict[str, str]:
    """The msip_labels header Outlook and Exchange read the label off.

    This stamps the classification. It does not encrypt: a label configured
    with rights protection only actually protects the mail if a transport rule
    on the Exchange side applies it to messages carrying this header.
    """
    meta = SENSITIVITY.get((label_id or "").strip())
    if not meta or not meta.get("guid"):
        return {}
    guid = meta["guid"]
    stamped = datetime.now(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S.%f0Z")
    method = meta.get("method") or _CONFIG.get("sensitivity_method") or "Privileged"
    bits = meta.get("content_bits")
    if bits is None:
        bits = _CONFIG.get("sensitivity_content_bits") or 0
    parts = [
        f"MSIP_Label_{guid}_Enabled=True",
        f"MSIP_Label_{guid}_SetDate={stamped}",
        f"MSIP_Label_{guid}_Method={method}",
        f"MSIP_Label_{guid}_Name={meta['label']}",
        f"MSIP_Label_{guid}_ContentBits={int(bits)}",
    ]
    tenant = str(_CONFIG.get("tenant_id") or "").strip().strip("{}")
    if tenant:
        parts.insert(2, f"MSIP_Label_{guid}_SiteId={tenant.lower()}")

    headers = {"msip_labels": "; ".join(parts)}
    # Encryption and Do Not Forward cannot be applied from here - an SMTP
    # client has no rights-management to apply. This says what was asked for
    # so a transport rule on the Exchange side can do it.
    if meta.get("protection"):
        headers["X-SmartHub-Protection"] = meta["protection"]
    return headers


# Category drives the coloured banner and the subject prefix. Kept server-side
# so the preview, the sent mail and the history row can never disagree.
LOGO_CID = "smarthub-logo"
_LOGO_CACHE: Dict[str, Any] = {}


def load_logo() -> Optional[bytes]:
    """Read the SmartHub logo once and keep it. Returns None if it cannot be
    found, in which case the template falls back to a text wordmark rather
    than rendering a broken image."""
    if "bytes" in _LOGO_CACHE:
        return _LOGO_CACHE["bytes"]

    configured = str(_CONFIG.get("logo_path") or "").strip()
    candidates = (
        [Path(configured)] if configured
        else [
            Path(__file__).parent / "assets" / "sh_logo.png",
            Path(__file__).parent.parent / "frontend" / "public" / "sh_logo.png",
        ]
    )
    for path in candidates:
        try:
            if path.is_file():
                _LOGO_CACHE["bytes"] = path.read_bytes()
                logger.info("Communication Center logo: %s", path)
                return _LOGO_CACHE["bytes"]
        except OSError:
            continue

    logger.warning(
        "Communication Center: no logo found (looked in %s) - using the text wordmark",
        ", ".join(str(c) for c in candidates),
    )
    _LOGO_CACHE["bytes"] = None
    return None


def logo_data_uri() -> Optional[str]:
    """Inline form, for the on-screen preview and the history viewer. The sent
    email uses a cid: reference instead - Outlook blocks remote images, and a
    data: URI is stripped by several clients."""
    data = load_logo()
    if not data:
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


# Brand faces first, Arial as the fallback. Email clients strip @font-face,
# so these only resolve where the fonts are installed locally - the normal
# case on a managed J&J machine.
FONT_DISPLAY = "'Johnson Display', 'JohnsonDisplay', Arial, Helvetica, sans-serif"
FONT_TEXT = "'Johnson Text', 'JohnsonText', Arial, Helvetica, sans-serif"

CATEGORIES: Dict[str, Dict[str, str]] = {
    "update": {
        "label": "Update",
        "color": "#eb1700",
        "prefix": "[SmartHub Update]",
    },
    "warning": {
        "label": "Warning",
        "color": "#ff6017",
        "prefix": "[SmartHub Warning]",
    },
    "maintenance": {
        "label": "Maintenance",
        "color": "#6e6259",
        "prefix": "[SmartHub Maintenance]",
    },
    "custom": {
        "label": "Custom",
        "color": "#eb1700",
        "prefix": "",
    },
}


# ============================================================================
# DATABASE
# ============================================================================

_db = db.transaction

# Who is sending a claimed queue row, for the record and for working out
# whether a claim has been abandoned.
_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def _json_or(value: Any, default: Any) -> Any:
    """Parse a JSON column, falling back instead of 500-ing on an odd row.

    Deliberately different from the parse in clean_layout(), which rejects
    malformed input with a 400 - that one reads a client payload, this one
    reads our own storage and should stay readable either way.
    """
    try:
        parsed = json.loads(value or "")
    except (ValueError, TypeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _require_category(category: str) -> None:
    if category not in CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Unknown category '{category}'")


def _require_length(subject: str, body: str) -> None:
    """Same ceiling for a send and for a saved template - a template that
    cannot be sent is worse than one that cannot be saved."""
    if len(subject or "") > MAX_SUBJECT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Subject is longer than {MAX_SUBJECT_CHARS} characters",
        )
    if len(body or "") > MAX_BODY_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Message is longer than {MAX_BODY_CHARS} characters",
        )


def _like(term: str) -> str:
    """LIKE pattern that treats % and _ as the characters the admin typed
    rather than as wildcards."""
    escaped = term.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
    return f"%{escaped}%"


def _hydrate_history_row(row) -> Dict[str, Any]:
    """One history row in API shape, with its JSON columns decoded."""
    entry = dict(row)
    # The page checks is_test === 1; the column is a boolean now.
    if "is_test" in entry:
        entry["is_test"] = 1 if entry["is_test"] else 0
    entry["attachments"] = _json_or(entry.get("attachments"), [])
    entry["layout"] = _json_or(entry.get("layout"), {})
    # Whether the files can still be handed back, or only named. A row from
    # before the store existed, one sent with storage off, and one whose files
    # have aged out all look the same from here, and all mean "names only".
    entry["files_stored"] = bool(_stored_paths(entry.get("attachment_dir") or ""))
    # The label the mail carried, by its display path. A label that has since
    # been removed from the config falls back to the raw id so the row does
    # not silently read as unlabelled.
    label_id = entry.get("sensitivity") or ""
    entry["sensitivity_label"] = (
        SENSITIVITY.get(label_id, {}).get("path") or label_id
    )
    return entry


def _split_stored(raw: str) -> List[str]:
    """Read back a recipient list this module wrote. Already validated on the
    way in, so unlike parse_recipients() this neither checks nor raises - a
    domain rule tightened since Tuesday must not strand Tuesday's queue."""
    return [addr.strip() for addr in (raw or "").split(",") if addr.strip()]


def recent_sends(limit: int = 5, category: Optional[str] = None) -> List[Dict[str, Any]]:
    """The last few real sends, for the drafter to read the house voice off.

    Tests and failures are left out: a rehearsal to yourself and a message that
    never arrived are both poor examples of what this team sends people.
    """
    where = ["NOT is_test", "status = 'sent'"]
    params: List[Any] = []
    if category:
        where.append("category = ?")
        params.append(category)
    with _db(read_only=True) as conn:
        rows = conn.execute(
            "SELECT subject, body, category, sent_at FROM email_history "
            "WHERE %s ORDER BY sent_at DESC LIMIT ?" % " AND ".join(where),
            (*params, max(1, min(limit, 20)))).fetchall()
    return [dict(r) for r in rows]


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def _safe_filename(raw: str) -> str:
    r"""A filename safe to join onto a path.

    Strips any directory the browser sent, Windows separators included - a
    bare Path().name leaves "..\payload.txt" whole on a posix host.
    """
    name = Path(str(raw or "").replace("\\", "/")).name
    name = _SAFE_NAME_RE.sub("_", name).strip(". ")
    return name[:180] or "attachment"


def _stored_paths(dirname: str) -> List[Path]:
    """The files kept for one message, in the order they were attached."""
    if not dirname:
        return []
    folder = ATTACH_DIR / _safe_filename(dirname)
    if not folder.is_dir():
        return []
    try:
        return sorted(p for p in folder.iterdir() if p.is_file())
    except OSError:
        return []


def store_attachments(owner_id: str, attachments: List[Dict[str, Any]]) -> str:
    """Write the files to disk; return the directory to record, or "".

    "" means "no files to retrieve later", which is what every row written
    before this existed already says, so the read side needs no special case.
    A storage failure is logged and downgraded to that rather than raised: the
    mail itself is fine, and losing the copy should not lose the send.
    """
    if not attachments or not _CONFIG.get("store_attachments", True):
        return ""

    folder = ATTACH_DIR / _safe_filename(owner_id)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for index, att in enumerate(attachments):
            name = "%02d__%s" % (index, _safe_filename(att["filename"]))
            (folder / name).write_bytes(att["content"])
    except OSError as exc:
        logger.error("Could not store attachments for %s: %s", owner_id, exc)
        shutil.rmtree(folder, ignore_errors=True)
        return ""
    return owner_id


def load_attachments(dirname: str) -> List[Dict[str, Any]]:
    """Read a stored set back into the shape send_message() wants."""
    out: List[Dict[str, Any]] = []
    for path in _stored_paths(dirname):
        filename = path.name.split("__", 1)[-1]
        guessed, _ = mimetypes.guess_type(filename)
        maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
        try:
            content = path.read_bytes()
        except OSError as exc:
            logger.warning("Stored attachment %s unreadable: %s", path, exc)
            continue
        out.append({
            "filename": filename,
            "content": content,
            "size": len(content),
            "maintype": maintype,
            "subtype": subtype or "octet-stream",
        })
    return out


def prune_attachments() -> None:
    """Drop stored files past the retention window. 0 days keeps them.

    Anything a queued message is still waiting on is left alone whatever its
    age: with the retention window and the scheduling horizon both defaulting
    to 90 days, a send booked for the far end of that horizon would otherwise
    have its files deleted out from under it the day before it went out.
    """
    days = int(_CONFIG.get("attachment_retention_days") or 0)
    if days <= 0 or not ATTACH_DIR.is_dir():
        return

    with _db(read_only=True) as conn:
        spoken_for = {
            row["attachment_dir"]
            for row in conn.execute(
                "SELECT attachment_dir FROM email_scheduled "
                "WHERE status IN ('pending', 'sending') AND attachment_dir <> ''"
            )
        }

    cutoff = datetime.now().timestamp() - days * 86400
    for folder in ATTACH_DIR.iterdir():
        if folder.name in spoken_for:
            continue
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                shutil.rmtree(folder, ignore_errors=True)
                logger.info("Pruned stored attachments: %s", folder.name)
        except OSError:
            continue


# ============================================================================
# ADDRESSES
# ============================================================================

def parse_recipients(raw: str, field: str) -> List[str]:
    """Split on comma, semicolon or newline. Validates, lowercases, dedupes
    while keeping the order the admin typed."""
    if not raw or not raw.strip():
        return []

    out: List[str] = []
    seen = set()
    for token in re.split(r"[,;\n\r]+", raw):
        addr = token.strip().strip("<>")
        if not addr:
            continue
        if not ADDR_RE.match(addr):
            raise HTTPException(
                status_code=400,
                detail=f"{field}: '{addr}' is not a valid email address",
            )
        allowed = _CONFIG.get("allowed_domains") or []
        if allowed:
            domain = addr.rsplit("@", 1)[1].lower()
            if not any(domain == d.lower() or domain.endswith("." + d.lower())
                       for d in allowed):
                raise HTTPException(
                    status_code=400,
                    detail=(f"{field}: '{addr}' is outside the allowed domains "
                            f"({', '.join(allowed)})"),
                )
        key = addr.lower()
        if key not in seen:
            seen.add(key)
            out.append(addr)
    return out


# ============================================================================
# BODY → HTML
# ============================================================================

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.S)
_URL_RE = re.compile(r"(https?://[^\s<>\"]+)")

_MD_LINK_RE = re.compile(
    r"\[([^\]\n]{1,200})\]\(\s*((?:https?://|mailto:)[^\s)\"']+)\s*\)")
_HEADING_RE = re.compile(r"^(#{1,3})\s+(\S.*)$")
_BULLET_RE = re.compile(r"^[-*]\s+(\S.*)$")
_NUMBER_RE = re.compile(r"^\d{1,3}[.)]\s+(\S.*)$")
_HEADING_SIZES = (24, 20, 17)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _inline(text: str, accent: str) -> str:
    """Bold, italic and links within one already-escaped block.

    Explicit [text](url) links are lifted out to placeholders first: the bare
    URL pass below matches anything after http, and would otherwise link the
    address inside the href it had just written.
    """
    held: List[str] = []

    def hold(match: "re.Match") -> str:
        held.append('<a href="%s" style="color:%s;">%s</a>'
                    % (match.group(2), accent, match.group(1)))
        return "\x00%d\x00" % (len(held) - 1)

    out = _MD_LINK_RE.sub(hold, text)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _ITALIC_RE.sub(r"<em>\1</em>", out)
    out = _URL_RE.sub(r'<a href="\1" style="color:%s;">\1</a>' % accent, out)

    def give_back(match: "re.Match") -> str:
        # The index can only be one we wrote, because _CONTROL_RE took every
        # \x00 out of the body first. Bounded anyway, so that moving that
        # strip some day is a rendering oddity rather than a 500.
        index = int(match.group(1))
        return held[index] if index < len(held) else match.group(0)

    return re.sub(r"\x00(\d+)\x00", give_back, out)


def body_to_html(body: str, accent: str = "#eb1700") -> str:
    """Render the typed body as the block of HTML that sits in the template.

    Walks the text a line at a time and closes the open block whenever the
    kind of line changes, so a list interrupted by a paragraph and picked up
    again comes out as two lists rather than one with a hole in it.
    """
    text = _CONTROL_RE.sub(
        "", (body or "").replace("\r\n", "\n").replace("\r", "\n"))
    escaped = html_lib.escape(text, quote=False)

    blocks: List[str] = []
    paragraph: List[str] = []
    items: List[str] = []
    list_tag = "ul"

    def flush_paragraph() -> None:
        if not paragraph:
            return
        # Joined before formatting, so **bold spanning
        # two lines** still closes.
        rendered = _inline("\n".join(paragraph), accent)
        blocks.append('<p style="margin:0 0 14px 0;">%s</p>'
                      % rendered.replace("\n", "<br>"))
        paragraph.clear()

    def flush_list() -> None:
        if not items:
            return
        blocks.append(
            '<%s style="margin:0 0 14px 0;padding-left:22px;">%s</%s>'
            % (list_tag,
               "".join('<li style="margin:0 0 6px 0;">%s</li>' % _inline(i, accent)
                       for i in items),
               list_tag))
        items.clear()

    for line in escaped.split("\n"):
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            flush_list()
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            flush_paragraph()
            flush_list()
            blocks.append(
                '<div style="font-family:%s;font-size:%dpx;line-height:1.25;'
                'color:%s;margin:4px 0 10px 0;">%s</div>'
                % (FONT_DISPLAY, _HEADING_SIZES[len(heading.group(1)) - 1],
                   accent, _inline(heading.group(2), accent)))
            continue

        bullet = _BULLET_RE.match(stripped)
        number = _NUMBER_RE.match(stripped)
        if bullet or number:
            wanted = "ul" if bullet else "ol"
            if items and list_tag != wanted:
                flush_list()
            flush_paragraph()
            list_tag = wanted
            items.append((bullet or number).group(1))
            continue

        flush_list()
        paragraph.append(line)

    flush_paragraph()
    flush_list()
    return "".join(blocks)


def build_email_html(category: str, body: str,
                     layout: Optional[Dict[str, Any]] = None,
                     logo_src: Optional[str] = None) -> str:
    """Wrap the body in the SmartHub house template.

    Table-based with inline CSS, because Outlook renders through Word: it drops
    <style> blocks and ignores most modern layout.

    `logo_src` is how the masthead image is addressed, and it differs by
    destination: the sent mail passes "cid:smarthub-logo" and carries the PNG as
    a related part (Outlook blocks remote images, so linking one would show a
    broken box), while the on-screen preview and the history viewer pass a
    data: URI, since cid: means nothing in a browser iframe. None falls back to
    a text wordmark. Resources rows use a text arrow for the same reason the
    logo is embedded rather than linked.

    Fonts: email clients strip @font-face, so the brand faces can only be named
    and hoped for. On a managed J&J machine they are installed locally and get
    used; everywhere else this falls back to Arial without a visible break.
    """
    layout = layout or {}
    meta = CATEGORIES.get(category, CATEGORIES["custom"])
    accent = meta["color"]

    banner_title = str(layout.get("banner_title") or "").strip()
    banner_subtitle = str(layout.get("banner_subtitle") or "").strip()
    headline = str(layout.get("headline") or "").strip()
    resources = layout.get("resources") or []

    content = body_to_html(body, accent)
    esc = html_lib.escape

    # -- banner -------------------------------------------------------------
    banner = ""
    if banner_title or banner_subtitle:
        subtitle_html = ""
        if banner_subtitle:
            subtitle_html = (
                '<div style="font-family:%s;font-size:20px;font-style:italic;'
                'color:#ffffff;padding-top:2px;">%s</div>'
                % (FONT_DISPLAY, esc(banner_subtitle))
            )
        banner = """
      <tr>
        <td style="background-color:%s;padding:26px 30px 22px 30px;">
          <div style="font-family:%s;font-size:40px;line-height:1.05;
                      color:#ffffff;">%s</div>
          %s
        </td>
      </tr>""" % (accent, FONT_DISPLAY, esc(banner_title), subtitle_html)

    # -- headline -----------------------------------------------------------
    headline_html = ""
    if headline:
        headline_html = """
      <tr>
        <td style="padding:26px 30px 0 30px;">
          <div style="font-family:%s;font-size:26px;line-height:1.2;
                      color:%s;">%s</div>
        </td>
      </tr>""" % (FONT_DISPLAY, accent, esc(headline))

    # -- resources ----------------------------------------------------------
    rows = []
    for item in resources:
        if not isinstance(item, dict):
            continue
        r_title = str(item.get("title") or "").strip()
        r_desc = str(item.get("description") or "").strip()
        r_text = str(item.get("link_text") or "Link").strip()
        r_url = str(item.get("link_url") or "").strip()
        if not (r_title or r_desc or r_url):
            continue

        link_html = ""
        if r_url:
            link_html = (
                '<div style="padding-top:10px;font-family:%s;font-size:15px;">'
                '<span style="color:%s;">&#8594;</span>&nbsp;'
                '<a href="%s" style="color:#312c2a;">%s</a></div>'
                % (FONT_TEXT, accent, esc(r_url, quote=True), esc(r_text))
            )
        rows.append("""
          <tr>
            <td style="padding:0 0 26px 16px;border-left:3px solid %s;">
              <div style="font-family:%s;font-size:22px;line-height:1.2;
                          color:%s;">%s</div>
              <div style="font-family:%s;font-size:15px;line-height:1.55;
                          color:#312c2a;padding-top:6px;">%s</div>
              %s
            </td>
          </tr>""" % (accent, FONT_DISPLAY, accent, esc(r_title),
                      FONT_TEXT, esc(r_desc), link_html))

    resources_html = ""
    if rows:
        resources_html = """
      <tr>
        <td style="padding:8px 30px 0 30px;">
          <div style="font-family:%s;font-size:18px;color:#81766f;
                      padding-bottom:18px;">Resources</div>
          <table role="presentation" cellpadding="0" cellspacing="0" border="0"
                 width="100%%">%s
          </table>
        </td>
      </tr>""" % (FONT_DISPLAY, "".join(rows))

    # -- masthead -----------------------------------------------------------
    if logo_src:
        logo_html = (
            '<img src="%s" width="%d" alt="GRA SmartHub" '
            'style="display:block;border:0;outline:none;text-decoration:none;'
            '-ms-interpolation-mode:bicubic;">'
            % (esc(logo_src, quote=True), int(_CONFIG.get("logo_width") or 190))
        )
    else:
        logo_html = (
            '<div style="font-family:%s;font-size:26px;font-weight:bold;'
            'color:#eb1700;letter-spacing:-0.5px;">GRA SmartHub</div>' % FONT_DISPLAY
        )

    return """<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background-color:#ffffff;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%"
       style="background-color:#ffffff;">
  <tr><td align="center" style="padding:28px 12px;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640"
           style="max-width:640px;width:100%%;">

      <tr>
        <td style="padding:0 30px 22px 30px;">%s</td>
      </tr>%s%s

      <tr>
        <td style="padding:18px 30px 6px 30px;font-family:%s;font-size:15px;
                   line-height:1.6;color:#312c2a;">
          %s
        </td>
      </tr>

      <tr>
        <td style="padding:10px 30px 26px 30px;font-family:%s;font-size:15px;
                   line-height:1.6;color:#312c2a;">
          Regards,<br>
          <strong>GRA SmartHub</strong><br>
          <span style="color:#6e6259;">RDI - AI Squad</span>
        </td>
      </tr>%s

      <tr>
        <td style="background-color:#f1efed;padding:22px 30px;font-family:%s;
                   font-size:12px;line-height:1.6;color:#81766f;">
          This email is confidential and for internal use only.<br>
          Automated message from GRA SmartHub - please do not reply.
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>""" % (logo_html, banner, headline_html, FONT_TEXT, content,
              FONT_TEXT, resources_html, FONT_TEXT)


def build_subject(category: str, subject: str) -> str:
    """Prefix the subject by category, unless the admin already typed it."""
    meta = CATEGORIES.get(category, CATEGORIES["custom"])
    # Newlines out first - a header is one line, and the subject is the one
    # field that lands in one - then the remaining control characters, which
    # would otherwise reach the MTA inside an encoded word.
    clean = _CONTROL_RE.sub(
        "", re.sub(r"[\r\n]+", " ", subject or "")).strip()
    prefix = meta["prefix"]
    if prefix and not clean.lower().startswith(prefix.lower()):
        return f"{prefix} {clean}"
    return clean


LAYOUT_KEYS = ("banner_title", "banner_subtitle", "headline")
MAX_RESOURCES = 8


def clean_layout(raw: Any) -> Dict[str, Any]:
    """Whitelist the layout payload. Anything unrecognised is dropped rather
    than stored, so the template never renders a key it did not expect."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            raise HTTPException(status_code=400, detail="Layout is not valid JSON")
    if not isinstance(raw, dict):
        return {}

    out: Dict[str, Any] = {}
    for key in LAYOUT_KEYS:
        value = str(raw.get(key) or "").strip()
        if value:
            out[key] = value[:300]

    resources = []
    for item in (raw.get("resources") or [])[:MAX_RESOURCES]:
        if not isinstance(item, dict):
            continue
        entry = {
            "title": str(item.get("title") or "").strip()[:200],
            "description": str(item.get("description") or "").strip()[:600],
            "link_text": str(item.get("link_text") or "").strip()[:120],
            "link_url": str(item.get("link_url") or "").strip()[:1000],
        }
        url = entry["link_url"]
        if url and not url.lower().startswith(("http://", "https://", "mailto:")):
            raise HTTPException(
                status_code=400,
                detail="Resource links must start with http://, https:// or mailto:",
            )
        if any(entry.values()):
            resources.append(entry)
    if resources:
        out["resources"] = resources
    return out


# ============================================================================
# SENDING
# ============================================================================

def send_message(
    subject: str,
    html_body: str,
    to_addrs: List[str],
    cc_addrs: List[str],
    bcc_addrs: List[str],
    attachments: List[Dict[str, Any]],
    sensitivity: str = "",
) -> None:
    """Build and hand the message to the MTA. Raises on failure — the caller
    records the error in history."""
    msg = EmailMessage()
    msg["From"] = _CONFIG["sender"]
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    if _CONFIG.get("reply_to"):
        msg["Reply-To"] = _CONFIG["reply_to"]
    msg["Subject"] = subject
    msg["Auto-Submitted"] = "auto-generated"
    for name, value in sensitivity_headers(sensitivity).items():
        msg[name] = value

    # Plain-text fallback: strip the tags out of the rendered HTML.
    text = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", "", html_body)
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    # List items lose their shape entirely once the tags go, so mark them
    # before that happens rather than run the bullets into one paragraph.
    text = re.sub(r"(?i)<\s*li[^>]*>", "\n* ", text)
    text = re.sub(r"(?i)</\s*(p|li|ul|ol|tr|div|h[1-6])\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", html_lib.unescape(text)).strip()

    msg.set_content(text + "\n")
    msg.add_alternative(html_body, subtype="html")

    # The HTML alternative becomes multipart/related so the <img cid:...> in the
    # masthead resolves against a part carried inside the message. Remote images
    # are blocked by default in Outlook, so this is the only way the logo shows
    # without the recipient clicking "download pictures".
    logo_bytes = load_logo()
    if logo_bytes:
        html_part = msg.get_payload()[-1]
        html_part.add_related(
            logo_bytes, maintype="image", subtype="png", cid="<%s>" % LOGO_CID
        )

    for att in attachments:
        # Real type where the extension gives one away, so a PDF arrives as a
        # PDF instead of as an unnamed binary the recipient has to trust.
        msg.add_attachment(
            att["content"],
            maintype=att.get("maintype") or "application",
            subtype=att.get("subtype") or "octet-stream",
            filename=att["filename"],
        )

    envelope = to_addrs + cc_addrs + bcc_addrs
    with smtplib.SMTP(
        _CONFIG["smtp_host"],
        int(_CONFIG["smtp_port"]),
        timeout=int(_CONFIG["smtp_timeout"]),
    ) as server:
        server.send_message(msg, from_addr=_CONFIG["sender"], to_addrs=envelope)


def _record_history(row: Dict[str, Any]) -> None:
    row = {**row, "is_test": bool(row.get("is_test"))}
    with _db(autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO email_history
                (id, sent_at, sent_by, category, subject, body, layout,
                 to_addrs, cc_addrs, bcc_addrs, attachments, attachment_dir,
                 recipient_count, status, error, is_test,
                 scheduled_id, resent_from, sensitivity, release_notes)
            VALUES (:id, :sent_at, :sent_by, :category, :subject, :body, :layout,
                    :to_addrs, :cc_addrs, :bcc_addrs, :attachments, :attachment_dir,
                    :recipient_count, :status, :error, :is_test,
                    :scheduled_id, :resent_from, :sensitivity, :release_notes)
            """,
            row,
        )


def build_record(
    sender_email: str,
    category: str,
    subject: str,
    body: str,
    layout: Dict[str, Any],
    to_addrs: List[str],
    cc_addrs: List[str],
    bcc_addrs: List[str],
    attachments: List[Dict[str, Any]],
    attachment_dir: str = "",
    is_test: bool = False,
    scheduled_id: str = "",
    resent_from: str = "",
    sensitivity: str = "",
    release_note_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """One history row, before it has been sent.

    Three paths write history now - compose, the queue and a resend - and
    every one of them has to produce the same shape, so they all come through
    here rather than each building the dict by hand.
    """
    return {
        "id": str(uuid.uuid4()),
        "sent_at": datetime.now(EASTERN).isoformat(),
        "sent_by": sender_email.lower(),
        "category": category,
        "subject": subject,
        "body": body,
        "layout": json.dumps(layout),
        "to_addrs": ", ".join(to_addrs),
        "cc_addrs": ", ".join(cc_addrs),
        "bcc_addrs": ", ".join(bcc_addrs),
        "attachments": json.dumps(
            [{"filename": a["filename"], "size": a["size"]} for a in attachments]
        ),
        "attachment_dir": attachment_dir,
        "recipient_count": len(to_addrs) + len(cc_addrs) + len(bcc_addrs),
        "status": "sent",
        "error": "",
        "is_test": 1 if is_test else 0,
        "scheduled_id": scheduled_id,
        "resent_from": resent_from,
        "sensitivity": sensitivity,
        "release_notes": ",".join(release_note_ids or []),
    }


def deliver(
    record: Dict[str, Any],
    to_addrs: List[str],
    cc_addrs: List[str],
    bcc_addrs: List[str],
    attachments: List[Dict[str, Any]],
) -> None:
    """Render, hand to the MTA, and write the history row either way.

    Blocking - call it through asyncio.to_thread(). Re-raises after recording
    the failure, so a caller can turn it into a 502 and the row is on the
    record with its error whether anyone was watching or not.
    """
    html_body = build_email_html(
        record["category"], record["body"],
        _json_or(record["layout"], {}), "cid:%s" % LOGO_CID,
    )
    try:
        send_message(record["subject"], html_body,
                     to_addrs, cc_addrs, bcc_addrs, attachments,
                     record.get("sensitivity") or "")
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        try:
            _record_history(record)
        except Exception as db_exc:
            logger.error("Communication %s failed and the failure could not be recorded: %s",
                         record["id"], db_exc)
        raise

    record["status"] = "sent"
    record["error"] = ""
    try:
        _record_history(record)
    except Exception as exc:
        # The mail has gone. Reporting a failure now would only invite a
        # second send to everyone, so log it loudly and carry on.
        logger.error("Communication %s was sent but its history row could not be written: %s",
                     record["id"], exc)
    _close_release_notes(record)


def _close_release_notes(record: Dict[str, Any]) -> None:
    """Tick off the what's-new items this message announced.

    Only after the MTA has taken it, and never for a test. Failing here must
    not turn a delivered email into an error, so it is logged and swallowed -
    an item still sitting in the queue is a nuisance, a false 502 after five
    hundred people already got the mail is worse.
    """
    ids = [n for n in (record.get("release_notes") or "").split(",") if n.strip()]
    if not ids or record.get("is_test"):
        return
    try:
        import release_notes as notes
        closed = notes.mark_announced(ids, record["id"])
        if closed:
            logger.info("Marked %d release note(s) announced in %s", closed, record["id"])
    except Exception as exc:
        logger.warning("Could not mark release notes announced: %s", exc)


# ============================================================================
# ROUTES — compose & send
# ============================================================================

def _ai_drafting_available() -> bool:
    # Imported here, not at the top: communication_ai reaches into
    # assistant.router for the Azure settings, and app.py imports this module
    # long before that one is wired up.
    try:
        import communication_ai
        return communication_ai.is_available()
    except Exception:
        return False


@communications_router.get("/config")
async def get_config(auth: dict = Depends(require_auth)):
    """Limits and categories, so the page can show them instead of guessing."""
    return {
        "sender": _CONFIG["sender"],
        "allowed_domains": _CONFIG["allowed_domains"],
        "max_recipients": _CONFIG["max_recipients"],
        "max_attachment_mb": _CONFIG["max_attachment_mb"],
        "max_attachments": _CONFIG["max_attachments"],
        "max_subject_chars": MAX_SUBJECT_CHARS,
        "max_body_chars": MAX_BODY_CHARS,
        "max_schedule_days": int(_CONFIG.get("max_schedule_days") or 90),
        "store_attachments": bool(_CONFIG.get("store_attachments", True)),
        # The page labels its date picker with this. Send times are read as
        # Eastern whatever the admin's laptop is set to, and saying so costs
        # less than being wrong by five hours.
        "timezone": "America/New_York",
        "timezone_label": "ET",
        "categories": [
            {"id": key, "label": meta["label"], "color": meta["color"],
             "prefix": meta["prefix"]}
            for key, meta in CATEGORIES.items()
        ],
        # The tree as configured, so the picker can nest sub-labels the way
        # Outlook does. Empty = no picker.
        "sensitivity_labels": sensitivity_tree(),
        "default_sensitivity": _CONFIG.get("default_sensitivity") or "",
        # False on a host with no Azure OpenAI configured, so the compose tab
        # hides the button rather than offering something that 503s.
        "ai_drafting": _ai_drafting_available(),
    }


class PreviewRequest(BaseModel):
    category: str = "custom"
    subject: str = ""
    body: str = ""
    layout: Optional[Dict[str, Any]] = None


@communications_router.post("/preview")
async def preview(req: PreviewRequest, auth: dict = Depends(require_auth)):
    """Render exactly what send() would render, so the preview can't drift."""
    return {
        "subject": build_subject(req.category, req.subject),
        "html": build_email_html(
            req.category, req.body, clean_layout(req.layout), logo_data_uri()
        ),
    }


class DraftRequest(BaseModel):
    mode: str = "write"
    prompt: str = ""
    category: str = "custom"
    subject: str = ""
    body: str = ""
    audience: str = ""
    # Ticked in the What's New panel. Empty means "whatever is unannounced".
    note_ids: List[str] = []


@communications_router.post("/draft")
async def draft_communication(req: DraftRequest, auth: dict = Depends(require_auth)):
    """Write or rework the message with the portal's model.

    The model can look things up before it writes — the portal knowledge base,
    the what's-new list, past sends, and the same data tools the floating
    assistant has. Those data tools run against this caller's own token and
    re-check this caller's own permissions, so the draft can never contain
    something the person writing it could not open themselves.

    Nothing is sent and nothing is stored. The draft goes back to the browser
    and the admin decides whether to keep it. It is audited, because "who asked
    the model to write the outage notice" is a fair question later.
    """
    import communication_ai

    _require_category(req.category)
    if req.mode == "write" and not req.prompt.strip():
        raise HTTPException(status_code=400, detail="Say what the message should cover")
    if req.mode in communication_ai.NEEDS_BODY and not req.body.strip():
        raise HTTPException(status_code=400, detail="There is no message to work on yet")
    if not communication_ai.is_available():
        raise HTTPException(status_code=503, detail="AI drafting is not configured on this server")

    email = auth["user"]["email"]
    try:
        result = await asyncio.to_thread(
            communication_ai.draft,
            mode=req.mode,
            prompt=req.prompt,
            category=req.category,
            subject=req.subject,
            body=req.body,
            audience=req.audience,
            sender=auth["user"].get("name") or email,
            note_ids=req.note_ids[:50],
            perms=auth.get("permissions"),
            role=auth.get("role"),
            token=auth.get("token"),
            email=email,
        )
    except Exception as exc:
        logger.error(f"Communication draft failed for {email}: {exc}")
        raise HTTPException(status_code=502, detail=f"Could not draft the message: {exc}")

    write_audit_event(
        action="communication_drafted",
        performed_by=email,
        target=req.mode,
        details=f"[{req.category}] {(req.prompt or req.subject or '')[:180]}"
                + (f" · looked up: {', '.join(result.get('looked_up') or [])}"
                   if result.get("looked_up") else ""),
    )
    return result


def parse_send_at(raw: str) -> Optional[datetime]:
    """Read "2026-09-20T14:30" off the picker as an Eastern wall-clock time.

    Not the browser's timezone: every other timestamp on this page is already
    ET, the server's clock is ET, and an admin typing "Saturday 9pm" means 9pm
    in the office. The field is labelled ET so nobody has to guess.
    """
    value = (raw or "").strip()
    if not value:
        return None
    try:
        naive = datetime.fromisoformat(value[:19]).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Send time is not a valid date and time")

    when = EASTERN.localize(naive)
    now = datetime.now(EASTERN)
    if when <= now + timedelta(seconds=30):
        raise HTTPException(status_code=400, detail="Pick a send time in the future")

    horizon = int(_CONFIG.get("max_schedule_days") or 90)
    if when > now + timedelta(days=horizon):
        raise HTTPException(
            status_code=400,
            detail=f"A message cannot be scheduled more than {horizon} days ahead")
    return when


@communications_router.post("/send")
async def send_communication(
    category: str = Form("custom"),
    subject: str = Form(...),
    body: str = Form(...),
    layout: str = Form("{}"),
    to: str = Form(""),
    cc: str = Form(""),
    bcc: str = Form(""),
    test_only: bool = Form(False),
    send_at: str = Form(""),
    sensitivity: str = Form(""),
    # Comma-separated what's-new ids this message announces. They are marked
    # announced only once the mail actually leaves, never at compose time.
    release_notes: str = Form(""),
    files: Optional[List[UploadFile]] = File(None),
    auth: dict = Depends(require_auth),
):
    sender_email = auth["user"]["email"]
    note_ids = [n.strip() for n in (release_notes or "").split(",") if n.strip()][:50]
    # A rehearsal to yourself has not told anyone anything.
    if test_only:
        note_ids = []

    _require_category(category)
    sensitivity = _require_sensitivity(sensitivity)
    if not subject.strip():
        raise HTTPException(status_code=400, detail="Subject is required")
    if not body.strip():
        raise HTTPException(status_code=400, detail="Message body is required")
    _require_length(subject, body)

    # A test goes to the admin who pressed the button and nobody else, no
    # matter what is sitting in the recipient boxes.
    if test_only:
        # A test ignores the typed recipients entirely and goes to the admin who
        # pressed the button - but it still carries the standing Cc below, so a
        # test is a faithful rehearsal of the real thing.
        to_addrs, cc_addrs, bcc_addrs = [sender_email], [], []
    else:
        to_addrs = parse_recipients(to, "To")
        cc_addrs = parse_recipients(cc, "Cc")
        bcc_addrs = parse_recipients(bcc, "Bcc")

        if not to_addrs:
            raise HTTPException(status_code=400, detail="At least one To recipient is required")

    # Standing Cc from dq_config.json, on every send including tests. Appended
    # server-side rather than prefilled into the Cc box, because the admin never
    # sees it and must not be able to drop it.
    cc_addrs += _CONFIG.get("default_cc") or []

    # One copy per person: To wins over Cc, Cc wins over Bcc. Also catches a
    # repeat inside a single field, and an address that is both typed in and
    # already on the standing list - including the admin testing a message who
    # is themselves on that list.
    seen = {a.lower() for a in to_addrs}

    def _dedupe(addrs: List[str]) -> List[str]:
        kept = []
        for addr in addrs:
            key = addr.lower()
            if key not in seen:
                seen.add(key)
                kept.append(addr)
        return kept

    cc_addrs = _dedupe(cc_addrs)
    bcc_addrs = _dedupe(bcc_addrs)

    total = len(to_addrs) + len(cc_addrs) + len(bcc_addrs)
    if total > int(_CONFIG["max_recipients"]):
        raise HTTPException(
            status_code=400,
            detail=(f"{total} recipients exceeds the limit of "
                    f"{_CONFIG['max_recipients']}"),
        )

    # ── attachments ────────────────────────────────────────────────────────
    uploads = [f for f in (files or []) if f and f.filename]
    if len(uploads) > int(_CONFIG["max_attachments"]):
        raise HTTPException(
            status_code=400,
            detail=f"At most {_CONFIG['max_attachments']} attachments allowed",
        )

    max_bytes = int(float(_CONFIG["max_attachment_mb"]) * 1024 * 1024)
    attachments: List[Dict[str, Any]] = []
    running_total = 0
    for upload in uploads:
        # Read in chunks and stop the moment the budget is blown, rather than
        # pulling a multi-hundred-MB upload into memory only to refuse it.
        parts: List[bytes] = []
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            running_total += len(chunk)
            if running_total > max_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=(f"Attachments exceed the "
                            f"{_CONFIG['max_attachment_mb']} MB limit"),
                )
            parts.append(chunk)
        content = b"".join(parts)
        # Strip any path the browser sent, Windows separators included: a bare
        # Path().name leaves "..\\payload.txt" whole on a posix host.
        filename = Path(str(upload.filename).replace("\\", "/")).name or "attachment"
        guessed, _ = mimetypes.guess_type(filename)
        maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
        attachments.append({
            "filename": filename,
            "content": content,
            "size": len(content),
            "maintype": maintype,
            "subtype": subtype or "octet-stream",
        })

    clean = clean_layout(layout)
    full_subject = build_subject(category, subject)

    # ── queue it instead ───────────────────────────────────────────────────
    # A test always goes now. "Send me this on Saturday so I can check it" is
    # not what the test button means, and finding out on Saturday that the MTA
    # is unreachable defeats the point of a rehearsal.
    when = None if test_only else parse_send_at(send_at)
    if when:
        queue_id = str(uuid.uuid4())
        stored = store_attachments(queue_id, attachments)
        if attachments and not stored:
            raise HTTPException(
                status_code=507,
                detail=("The attachments could not be stored, so this message "
                        "cannot be queued. Send it now, or schedule it without "
                        "the files."),
            )

        row = {
            "id": queue_id,
            "created_at": datetime.now(EASTERN).isoformat(),
            "created_by": sender_email.lower(),
            "send_at": when.isoformat(),
            "category": category,
            "subject": full_subject,
            "body": body,
            "layout": json.dumps(clean),
            "to_addrs": ", ".join(to_addrs),
            "cc_addrs": ", ".join(cc_addrs),
            "bcc_addrs": ", ".join(bcc_addrs),
            "attachments": json.dumps(
                [{"filename": a["filename"], "size": a["size"]} for a in attachments]
            ),
            "attachment_dir": stored,
            "recipient_count": len(to_addrs) + len(cc_addrs) + len(bcc_addrs),
            "status": "pending",
            "error": "",
            "history_id": "",
            "sensitivity": sensitivity,
            "release_notes": ",".join(note_ids),
        }

        def _queue():
            with _db() as conn:
                conn.execute(
                    """
                    INSERT INTO email_scheduled
                        (id, created_at, created_by, send_at, category, subject, body,
                         layout, to_addrs, cc_addrs, bcc_addrs, attachments,
                         attachment_dir, recipient_count, status, error, history_id,
                         sensitivity, release_notes)
                    VALUES (:id, :created_at, :created_by, :send_at, :category, :subject,
                            :body, :layout, :to_addrs, :cc_addrs, :bcc_addrs,
                            :attachments, :attachment_dir, :recipient_count, :status,
                            :error, :history_id, :sensitivity, :release_notes)
                    """,
                    row,
                )

        await asyncio.to_thread(_queue)

        write_audit_event(
            action="communication_scheduled",
            performed_by=sender_email,
            target=f"{row['recipient_count']} recipient(s)",
            details=f"[{category}] {full_subject} for {when:%Y-%m-%d %H:%M %Z}",
        )
        logger.info("Communication queued for %s by %s", when.isoformat(), sender_email)

        return {
            "success": True,
            "scheduled": True,
            "id": queue_id,
            "subject": full_subject,
            "send_at": when.isoformat(),
            "recipient_count": row["recipient_count"],
            "test": False,
        }

    # ── or send it now ─────────────────────────────────────────────────────
    record = build_record(
        sender_email=sender_email,
        category=category,
        subject=full_subject,
        body=body,
        layout=clean,
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        bcc_addrs=bcc_addrs,
        attachments=attachments,
        is_test=test_only,
        sensitivity=sensitivity,
        release_note_ids=note_ids,
    )
    # A test's files are not kept: nobody resends a rehearsal, and testing a
    # 10 MB deck every morning would fill the disk with copies of it.
    if not test_only:
        record["attachment_dir"] = store_attachments(record["id"], attachments)

    try:
        await asyncio.to_thread(
            deliver, record, to_addrs, cc_addrs, bcc_addrs, attachments)
    except Exception as exc:
        logger.error(f"Communication send failed for {sender_email}: {exc}")
        raise HTTPException(status_code=502, detail=f"Could not send email: {exc}")

    write_audit_event(
        action="communication_test_sent" if test_only else "communication_sent",
        performed_by=sender_email,
        target=f"{record['recipient_count']} recipient(s)",
        details=f"[{category}] {full_subject}",
    )

    return {
        "success": True,
        "scheduled": False,
        "id": record["id"],
        "subject": full_subject,
        "recipient_count": record["recipient_count"],
        "test": test_only,
    }


# ============================================================================
# ROUTES — templates
# ============================================================================

class TemplateRequest(BaseModel):
    name: str
    category: str = "custom"
    subject: str = ""
    body: str = ""
    layout: Optional[Dict[str, Any]] = None


@communications_router.get("/templates")
def list_templates(auth: dict = Depends(require_auth)):
    with _db(read_only=True) as conn:
        rows = conn.execute(
            "SELECT * FROM email_templates ORDER BY lower(name), name"
        ).fetchall()

    templates = []
    for row in rows:
        item = dict(row)
        item["layout"] = _json_or(item.get("layout"), {})
        templates.append(item)
    return {"templates": templates}


@communications_router.post("/templates")
def create_template(req: TemplateRequest, auth: dict = Depends(require_auth)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Template name is required")
    _require_category(req.category)
    _require_length(req.subject, req.body)

    now = datetime.now(EASTERN).isoformat()
    template_id = str(uuid.uuid4())
    try:
        with _db() as conn:
            conn.execute(
                """
                INSERT INTO email_templates
                    (id, name, category, subject, body, layout,
                     created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (template_id, name, req.category, req.subject, req.body,
                 json.dumps(clean_layout(req.layout)),
                 auth["user"]["email"].lower(), now, now),
            )
    except db.UniqueViolation:
        raise HTTPException(status_code=409, detail=f"A template named '{name}' already exists")

    write_audit_event(
        action="communication_template_created",
        performed_by=auth["user"]["email"],
        target=name,
    )
    return {"success": True, "id": template_id}


@communications_router.put("/templates/{template_id}")
def update_template(
    template_id: str,
    req: TemplateRequest,
    auth: dict = Depends(require_auth),
):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Template name is required")
    _require_category(req.category)
    _require_length(req.subject, req.body)

    with _db() as conn:
        existing = conn.execute(
            "SELECT id FROM email_templates WHERE id = ?", (template_id,)
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Template not found")
        try:
            conn.execute(
                """
                UPDATE email_templates
                   SET name = ?, category = ?, subject = ?, body = ?, layout = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (name, req.category, req.subject, req.body,
                 json.dumps(clean_layout(req.layout)),
                 datetime.now(EASTERN).isoformat(), template_id),
            )
        except db.UniqueViolation:
            # name is UNIQUE - renaming onto an existing template used to 500.
            raise HTTPException(
                status_code=409,
                detail=f"A template named '{name}' already exists",
            )

    write_audit_event(
        action="communication_template_updated",
        performed_by=auth["user"]["email"],
        target=name,
    )
    return {"success": True}


@communications_router.delete("/templates/{template_id}")
def delete_template(template_id: str, auth: dict = Depends(require_auth)):
    with _db() as conn:
        row = conn.execute(
            "DELETE FROM email_templates WHERE id = ? RETURNING name", (template_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Template not found")

    write_audit_event(
        action="communication_template_deleted",
        performed_by=auth["user"]["email"],
        target=row["name"],
    )
    return {"success": True}


# ============================================================================
# ROUTES — history
# ============================================================================

def _history_where(search: str, category: str) -> Tuple[List[str], List[Any]]:
    """The search and category half of the filter, shared by the list, its
    counts and the CSV export."""
    where: List[str] = []
    params: List[Any] = []
    if search.strip():
        # ILIKE: SQLite's LIKE ignored case, and the search box always has.
        where.append("(subject ILIKE ? ESCAPE '\\' OR to_addrs ILIKE ? ESCAPE '\\' "
                     "OR sent_by ILIKE ? ESCAPE '\\')")
        term = _like(search.strip())
        params += [term, term, term]
    if category.strip():
        _require_category(category.strip())
        where.append("category = ?")
        params.append(category.strip())
    return where, params


def _history_with_status(
    where: List[str], params: List[Any], status: str
) -> Tuple[List[str], List[Any]]:
    """Add the status rail to a filter.

    "sent" and "failed" read the delivery status, "test" reads the flag, so a
    test that went out counts under both - the rail has always shown it that
    way and the move to the server keeps it.
    """
    wanted = status.strip().lower()
    where, params = list(where), list(params)
    if wanted == "test":
        where.append("is_test")
    elif wanted in ("sent", "failed"):
        where.append("status = ?")
        params.append(wanted)
    elif wanted not in ("", "all"):
        raise HTTPException(status_code=400, detail=f"Unknown status '{status}'")
    return where, params


@communications_router.get("/history/export")
def export_history(
    search: str = Query(""),
    category: str = Query(""),
    status: str = Query(""),
    auth: dict = Depends(require_auth),
):
    """The filtered history as a CSV download.

    Declared above /history/{entry_id} on purpose: routes match in the order
    they are written, so the other way round "export" arrives as an entry id
    and comes back 404.

    The body is left out. It is the one column that can run to fifty thousand
    characters, it is already in the viewer, and a spreadsheet is the wrong
    place to read an email.
    """
    where, params = _history_with_status(*_history_where(search, category), status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    with _db(read_only=True) as conn:
        rows = conn.execute(
            f"""
            SELECT sent_at, sent_by, category, subject, status, error,
                   recipient_count, to_addrs, cc_addrs, bcc_addrs, attachments,
                   is_test, scheduled_id, resent_from
              FROM email_history {clause}
             ORDER BY sent_at DESC, id
            """,
            params,
        ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "sent_at", "sent_by", "category", "subject", "status", "error",
        "recipients", "to", "cc", "bcc", "attachments", "test",
        "was_scheduled", "resend_of",
    ])
    for row in rows:
        files = _json_or(row["attachments"], [])
        writer.writerow([
            row["sent_at"], row["sent_by"], row["category"], row["subject"],
            row["status"], row["error"], row["recipient_count"],
            row["to_addrs"], row["cc_addrs"], row["bcc_addrs"],
            "; ".join(str(f.get("filename") or "") for f in files),
            "yes" if row["is_test"] else "",
            "yes" if row["scheduled_id"] else "",
            row["resent_from"],
        ])

    filename = f"communications_{datetime.now(EASTERN).strftime('%Y%m%d_%H%M%S')}.csv"
    write_audit_event(
        action="communication_history_exported",
        performed_by=auth["user"]["email"],
        target=f"{len(rows)} row(s)",
    )
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@communications_router.get("/history")
def get_history(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    search: str = Query(""),
    category: str = Query(""),
    status: str = Query(""),
    auth: dict = Depends(require_auth),
):
    """One page of sent mail, plus the tallies for everything the search matches.

    The status rail narrows the page but not the counts, and both are worked
    out here rather than in the browser: the page used to tally only the rows
    it had already loaded, so "Failed 0" could be true of page one and quite
    wrong about the three failures sitting on page three.
    """
    where, params = _history_where(search, category)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    page_where, page_params = _history_with_status(where, params, status)
    page_clause = f"WHERE {' AND '.join(page_where)}" if page_where else ""

    # Tallies, total and page in one statement: one round trip, and one
    # snapshot, so they can't disagree about a row written in between. The
    # LEFT JOIN keeps the tallies when the page itself is empty.
    with _db(read_only=True) as conn:
        rows = conn.execute(
            f"""
            SELECT c.all_n, c.sent_n, c.failed_n, c.test_n, t.total, p.*
              FROM (SELECT COUNT(*) AS all_n,
                           COUNT(*) FILTER (WHERE status = 'sent')   AS sent_n,
                           COUNT(*) FILTER (WHERE status = 'failed') AS failed_n,
                           COUNT(*) FILTER (WHERE is_test)           AS test_n
                      FROM email_history {clause}) AS c
             CROSS JOIN (SELECT COUNT(*) AS total FROM email_history {page_clause}) AS t
              LEFT JOIN LATERAL (
                    SELECT id, sent_at, sent_by, category, subject, to_addrs, cc_addrs,
                           bcc_addrs, attachments, attachment_dir, recipient_count,
                           status, error, is_test, scheduled_id, resent_from,
                           sensitivity
                      FROM email_history
                      {page_clause}
                     ORDER BY sent_at DESC, id
                     LIMIT ? OFFSET ?
                   ) AS p ON true
            """,
            params + page_params + page_params + [limit, offset],
        ).fetchall()

        type_where, type_params = _history_with_status(*_history_where(search, ""), status)
        type_clause = f"WHERE {' AND '.join(type_where)}" if type_where else ""
        by_type = conn.execute(
            f"SELECT category, COUNT(*) AS n FROM email_history {type_clause} GROUP BY category",
            type_params,
        ).fetchall()

    head = rows[0]
    counts = {k: head[k] for k in ("all_n", "sent_n", "failed_n", "test_n")}
    total = head["total"]
    tally = {"all_n", "sent_n", "failed_n", "test_n", "total"}
    entries = [_hydrate_history_row({k: v for k, v in r.items() if k not in tally})
               for r in rows if r["id"] is not None]
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "entries": entries,
        "counts": {
            "all": counts["all_n"] or 0,
            "sent": counts["sent_n"] or 0,
            "failed": counts["failed_n"] or 0,
            "test": counts["test_n"] or 0,
        },
        "by_category": {r["category"]: r["n"] for r in by_type},
    }


@communications_router.get("/history/{entry_id}")
def get_history_entry(entry_id: str, auth: dict = Depends(require_auth)):
    with _db(read_only=True) as conn:
        row = conn.execute(
            "SELECT * FROM email_history WHERE id = ?", (entry_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="History entry not found")

    entry = _hydrate_history_row(row)
    # Re-render from the stored body so the viewer sees what went out.
    entry["html"] = build_email_html(
        entry["category"], entry["body"], entry["layout"], logo_data_uri()
    )
    return entry


@communications_router.get("/history/{entry_id}/files/{index}")
def download_history_file(
    entry_id: str, index: int, auth: dict = Depends(require_auth)
):
    """Hand back one attachment as it went out.

    Indexed by position rather than by name so two files called report.xlsx
    stay distinguishable, and so the link the page builds from the recorded
    list lines up with what is on disk.
    """
    with _db(read_only=True) as conn:
        row = conn.execute(
            "SELECT attachment_dir FROM email_history WHERE id = ?", (entry_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="History entry not found")

    paths = _stored_paths(row["attachment_dir"])
    if index < 0 or index >= len(paths):
        raise HTTPException(
            status_code=404,
            detail="That file is no longer stored. Only its name was kept",
        )

    path = paths[index]
    filename = path.name.split("__", 1)[-1]
    guessed, _ = mimetypes.guess_type(filename)
    return FileResponse(
        path, filename=filename, media_type=guessed or "application/octet-stream"
    )


@communications_router.post("/history/{entry_id}/resend")
async def resend_history_entry(entry_id: str, auth: dict = Depends(require_auth)):
    """Send a stored message again, to the same people, with the same files.

    Written as a NEW history row pointing back at the original. A failure and
    the retry that fixed it are both worth having on the record, and quietly
    flipping the old row to "sent" would lose the fact that it ever failed.
    """
    sender_email = auth["user"]["email"]

    with _db(read_only=True) as conn:
        row = conn.execute(
            "SELECT * FROM email_history WHERE id = ?", (entry_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="History entry not found")

    original = dict(row)
    to_addrs = _split_stored(original["to_addrs"])
    cc_addrs = _split_stored(original["cc_addrs"])
    bcc_addrs = _split_stored(original["bcc_addrs"])
    if not to_addrs:
        raise HTTPException(
            status_code=400,
            detail="That message has no stored recipients to send to",
        )

    attachments = load_attachments(original.get("attachment_dir") or "")
    recorded = _json_or(original.get("attachments"), [])
    missing = max(0, len(recorded) - len(attachments))

    record = build_record(
        sender_email=sender_email,
        category=original["category"],
        # Already carries its category prefix; build_subject() would leave it
        # alone, but there is nothing here to re-prefix in the first place.
        subject=original["subject"],
        body=original["body"],
        layout=_json_or(original.get("layout"), {}),
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        bcc_addrs=bcc_addrs,
        attachments=attachments,
        # Points at the same folder as the original rather than writing a
        # second copy of files that have not changed.
        attachment_dir=original.get("attachment_dir") or "",
        is_test=bool(original.get("is_test")),
        resent_from=entry_id,
        # Same classification it went out with the first time.
        sensitivity=original.get("sensitivity") or "",
    )

    try:
        await asyncio.to_thread(
            deliver, record, to_addrs, cc_addrs, bcc_addrs, attachments)
    except Exception as exc:
        logger.error(f"Communication resend of {entry_id} failed: {exc}")
        raise HTTPException(status_code=502, detail=f"Could not send email: {exc}")

    write_audit_event(
        action="communication_resent",
        performed_by=sender_email,
        target=f"{record['recipient_count']} recipient(s)",
        details=f"Resend of {entry_id}: {original['subject']}",
    )

    return {
        "success": True,
        "id": record["id"],
        "subject": record["subject"],
        "recipient_count": record["recipient_count"],
        "missing_attachments": missing,
    }


# ============================================================================
# ROUTES — the queue
# ============================================================================

def _hydrate_scheduled_row(row) -> Dict[str, Any]:
    entry = dict(row)
    entry["attachments"] = _json_or(entry.get("attachments"), [])
    entry["layout"] = _json_or(entry.get("layout"), {})
    label_id = entry.get("sensitivity") or ""
    entry["sensitivity_label"] = (
        SENSITIVITY.get(label_id, {}).get("path") or label_id
    )
    return entry


@communications_router.get("/scheduled")
def list_scheduled(
    status: str = Query("pending"),
    limit: int = Query(100, ge=1, le=500),
    auth: dict = Depends(require_auth),
):
    """The queue, plus a tally per status for the rail.

    Pending first and soonest first; everything else newest first, because a
    finished row is history and the interesting one is the last.
    """
    wanted = status.strip().lower()
    allowed = ("pending", "sending", "sent", "failed", "cancelled")
    if wanted not in allowed + ("", "all"):
        raise HTTPException(status_code=400, detail=f"Unknown status '{status}'")

    clause, params = "", []
    if wanted in allowed:
        clause, params = "WHERE status = ?", [wanted]

    # Per-status tallies ride along with the page: one round trip.
    with _db(read_only=True) as conn:
        rows = conn.execute(
            f"""
            SELECT c.tallies, p.*
              FROM (SELECT json_object_agg(status, n) AS tallies
                      FROM (SELECT status, COUNT(*) AS n FROM email_scheduled
                             GROUP BY status) AS s) AS c
              LEFT JOIN LATERAL (
                    SELECT id, created_at, created_by, send_at, category, subject,
                           to_addrs, cc_addrs, bcc_addrs, attachments, attachment_dir,
                           recipient_count, status, error, history_id, sensitivity
                      FROM email_scheduled
                      {clause}
                     ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
                              CASE WHEN status = 'pending' THEN send_at END ASC,
                              send_at DESC, id
                     LIMIT ?
                   ) AS p ON true
            """,
            params + [limit],
        ).fetchall()

    found = json.loads(rows[0]["tallies"] or "{}")
    counts = {key: int(found.get(key, 0)) for key in allowed}
    counts["all"] = sum(counts[key] for key in allowed)
    rows = [{k: v for k, v in r.items() if k != "tallies"} for r in rows if r["id"] is not None]
    return {
        "entries": [_hydrate_scheduled_row(row) for row in rows],
        "counts": counts,
        "now": datetime.now(EASTERN).isoformat(),
    }


@communications_router.get("/scheduled/{scheduled_id}")
def get_scheduled(scheduled_id: str, auth: dict = Depends(require_auth)):
    """One queued message, rendered, so the preview pane can show what will go
    out rather than just when."""
    with _db(read_only=True) as conn:
        row = conn.execute(
            "SELECT * FROM email_scheduled WHERE id = ?", (scheduled_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Scheduled message not found")

    entry = _hydrate_scheduled_row(row)
    entry["html"] = build_email_html(
        entry["category"], entry["body"], entry["layout"], logo_data_uri()
    )
    entry["files_stored"] = bool(_stored_paths(entry.get("attachment_dir") or ""))
    # The label the mail carried, by its display path. A label that has since
    # been removed from the config falls back to the raw id so the row does
    # not silently read as unlabelled.
    label_id = entry.get("sensitivity") or ""
    entry["sensitivity_label"] = (
        SENSITIVITY.get(label_id, {}).get("path") or label_id
    )
    return entry


@communications_router.delete("/scheduled/{scheduled_id}")
def cancel_scheduled(scheduled_id: str, auth: dict = Depends(require_auth)):
    """Cancel a queued send. The row stays, marked cancelled - who called off
    Saturday's maintenance notice is worth being able to answer."""
    with _db() as conn:
        # Conditional update first: if the queue claimed it a moment ago this
        # changes nothing, and the files it is about to send are left alone.
        row = conn.execute(
            "UPDATE email_scheduled SET status = 'cancelled' "
            "WHERE id = ? AND status = 'pending' RETURNING subject",
            (scheduled_id,),
        ).fetchone()
        if not row:
            current = conn.execute(
                "SELECT status FROM email_scheduled WHERE id = ?", (scheduled_id,)
            ).fetchone()
            if not current:
                raise HTTPException(status_code=404, detail="Scheduled message not found")
            raise HTTPException(
                status_code=409,
                detail=f"That message is already {current['status']}, too late to cancel",
            )

    shutil.rmtree(ATTACH_DIR / _safe_filename(scheduled_id), ignore_errors=True)

    write_audit_event(
        action="communication_schedule_cancelled",
        performed_by=auth["user"]["email"],
        target=row["subject"],
    )
    return {"success": True}


# ============================================================================
# THE QUEUE WORKER
# ============================================================================

_scheduler_task: Optional[asyncio.Task] = None


def _stuck_after_seconds() -> int:
    """How long a claim may sit in 'sending' before it counts as abandoned:
    comfortably longer than any real send (connect + transfer, both bounded by
    the SMTP timeout), so a slow send on another worker is never mistaken for
    a dead one."""
    return max(900, int(_CONFIG.get("smtp_timeout") or 60) * 6)


def _release_stuck_rows() -> None:
    """A row left 'sending' belongs to a process that died mid-flight.

    Marked failed rather than re-queued: the MTA may well have accepted it
    before the lights went out, and sending a maintenance notice to five
    hundred people twice is worse than making an admin look and decide.

    Only claims older than _stuck_after_seconds() count. Other workers and
    replicas share this queue, so a fresh claim is someone else mid-send.
    """
    with _db(autocommit=True) as conn:
        stuck = conn.execute(
            "UPDATE email_scheduled SET status = 'failed', error = ? "
            "WHERE status = 'sending' "
            "AND (claimed_at IS NULL OR claimed_at < now() - make_interval(secs => ?)) "
            "RETURNING id",
            ("The server restarted while this was being sent. Check the sent "
             "history before sending it again.", _stuck_after_seconds()),
        ).fetchall()
    if stuck:
        logger.warning("Communication Center: %d queued send(s) were interrupted", len(stuck))


async def _send_one_scheduled(row: Dict[str, Any]) -> None:
    to_addrs = _split_stored(row["to_addrs"])
    cc_addrs = _split_stored(row["cc_addrs"])
    bcc_addrs = _split_stored(row["bcc_addrs"])
    attachments = load_attachments(row.get("attachment_dir") or "")

    record = build_record(
        sender_email=row["created_by"],
        category=row["category"],
        subject=row["subject"],
        body=row["body"],
        layout=_json_or(row.get("layout"), {}),
        to_addrs=to_addrs,
        cc_addrs=cc_addrs,
        bcc_addrs=bcc_addrs,
        attachments=attachments,
        attachment_dir=row.get("attachment_dir") or "",
        scheduled_id=row["id"],
        sensitivity=row.get("sensitivity") or "",
        release_note_ids=_split_stored(row.get("release_notes") or ""),
    )

    try:
        await asyncio.to_thread(
            deliver, record, to_addrs, cc_addrs, bcc_addrs, attachments)
        status, error = "sent", ""
        logger.info("Queued communication %s sent to %d recipient(s)",
                    row["id"], record["recipient_count"])
    except Exception as exc:
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        logger.error("Queued communication %s failed: %s", row["id"], exc)

    def _finish():
        with _db(autocommit=True) as conn:
            conn.execute(
                "UPDATE email_scheduled SET status = ?, error = ?, history_id = ? "
                "WHERE id = ?",
                (status, error, record["id"], row["id"]),
            )

    await asyncio.to_thread(_finish)

    write_audit_event(
        action="communication_sent" if status == "sent" else "communication_send_failed",
        performed_by=row["created_by"],
        target=f"{record['recipient_count']} recipient(s)",
        details=f"[{row['category']}] {row['subject']} (scheduled)",
    )


def _claim_next_due() -> Optional[Dict[str, Any]]:
    """Take the next message that is due, or None.

    Claiming and reading happen in one statement. SKIP LOCKED lets several
    workers poll the same queue without waiting on each other, and the
    status = 'pending' condition means a cancel landing in the same instant
    wins or loses cleanly - the message never goes out twice. The database
    clock decides what is due, so replicas can't disagree about it.
    """
    with _db(autocommit=True) as conn:
        row = conn.execute(
            """
            UPDATE email_scheduled
               SET status = 'sending', claimed_by = ?, claimed_at = now()
             WHERE id = (
                   SELECT id FROM email_scheduled
                    WHERE status = 'pending' AND send_at <= now()
                    ORDER BY send_at, id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED)
               AND status = 'pending'
            RETURNING *
            """,
            (_WORKER_ID,),
        ).fetchone()
    return dict(row) if row else None


async def _run_due_sends() -> None:
    # One at a time, claimed just before it goes out, so a crash strands at
    # most the message in hand. Twenty per tick keeps one busy minute from
    # starving the rest of the loop.
    for _ in range(20):
        row = await asyncio.to_thread(_claim_next_due)
        if row is None:
            return
        await _send_one_scheduled(row)


async def _scheduler_loop() -> None:
    poll = max(5, int(_CONFIG.get("scheduler_poll_seconds") or 30))
    logger.info("Communication Center queue running, checking every %ss", poll)
    while True:
        try:
            await asyncio.to_thread(_release_stuck_rows)
            await _run_due_sends()
            await asyncio.to_thread(prune_attachments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never let one bad tick kill the loop - the next message due is
            # usually unrelated to whatever went wrong with this one.
            logger.error("Communication Center queue tick failed: %s", exc)
        await asyncio.sleep(poll)


async def start_scheduler() -> None:
    """Start the queue worker. Never raises.

    This runs as an app startup handler, and an exception in one of those stops
    the whole portal from coming up. A locked database or a missing directory
    is worth a loud log line and a Communication Center that cannot schedule -
    it is not worth taking Data Validation and the Audit Log down with it.
    """
    global _scheduler_task
    try:
        if _scheduler_task is None or _scheduler_task.done():
            _scheduler_task = asyncio.create_task(_scheduler_loop())
    except Exception as exc:
        logger.error("Communication Center queue could not start: %s", exc)


async def stop_scheduler() -> None:
    """Stop the queue worker. Never raises, for the same reason."""
    global _scheduler_task
    try:
        if _scheduler_task and not _scheduler_task.done():
            _scheduler_task.cancel()
    except Exception as exc:
        logger.warning("Communication Center queue shutdown: %s", exc)
    finally:
        _scheduler_task = None


# Registered on the router rather than in app.py: include_router() copies these
# onto the app, so wiring the feature in stays the one-line job it already was.
communications_router.add_event_handler("startup", start_scheduler)
communications_router.add_event_handler("shutdown", stop_scheduler)