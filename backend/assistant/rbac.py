"""
Permission check for the assistant.

The assistant used to re-resolve roles itself (its own Graph call, its own
hardcoded copy of the role -> permission map in role_map.json). That was a
second, hand-maintained source of truth sitting next to auth.py's real one —
any time a role's permissions or AD group mapping changed in one place and
not the other, the assistant would silently grant or deny the wrong tools.

require_auth() in app.py already resolves role + permissions once per
request (with its own caching). The assistant's chat endpoint calls that
same dependency, so this module now only needs a one-line permission check
against the dict require_auth already produced — no separate Graph call,
no separate role map, nothing that can drift.
"""


def has(perms: dict, permission: str) -> bool:
    return perms.get(permission) is True