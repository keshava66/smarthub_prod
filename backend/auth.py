from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from fastapi import Depends, Header, HTTPException

logger = logging.getLogger(__name__)

GRAPH_API_ENDPOINT = "https://graph.microsoft.com/v1.0"

# One shared session so Graph calls reuse pooled keep-alive connections.
# Bare requests.get was opening a fresh TCP+TLS handshake for every call,
# which through the corporate proxy costs more than the request itself.
# requests.Session is thread-safe for the way we use it (concurrent GETs).
_graph_session = requests.Session()
_graph_session.mount(
    "https://",
    requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16),
)

# Validating every request against Graph would add a network round-trip to each
# call. Cache the validated identity keyed on a hash of the token (never the
# token itself) for a short window.
TOKEN_CACHE_TTL_SECONDS = 300
GRAPH_TIMEOUT_SECONDS = 10

_token_cache: Dict[str, Tuple[float, dict]] = {}
_token_cache_lock = threading.Lock()

# Role resolution reads the RBAC config on every call otherwise; cache briefly
# and invalidate explicitly whenever the config is written.
ROLE_CACHE_TTL_SECONDS = 60
_role_cache: Dict[str, Tuple[float, str]] = {}
_role_cache_lock = threading.Lock()


# ============================================================================
# BUILT-IN ROLE DEFINITIONS
# ----------------------------------------------------------------------------
# These are the canonical definitions. The frontend's copy in RBACContext.jsx
# is for rendering only and must not be trusted for access control.
# ============================================================================

ALL_PERMISSIONS = [
    "viewDashboard", "viewDQE", "viewITChecks", "runITChecks",
    "viewDataValidation", "runDataValidation", "viewBusinessChecks", "viewIndCtaQuality", "viewIndCtaSql", "viewSafetyDQ",
    "viewFuzzyMatch", "viewData", "viewMetadata", "viewAIChatbot",
    "viewIngestionLab", "runIngestionLab", "viewRAG", "viewEPodRAG", "viewRimDocsRAG",
    "viewRimDocsAgentic", "viewUnstructured", "viewUnstructuredSQL", "downloadUnstructuredData",
    "viewUnstructuredRimDocsTotals",
    "viewDataStudio", "runSnowflakeQueryRunner",
    "useAllSnowflakeRoles", "writeSnowflake", "copySnowflakeTables",
    "runPostgresStudio", "runS3Explorer", "runStreamsetsExtractor", "runDataShuttle",
    "viewStreamsetsSecrets", "viewGraDataDomains",
    "editGraDataDomains", "viewAudit", "viewUserManagement",
    "manageUsers", "manageRoles", "runBDCOperations", "viewBDCSuperuser",
    "viewBDCExecutive", "viewBDCStandard", "downloadBDCData",
    "viewAssistantAvatar", "manageDataAPIExports", "viewPGindexer", "buildPGindexer",
    "sendCommunications",
    "viewUnstructuredVR", "viewUnstructuredIMMBOT", "viewUnstructuredMirAI",
    "viewUnstructuredRMWCreate", "viewUnstructuredHAQ", "viewUnstructuredNipo",
    "viewUnstructuredRimDocs", "viewUnstructuredEPOD", "viewUnstructuredFDA",
    "viewUnstructuredVRInternal", "viewUnstructuredVRExternal", "viewUnstructuredVRCortellis",
    "viewUnstructuredIMMBOTExplore", "viewUnstructuredIMMBOTTable", "viewUnstructuredIMMBOTBreakdown",
    "viewUnstructuredRimDocsExplore", "viewUnstructuredRimDocsTable", "viewUnstructuredRimDocsBreakdown",
    "viewUnstructuredEPODExplore", "viewUnstructuredEPODTable",
    "viewUnstructuredNipoExplore", "viewUnstructuredNipoTable",
    "viewUnstructuredFDAExplore", "viewUnstructuredFDATable",
]

# One per document-set tab on the Unstructured Explorer.
UNSTRUCTURED_TABS = (
    "viewUnstructuredVR", "viewUnstructuredIMMBOT", "viewUnstructuredMirAI",
    "viewUnstructuredRMWCreate", "viewUnstructuredHAQ", "viewUnstructuredNipo",
    "viewUnstructuredRimDocs", "viewUnstructuredEPOD", "viewUnstructuredFDA",
)

# Sub-tabs inside each document-set tab.
UNSTRUCTURED_SUBTABS: Dict[str, Tuple[str, ...]] = {
    "viewUnstructuredVR": ("viewUnstructuredVRInternal", "viewUnstructuredVRExternal", "viewUnstructuredVRCortellis"),
    "viewUnstructuredIMMBOT": ("viewUnstructuredIMMBOTExplore", "viewUnstructuredIMMBOTTable", "viewUnstructuredIMMBOTBreakdown"),
    "viewUnstructuredRimDocs": ("viewUnstructuredRimDocsExplore", "viewUnstructuredRimDocsTable", "viewUnstructuredRimDocsBreakdown"),
    "viewUnstructuredEPOD": ("viewUnstructuredEPODExplore", "viewUnstructuredEPODTable"),
    "viewUnstructuredNipo": ("viewUnstructuredNipoExplore", "viewUnstructuredNipoTable"),
    "viewUnstructuredFDA": ("viewUnstructuredFDAExplore", "viewUnstructuredFDATable"),
}

# parent -> children added after roles were first saved. Parents come first,
# so a tab is filled in before its own sub-tabs look at it.
# Snowflake sub permissions. Roles saved before these existed get them OFF,
# except admin which gets them ON (see with_snowflake_subperms).
SNOWFLAKE_SUBPERMS = ("useAllSnowflakeRoles", "writeSnowflake", "copySnowflakeTables")

UNSTRUCTURED_GROUPS: Dict[str, Tuple[str, ...]] = {"viewUnstructured": UNSTRUCTURED_TABS, **UNSTRUCTURED_SUBTABS}


# Where each permission sits in the tree the User Management page draws (its
# PERMISSION_GROUPS). Top-level permissions have no entry. Per-user grants use
# this so a sub-feature can't be switched on without the page it lives in.
PERMISSION_PARENTS: Dict[str, str] = {
    "viewITChecks": "viewDQE",
    "runITChecks": "viewITChecks",
    "runIngestionLab": "viewIngestionLab",
    "viewDataValidation": "viewDQE",
    "runDataValidation": "viewDataValidation",
    "viewBusinessChecks": "viewDQE",
    "viewBDCExecutive": "viewBusinessChecks",
    "viewBDCStandard": "viewBusinessChecks",
    "runBDCOperations": "viewBDCStandard",
    "viewBDCSuperuser": "viewBDCStandard",
    "downloadBDCData": "viewBDCStandard",
    "viewIndCtaQuality": "viewDQE",
    "viewIndCtaSql": "viewIndCtaQuality",
    "viewSafetyDQ": "viewDQE",
    "viewData": "viewFuzzyMatch",
    "viewMetadata": "viewFuzzyMatch",
    "viewUnstructuredSQL": "viewUnstructured",
    "downloadUnstructuredData": "viewUnstructured",
    "viewUnstructuredRimDocsTotals": "viewUnstructuredRimDocsExplore",
    **{tab: "viewUnstructured" for tab in UNSTRUCTURED_TABS},
    **{sub: tab for tab, subs in UNSTRUCTURED_SUBTABS.items() for sub in subs},
    "runSnowflakeQueryRunner": "viewDataStudio",
    **{p: "runSnowflakeQueryRunner" for p in SNOWFLAKE_SUBPERMS},
    "runPostgresStudio": "viewDataStudio",
    "runS3Explorer": "viewDataStudio",
    "runStreamsetsExtractor": "viewDataStudio",
    "runDataShuttle": "viewDataStudio",
    "viewStreamsetsSecrets": "runStreamsetsExtractor",
    "viewPGindexer": "viewDataStudio",
    "buildPGindexer": "viewPGindexer",
    "editGraDataDomains": "viewGraDataDomains",
    "viewEPodRAG": "viewRAG",
    "viewRimDocsRAG": "viewRAG",
    "viewRimDocsAgentic": "viewRAG",
    "manageUsers": "viewUserManagement",
    "manageRoles": "viewUserManagement",
}


def _depth(permission: str) -> int:
    depth = 0
    while permission in PERMISSION_PARENTS:
        permission = PERMISSION_PARENTS[permission]
        depth += 1
    return depth


# Parents always come before their children, so one pass can check a parent's
# final value before deciding a child's.
PERMISSIONS_TREE_ORDER: List[str] = sorted(ALL_PERMISSIONS, key=_depth)


def _perms(**granted: bool) -> Dict[str, bool]:
    """Build a full permission map, defaulting everything not named to False."""
    base = {p: False for p in ALL_PERMISSIONS}
    unknown = set(granted) - set(base)
    if unknown:
        raise ValueError(f"Unknown permission(s): {sorted(unknown)}")
    base.update(granted)
    return base


BUILTIN_ROLES: Dict[str, Dict[str, Any]] = {
    "admin": {
        "name": "Admin",
        "description": "Full access to all features including user management",
        "color": "#eb1700",
        "permissions": {p: True for p in ALL_PERMISSIONS},
        "bdc_datasets": ["*"],  # default only; untick it and admins are limited like anyone
    },
    "dqe_righttime_superuser": {
        "name": "DQE - RightTime Super User",
        "description": "Full Access to data quality engine business defined checks",
        "color": "#328714",
        "permissions": _perms(
            viewDashboard=True, viewDQE=True, viewBusinessChecks=True,
            runBDCOperations=True, viewBDCSuperuser=True, viewBDCExecutive=True, viewBDCStandard=True, downloadBDCData=True,
        ),
    },
    "it_viewer": {
        "name": "DQE - IT User",
        "description": "Read-only access to data quality engine business defined checks and IT checks",
        "color": "#ff6017",
        "permissions": _perms(
            viewDashboard=True, viewDQE=True, viewITChecks=True,
            viewDataValidation=True, viewBusinessChecks=True, viewBDCExecutive=True, viewBDCStandard=True, downloadBDCData=True,
            viewIndCtaQuality=True,
        ),
    },
    "viewer": {
        "name": "No access",
        "description": "Read-only access to dashboard",
        "color": "#a39992",
        "permissions": _perms(viewDashboard=True),
    },
}

DEFAULT_ROLE = "viewer"

# Highest priority first — matches the frontend's rolesPriority ordering.
AD_GROUP_PRIORITY = ["admin", "dqe_righttime_superuser", "it_viewer"]

# AD group display names / object IDs, supplied per environment.
#
# Resolved from two sources, in order of precedence:
#   1. `ad_groups` section of backend/dq_config.json (wired in from app.py)
#   2. AD_GROUP_* environment variables
#
# dq_config.json is listed first because that is where the rest of this
# application's per-environment settings already live; the env vars remain
# supported for container deployments that would rather not mount the file.
AD_GROUP_ROLES: Dict[str, str] = {}

_ENV_VAR_BY_ROLE = {
    "admin": "AD_GROUP_ADMIN",
    "dqe_righttime_superuser": "AD_GROUP_DQE_RIGHTTIME_SUPERUSER",
    "it_viewer": "AD_GROUP_IT_VIEWER",
}


def configure_ad_groups(config_section: Optional[Dict[str, str]] = None, quiet: bool = False) -> None:
    AD_GROUP_ROLES.clear()

    for role, env_var in _ENV_VAR_BY_ROLE.items():
        group = None
        if config_section:
            group = config_section.get(role)
        if not group:
            group = os.environ.get(env_var)
        if group:
            AD_GROUP_ROLES[group] = role

    if not AD_GROUP_ROLES:
        if not quiet:
            logger.warning(
                "No AD group mappings configured — every user without an explicit "
                "role assigned in User Management will resolve to '%s'. Set the "
                "'ad_groups' section of dq_config.json or the AD_GROUP_* "
                "environment variables.",
                DEFAULT_ROLE,
            )
    else:
        logger.info(f"AD group mappings loaded for roles: {sorted(AD_GROUP_ROLES.values())}")

    invalidate_role_cache()



# ============================================================================
# RBAC CONFIG ACCESS
# ----------------------------------------------------------------------------
# Injected by app.py at startup so this module doesn't import app.py (which
# would be circular). Expected signature: () -> dict
# ============================================================================

_load_rbac_config: Optional[Callable[[], dict]] = None


def set_rbac_config_loader(loader: Callable[[], dict]) -> None:
    global _load_rbac_config
    _load_rbac_config = loader


def _rbac_config() -> dict:
    if _load_rbac_config is None:
        logger.error("RBAC config loader not wired — denying elevated access")
        return {"custom_roles": {}, "user_roles": {}, "user_role_overrides": {}, "user_permission_overrides": {}}
    try:
        return _load_rbac_config() or {}
    except Exception as e:
        logger.error(f"Failed to load RBAC config: {e}")
        # Fail closed: no overrides, no custom roles → everyone falls back to
        # their AD group or 'viewer'. Never fail open to admin.
        return {"custom_roles": {}, "user_roles": {}, "user_role_overrides": {}, "user_permission_overrides": {}}


def invalidate_role_cache(email: Optional[str] = None) -> None:
    """Call after any RBAC write so permission changes take effect immediately."""
    with _role_cache_lock:
        if email:
            _role_cache.pop(email.lower(), None)
        else:
            _role_cache.clear()


# ============================================================================
# TOKEN VALIDATION
# ============================================================================

def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _cached_identity(token: str) -> Optional[dict]:
    key = _token_key(token)
    now = time.time()
    with _token_cache_lock:
        entry = _token_cache.get(key)
        if entry and entry[0] > now:
            return entry[1]
        if entry:
            del _token_cache[key]
    return None


def _cache_identity(token: str, user: dict) -> None:
    key = _token_key(token)
    with _token_cache_lock:
        # Opportunistic sweep so the cache can't grow without bound.
        if len(_token_cache) > 2000:
            now = time.time()
            for k in [k for k, v in _token_cache.items() if v[0] <= now]:
                del _token_cache[k]
        _token_cache[key] = (time.time() + TOKEN_CACHE_TTL_SECONDS, user)


def _extract_bearer(authorization: Optional[str]) -> str:
    # Same reasoning as the x_api_key check in require_auth: a non-str here
    # means the header was never resolved, which is a 401, not a 500 on .split().
    if not authorization or not isinstance(authorization, str):
        raise HTTPException(status_code=401, detail="Authorization required")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    return parts[1]


def _validate_token_with_graph(token: str) -> dict:
    """Blocking. Call via asyncio.to_thread from async code."""
    try:
        response = _graph_session.get(
            f"{GRAPH_API_ENDPOINT}/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=GRAPH_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as e:
        logger.warning(f"Graph token validation unreachable: {type(e).__name__}")
        raise HTTPException(status_code=503, detail="Identity provider unavailable")

    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    data = response.json()
    return {
        "id": data.get("id"),
        "username": data.get("userPrincipalName"),
        "name": data.get("displayName"),
        "email": (data.get("mail") or data.get("userPrincipalName") or "").lower(),
    }


MAX_GROUP_PAGES = 10


def _read_member_of(token: str, path: str) -> List[dict]:
    """Every group on a memberOf listing. Graph pages at 100 by default, so
    people in lots of groups used to lose the mapped one off page two.
    Raises on any failure so callers can tell 'no groups' from 'don't know'."""
    url = f"{GRAPH_API_ENDPOINT}{path}?$select=id,displayName&$top=999"
    groups: List[dict] = []
    for _ in range(MAX_GROUP_PAGES):
        response = _graph_session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=GRAPH_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise RuntimeError(f"memberOf returned {response.status_code}")
        data = response.json()
        groups.extend(data.get("value") or [])
        url = data.get("@odata.nextLink")
        if not url:
            break
    return groups


def _fetch_ad_groups(token: str) -> Optional[List[dict]]:
    """Group membership for the caller. None means the lookup failed, which
    is not the same as having no groups: it must not get cached."""
    try:
        return _read_member_of(token, "/me/memberOf")
    except (requests.exceptions.RequestException, RuntimeError, ValueError) as e:
        logger.warning(f"Group fetch failed: {e}")
        return None


def fetch_user_groups(token: str, email: str) -> List[dict]:
    """Someone else's groups, read with the caller's token. Someone who isn't
    in the directory has no groups. Anything else going wrong raises."""
    from urllib.parse import quote
    try:
        return _read_member_of(token, f"/users/{quote(email, safe='@')}/memberOf")
    except RuntimeError as e:
        if "404" in str(e):
            return []
        raise


# ============================================================================
# ROLE + PERMISSION RESOLUTION
# ============================================================================

def _role_exists(role: str, config: dict) -> bool:
    return role in BUILTIN_ROLES or role in (config.get("custom_roles") or {})


def _ad_role(groups: Optional[List[dict]]) -> Optional[str]:
    if not groups:
        return None
    group_names = {g.get("displayName") for g in groups if g.get("displayName")}
    group_ids = {g.get("id") for g in groups if g.get("id")}
    for candidate in AD_GROUP_PRIORITY:
        for ad_group, mapped in AD_GROUP_ROLES.items():
            if mapped == candidate and (ad_group in group_names or ad_group in group_ids):
                return candidate
    return None


def user_roles_entry(config: dict, email: str) -> Optional[dict]:
    """Someone's hand-given roles: {"roles": [...], "keep_ad": bool}, or None.
    Older configs only have user_role_overrides (one role, AD ignored)."""
    email_lc = (email or "").lower()
    entry = (config.get("user_roles") or {}).get(email_lc)
    if entry is None and "user_roles" not in config:
        legacy = (config.get("user_role_overrides") or {}).get(email_lc)
        entry = {"roles": [legacy], "keep_ad": False} if legacy else None
    return entry


def primary_role(roles: List[str]) -> str:
    """One role to show or log for someone holding several: admin if they
    have it, otherwise the first one."""
    if not roles:
        return DEFAULT_ROLE
    return "admin" if "admin" in roles else roles[0]


def resolve_roles_from_config(email: str, groups: Optional[List[dict]], config: dict) -> List[str]:
    """Every role someone holds, no cache.
    1. roles given by hand (ones that still exist), plus their AD group's
       role when keep_ad is on
    2. no hand-given roles: their AD group's role
    3. neither: DEFAULT_ROLE
    """
    entry = user_roles_entry(config, email) or {}
    manual = []
    for r in entry.get("roles") or []:
        # An assignment pointing at a role that's gone falls away, same as the page shows it
        if isinstance(r, str) and _role_exists(r, config) and r not in manual:
            manual.append(r)
    ad = _ad_role(groups)
    if manual:
        if entry.get("keep_ad") and ad and ad not in manual:
            manual.append(ad)
        return manual
    return [ad] if ad else [DEFAULT_ROLE]


def resolve_roles(email: str, groups: Optional[List[dict]] = None, cache: bool = True) -> List[str]:
    """resolve_roles_from_config against the live store, cached briefly.
    cache=False when the groups couldn't be read, so a Graph blip doesn't pin
    someone to the default role for the cache window."""
    email_lc = (email or "").lower()

    if cache:
        with _role_cache_lock:
            entry = _role_cache.get(email_lc)
            if entry and entry[0] > time.time():
                return list(entry[1])

    roles = resolve_roles_from_config(email_lc, groups, _rbac_config())

    if cache:
        with _role_cache_lock:
            _role_cache[email_lc] = (time.time() + ROLE_CACHE_TTL_SECONDS, tuple(roles))
    return roles


def resolve_role(email: str, groups: Optional[List[dict]] = None, cache: bool = True) -> str:
    return primary_role(resolve_roles(email, groups, cache))


DATA_STUDIO_TOOLS = ("runSnowflakeQueryRunner", "runS3Explorer", "viewPGindexer", "runStreamsetsExtractor", "runDataShuttle")


def with_data_studio_access(perms: Dict[str, Any]) -> Dict[str, Any]:
    """Roles saved before Data Studio had its own section only have the tool
    permissions (they sat under Unstructured Explorer). Give those roles page
    access too, so nobody loses Data Studio on upgrade. Once a role is saved
    again it carries viewDataStudio itself and this does nothing."""
    if "viewDataStudio" in perms:
        return perms
    return {**perms, "viewDataStudio": any(perms.get(p) is True for p in DATA_STUDIO_TOOLS)}


def with_bdc_downloads(perms: Dict[str, Any]) -> Dict[str, Any]:
    """Roles saved before downloads had their own permission could always
    download; they keep that until the role is saved again."""
    if "downloadBDCData" in perms:
        return perms
    return {**perms, "downloadBDCData": perms.get("viewBusinessChecks") is True}


def with_bdc_standard(perms: Dict[str, Any]) -> Dict[str, Any]:
    """Roles saved before Standard View had its own permission could always
    open it, so they keep it until the role is saved again."""
    if "viewBDCStandard" in perms:
        return perms
    return {**perms, "viewBDCStandard": perms.get("viewBusinessChecks") is True}


IND_CTA_QUALITY_ROLES = ("admin", "it_viewer")


def with_ind_cta_quality(perms: Dict[str, Any], role: str) -> Dict[str, Any]:
    """Roles saved before the IND/CTA Data Quality page existed: only admin and
    IT viewer get it. Anyone else needs it ticked in User Management."""
    if "viewIndCtaQuality" not in perms:
        perms = {**perms, "viewIndCtaQuality": role in IND_CTA_QUALITY_ROLES and perms.get("viewDQE") is True}
    # View SQL came later. Admin keeps it, everyone else has to be given it.
    if "viewIndCtaSql" not in perms:
        perms = {**perms, "viewIndCtaSql": role == "admin" and perms.get("viewIndCtaQuality") is True}
    # Safety is admin only until ticked in User Management
    if "viewSafetyDQ" not in perms:
        perms = {**perms, "viewSafetyDQ": role == "admin" and perms.get("viewDQE") is True}
    return perms


def with_unstructured_downloads(perms: Dict[str, Any], role: str) -> Dict[str, Any]:
    """Roles saved before Unstructured downloads had their own permission:
    admin keeps downloading, every other role loses it until granted."""
    if "viewUnstructuredRimDocsTotals" not in perms:
        # Same rule for RIMDocs total size / ingested %: admin keeps it.
        perms = {**perms, "viewUnstructuredRimDocsTotals": role == "admin" and perms.get("viewUnstructuredRimDocs", perms.get("viewUnstructured")) is True}
    if "downloadUnstructuredData" in perms:
        return perms
    return {**perms, "downloadUnstructuredData": role == "admin" and perms.get("viewUnstructured") is True}


def with_unstructured_tabs(perms: Dict[str, Any]) -> Dict[str, Any]:
    """Roles saved before the per-tab / sub-tab permissions existed: children
    follow their parent, so nobody loses a tab on upgrade."""
    perms = dict(perms)
    for parent, kids in UNSTRUCTURED_GROUPS.items():
        if not any(k in perms for k in kids):
            perms.update({k: perms.get(parent) is True for k in kids})
    # Cortellis was part of External before it got its own permission
    perms.setdefault("viewUnstructuredVRCortellis", perms.get("viewUnstructuredVRExternal") is True)
    return perms


def with_snowflake_subperms(perms: Dict[str, Any], role: str) -> Dict[str, Any]:
    """Roles saved before the Snowflake sub permissions existed: admin gets
    them on, every other role gets them off. Once saved again this does nothing."""
    missing = [p for p in SNOWFLAKE_SUBPERMS if p not in perms]
    if not missing:
        return perms
    on = role == "admin" and perms.get("runSnowflakeQueryRunner") is True
    return {**perms, **{p: on for p in missing}}


def get_role_permissions(role: str, config: Optional[dict] = None) -> Dict[str, bool]:
    """Custom roles (and custom overrides of built-ins) win over defaults."""
    if config is None:
        config = _rbac_config()
    custom = (config.get("custom_roles") or {}).get(role)
    if custom and isinstance(custom.get("permissions"), dict):
        merged = {p: False for p in ALL_PERMISSIONS}
        stored = with_unstructured_downloads(with_snowflake_subperms(
            with_ind_cta_quality(with_bdc_standard(with_bdc_downloads(with_unstructured_tabs(with_data_studio_access(custom["permissions"])))), role), role), role)
        merged.update({k: bool(v) for k, v in stored.items() if k in merged})
        return merged
    definition = BUILTIN_ROLES.get(role)
    if not definition:
        logger.warning(f"Unknown role '{role}' — falling back to {DEFAULT_ROLE}")
        definition = BUILTIN_ROLES[DEFAULT_ROLE]
    return dict(definition["permissions"])


def normalize_user_overrides(add: Any, remove: Any) -> Tuple[List[str], List[str]]:
    """Validate a per-user grant/revoke pair from the API. Raises ValueError on
    unknown permissions or a permission that is both granted and revoked.
    Returns both lists de-duplicated and in tree order, so what gets stored is
    stable no matter how the client ordered it."""
    for value in (add, remove):
        if value is not None and (not isinstance(value, list) or not all(isinstance(p, str) for p in value)):
            raise ValueError("add and remove must be lists of permission names")
    add_set = set(add or [])
    remove_set = set(remove or [])
    unknown = (add_set | remove_set) - set(ALL_PERMISSIONS)
    if unknown:
        raise ValueError(f"Unknown permission(s): {sorted(unknown)}")
    both = add_set & remove_set
    if both:
        raise ValueError(f"Permission(s) both granted and removed: {sorted(both)}")
    ordered = lambda chosen: [p for p in PERMISSIONS_TREE_ORDER if p in chosen]
    return ordered(add_set), ordered(remove_set)


ROLE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate_role_definition(definition: Any) -> Dict[str, Any]:
    """Check a role coming in from the API and return a clean copy.

    Keeps only name, description, color and permissions. Permissions come back
    as a full map of every known key, and a sub-permission whose parent is off
    is switched off too, so a stored role can never hold a state the page
    can't draw. Raises ValueError with a message fit to show the user."""
    if not isinstance(definition, dict):
        raise ValueError("Role definition must be an object")

    name = definition.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Role name is required")
    name = name.strip()
    if len(name) > 60:
        raise ValueError("Role name can be at most 60 characters")

    description = definition.get("description") or ""
    if not isinstance(description, str):
        raise ValueError("Description must be text")
    description = description.strip()
    if len(description) > 240:
        raise ValueError("Description can be at most 240 characters")

    color = definition.get("color") or "#a39992"
    if not isinstance(color, str) or not _COLOR_PATTERN.match(color):
        raise ValueError("Colour must look like #RRGGBB")

    perms_in = definition.get("permissions")
    if not isinstance(perms_in, dict):
        raise ValueError("Permissions must be an object")
    unknown = set(perms_in) - set(ALL_PERMISSIONS)
    if unknown:
        raise ValueError(f"Unknown permission(s): {sorted(unknown)}")
    if any(not isinstance(v, bool) for v in perms_in.values()):
        raise ValueError("Permission values must be true or false")

    perms = {p: perms_in.get(p) is True for p in ALL_PERMISSIONS}
    for p in PERMISSIONS_TREE_ORDER:
        parent = PERMISSION_PARENTS.get(p)
        if parent and not perms[parent]:
            perms[p] = False

    return {"name": name, "description": description, "color": color.lower(), "permissions": perms,
            "bdc_datasets": normalize_datasets(definition.get("bdc_datasets"))}


# ============================================================================
# BDC DATASET ACCESS
# ----------------------------------------------------------------------------
# Which DATASET_TITLEs (DQ_RULE_INPUT) someone sees on Business Defined Checks.
# Roles carry "bdc_datasets", people can get extra ones on their custom access.
# "*" means every dataset. Nothing assigned means nothing shown.
# No exemptions for people: admins and superusers get only what their roles
# and custom access give (the admin role starts with "*"). API keys (cron
# jobs, no person behind them) see everything.
# ============================================================================

ALL_DATASETS = "*"
# Stands for rules whose DATASET_TITLE is empty, so they can be given out too
NO_DATASET = "(No dataset)"


def dataset_key(title: Any) -> str:
    """What a rule's DATASET_TITLE counts as for access: blank is NO_DATASET."""
    return title if isinstance(title, str) and title.strip() else NO_DATASET


def normalize_datasets(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(d, str) for d in value):
        raise ValueError("Datasets must be a list of names")
    out = list(dict.fromkeys(d.strip() for d in value if d and d.strip()))
    if len(out) > 500 or any(len(d) > 200 for d in out):
        raise ValueError("Too many datasets, or a dataset name over 200 characters")
    return [ALL_DATASETS] if ALL_DATASETS in out else sorted(out)


def get_role_datasets(role: str, config: dict) -> List[str]:
    builtin = BUILTIN_ROLES.get(role) or {}
    custom = (config.get("custom_roles") or {}).get(role)
    # A built-in customised before datasets existed keeps the built-in default
    if custom and "bdc_datasets" in custom:
        return list(custom.get("bdc_datasets") or [])
    return list(builtin.get("bdc_datasets") or [])


def datasets_from_config(email: str, roles, permissions: Dict[str, Any], config: dict) -> Optional[set]:
    """None = every dataset, otherwise the set they may see (can be empty)."""
    out = set()
    for r in roles or []:
        out.update(get_role_datasets(r, config))
    out.update((get_user_overrides(email, config) or {}).get("datasets") or [])
    return None if ALL_DATASETS in out else out


def bdc_datasets(auth: dict) -> Optional[set]:
    if auth.get("is_api_key"):
        return None
    return datasets_from_config(auth["user"].get("email") or "", auth.get("roles") or [auth.get("role")],
                                auth.get("permissions") or {}, _rbac_config())


def sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def dataset_sql(auth: dict, column: str) -> str:
    """' AND <column> IN (...)', or '' when unrestricted. Literals, not params,
    because the queries this goes into already carry LIKE 'DELTA_%'."""
    allowed = bdc_datasets(auth)
    if allowed is None:
        return ""
    if not allowed:
        return " AND 1 = 0"
    parts = []
    names = sorted(allowed - {NO_DATASET})
    if names:
        parts.append(f"{column} IN ({', '.join(sql_literal(d) for d in names)})")
    if NO_DATASET in allowed:
        parts.append(f"NULLIF(TRIM({column}), '') IS NULL")
    return f" AND ({' OR '.join(parts)})"


def resolve_role_from_config(email: str, groups: Optional[List[dict]], config: dict) -> str:
    return primary_role(resolve_roles_from_config(email, groups, config))


def get_roles_permissions(roles, config: Optional[dict] = None) -> Dict[str, bool]:
    """Everything any of the roles gives. Each role is a valid tree on its
    own, so the union is too (a child is only on where its parent is)."""
    if config is None:
        config = _rbac_config()
    if isinstance(roles, str):
        roles = [roles]
    out = {p: False for p in ALL_PERMISSIONS}
    for r in roles or [DEFAULT_ROLE]:
        for p, v in get_role_permissions(r, config).items():
            if v is True:
                out[p] = True
    return out


def effective_permissions_from_config(email: str, groups: Optional[List[dict]], config: dict) -> Dict[str, bool]:
    roles = resolve_roles_from_config(email, groups, config)
    return apply_user_overrides(get_roles_permissions(roles, config), get_user_overrides(email, config))


def apply_user_overrides(perms: Dict[str, bool], overrides: Optional[dict]) -> Dict[str, bool]:
    """Layer one person's custom access on top of their role.

    `add` grants permissions the role doesn't, but only while the parent is
    held. `remove` takes permissions away, along with everything under them.
    Permissions the overrides don't touch keep the role's value as-is, so a
    user with no overrides gets exactly their role and nothing is re-shaped.
    UserManagement.jsx (effectiveAccess) mirrors this rule for display.
    """
    if not overrides:
        return perms
    add = set(overrides.get("add") or [])
    remove = set(overrides.get("remove") or [])
    if not add and not remove:
        return perms
    # ponytail: overrides saved before per-tab perms existed - granting a parent
    # meant all its children. Same rule as with_unstructured_tabs.
    for parent, kids in UNSTRUCTURED_GROUPS.items():
        if parent in add and not (add | remove) & set(kids):
            add |= set(kids)
    if "viewUnstructuredVRExternal" in add and "viewUnstructuredVRCortellis" not in add | remove:
        add.add("viewUnstructuredVRCortellis")  # same rule as with_unstructured_tabs

    out = dict(perms)
    cut = set()  # switched off by an override, directly or through a parent
    for p in PERMISSIONS_TREE_ORDER:
        parent = PERMISSION_PARENTS.get(p)
        if p in remove or (parent in cut and out.get(p)):
            out[p] = False
            cut.add(p)
        elif p in add:
            out[p] = parent is None or out.get(parent) is True
    return out


def get_user_overrides(email: str, config: Optional[dict] = None) -> Optional[dict]:
    if config is None:
        config = _rbac_config()
    return (config.get("user_permission_overrides") or {}).get((email or "").lower())


def get_effective_permissions(email: str, roles) -> Dict[str, bool]:
    """What this person can actually do: all their roles plus any custom
    access. Takes one role or a list. Reads the RBAC store once."""
    config = _rbac_config()
    return apply_user_overrides(get_roles_permissions(roles, config), get_user_overrides(email, config))


def holds_everything(permissions: Dict[str, Any]) -> bool:
    return all(permissions.get(p) is True for p in ALL_PERMISSIONS)


def get_all_roles() -> Dict[str, Any]:
    """Built-ins merged with custom/overridden definitions — for /api/rbac/config."""
    config = _rbac_config()
    roles = {rid: dict(rdef) for rid, rdef in BUILTIN_ROLES.items()}
    roles.update(config.get("custom_roles") or {})
    return roles


# ============================================================================
# API KEYS (non-interactive callers)
# ----------------------------------------------------------------------------
# Cron jobs and wrappers can't hold an Entra user token — Graph /me needs a
# signed-in user. They send a pre-shared key in X-API-Key instead, which maps
# to a fixed, deliberately narrow permission set. These are NOT roles
# ============================================================================

_api_keys: Dict[str, dict] = {}


def configure_api_keys(section: Optional[Dict[str, Any]] = None) -> None:
    """Load API keys from the 'api_keys' section of dq_config.json:

        "api_keys": {
          "dqe-report-cron": {
            "key": "<long random string>",
            "permissions": ["runITChecks", "runBDCOperations"]
          }
        }

    A key may also come from the SMARTHUB_API_KEY env var, which registers a
    single key named 'env-key' with the same two permissions. The env var wins
    nothing — it is only a fallback when no config section exists.
    """
    global _api_keys
    keys: Dict[str, dict] = {}

    for name, spec in (section or {}).items():
        secret = (spec or {}).get("key", "")
        if not secret:
            logger.warning(f"API key '{name}' has no key value — skipped")
            continue
        perms = list((spec or {}).get("permissions") or [])
        unknown = set(perms) - set(ALL_PERMISSIONS)
        if unknown:
            raise ValueError(
                f"API key '{name}' names unknown permission(s): {sorted(unknown)}"
            )
        keys[name] = {"key": secret, "permissions": perms}

    if not keys:
        env_key = os.getenv("SMARTHUB_API_KEY", "").strip()
        if env_key:
            keys["env-key"] = {
                "key": env_key,
                "permissions": ["runITChecks", "runBDCOperations"],
            }

    _api_keys = keys
    if keys:
        logger.info(f"API keys loaded: {sorted(keys)}")


def _identity_from_api_key(presented: str) -> Optional[dict]:
    """Return an auth dict if the key matches a configured one, else None.

    Compared with compare_digest against every key so a wrong key takes the
    same time as a right one.
    """
    matched = None
    for name, entry in _api_keys.items():
        if hmac.compare_digest(entry["key"], presented):
            matched = (name, entry)

    if not matched:
        return None

    name, entry = matched
    granted = {p: False for p in ALL_PERMISSIONS}
    for p in entry["permissions"]:
        granted[p] = True

    return {
        "user": {
            "email": f"{name}@apikey.local",
            "name": name,
            "id": f"apikey:{name}",
        },
        "token": "",
        "role": "api_key",
        "roles": ["api_key"],
        "permissions": granted,
        "is_api_key": True,
    }


# ============================================================================
# FASTAPI DEPENDENCIES
# ============================================================================

async def require_auth(
        authorization: Optional[str] = Header(None),
        x_api_key: Optional[str] = Header(None),
) -> dict:
    """Authenticated caller. Returns {"user": {...}, "token": str, "role": str,
    "permissions": {...}}. Attach to every non-public route."""
    # API key first — a caller sending one has no user token to fall back on.
    # isinstance rather than a bare truthiness check: when this function is
    # called directly (not via Depends) any unpassed Header parameter arrives
    # as the Header object itself, which is truthy and has no .strip().
    if isinstance(x_api_key, str) and x_api_key.strip():
        identity = _identity_from_api_key(x_api_key.strip())
        if identity is None:
            logger.warning("401 — unrecognised X-API-Key")
            raise HTTPException(status_code=401, detail="Invalid API key")
        logger.info(f"API key auth: {identity['user']['name']}")
        return identity

    token = _extract_bearer(authorization)

    cached = _cached_identity(token)
    if cached:
        user, groups = cached["user"], cached["groups"]
    else:
        # Right after sign-in the frontend fires several calls at once (audit
        # login event, rbac config, profile photo) all carrying the same new
        # token. Without this, each one did its own pair of Graph round trips.
        # First-comer starts the lookup, everyone else awaits the same task.
        key = _token_key(token)
        task = _identity_inflight.get(key)
        if task is None:
            task = asyncio.create_task(_resolve_identity(token))
            _identity_inflight[key] = task
            task.add_done_callback(lambda _t, _k=key: _identity_inflight.pop(_k, None))
        # shield so one caller disconnecting mid-flight doesn't cancel the
        # lookup out from under the other waiters
        ident = await asyncio.shield(task)
        user, groups = ident["user"], ident["groups"]

    groups_known = groups is not None
    groups = groups or []
    roles = resolve_roles(user["email"], groups, cache=groups_known)
    return {
        "user": user,
        "token": token,
        "role": primary_role(roles),
        "roles": roles,
        "groups": groups,
        "groups_known": groups_known,
        "permissions": get_effective_permissions(user["email"], roles),
    }


async def _resolve_identity(token: str) -> dict:
    # /me and /me/memberOf don't depend on each other — run both at once.
    user, groups = await asyncio.gather(
        asyncio.to_thread(_validate_token_with_graph, token),
        asyncio.to_thread(_fetch_ad_groups, token),
    )
    ident = {"user": user, "groups": groups}
    # Failed group read: don't keep it, the next request tries again
    if groups is not None:
        _cache_identity(token, ident)
    return ident


# Cold-token lookups currently in flight, keyed the same way as the cache.
# Only touched from the event loop, so a plain dict is fine.
_identity_inflight: Dict[str, "asyncio.Task"] = {}


def require_permission(*permissions: str, require_all: bool = True):
    """Dependency factory. Denies with 403 unless the caller's role grants the
    named permission(s).

        Depends(require_permission("manageRoles"))
        Depends(require_permission("viewDQE", "viewITChecks", require_all=False))
    """
    unknown = set(permissions) - set(ALL_PERMISSIONS)
    if unknown:
        raise ValueError(f"Unknown permission(s): {sorted(unknown)}")

    async def _dependency(auth: dict = Depends(require_auth)) -> dict:
        held = auth["permissions"]
        check = all if require_all else any
        if not check(held.get(p) is True for p in permissions):
            logger.warning(
                f"403 for {auth['user']['email']} (role={auth['role']}) "
                f"— required {list(permissions)}"
            )
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return auth

    return _dependency


# Populate from environment at import time so the module is usable even if
# app.py never calls configure_ad_groups(). Placed at the end of the module
# because it depends on invalidate_role_cache() being defined. quiet=True
# because app.py calls this again right after with dq_config.json — warning
# here was a false alarm on every reload. app.py's call still warns if the
# config is genuinely missing.
configure_ad_groups(quiet=True)
configure_api_keys()