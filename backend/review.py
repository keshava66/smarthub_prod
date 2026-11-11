from __future__ import annotations

import logging
import re
import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Header, Depends

from auth import require_auth, require_permission, dataset_sql
from bdc_admin import allowed_rule_ids, rule_allowed, review_mode_sql, DELTA_PREFIX
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_bdc_config_manager      = None
_bdc_connection_manager  = None
_write_connection_factory = None

review_router = APIRouter(prefix="/api/review", tags=["Review"])

REVIEW_TABLE_NAME = "DQ_REVIEW_REGISTRY"
DEFAULT_CORE_SCHEMA = "DQ_CORE"


def _review_table() -> str:
    """Where the review registry lives.

    Fully qualified, deliberately. Writes here run on a connection opened from
    the *write* credentials, whose own default database need not be the one the
    read side uses - and this name used to carry only the schema, so the same
    table name resolved to two different databases and an accepted review could
    simply not be there when the read side looked for it.

    The schema is a setting rather than a constant now, and both parts are read
    when a statement is built rather than when this module is imported: the
    config manager is wired in afterwards."""
    if _bdc_config_manager is not None:
        try:
            return _bdc_config_manager.core_table(REVIEW_TABLE_NAME)
        except Exception:
            pass
    return f"{DEFAULT_CORE_SCHEMA}.{REVIEW_TABLE_NAME}"


def _create_table_sql() -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {_review_table()} (
    REVIEW_ID               VARCHAR(36)    NOT NULL,
    RULE_ID                 VARCHAR(200)   NOT NULL,
    RULE_TITLE              VARCHAR(500),
    STATUS                  VARCHAR(30)    NOT NULL,
    REVIEWED_BY             VARCHAR(300),
    REVIEWED_AT             TIMESTAMP_TZ   DEFAULT CURRENT_TIMESTAMP(),
    REVIEW_NOTE             VARCHAR(4000),
    SQL_PREDICATE_SNAPSHOT  VARCHAR(16000),
    FAILED_ROW_COUNT        INTEGER,
    PRIMARY KEY (REVIEW_ID)
)
"""


def set_review_dependencies(config_manager, connection_manager, write_connection_factory=None):
    global _bdc_config_manager, _bdc_connection_manager, _write_connection_factory
    _bdc_config_manager       = config_manager
    _bdc_connection_manager   = connection_manager
    _write_connection_factory = write_connection_factory
    if connection_manager and connection_manager.available:
        try:
            sf = connection_manager.get_client()
            sf.execute(_create_table_sql())
            logger.info("DQ_REVIEW_REGISTRY ensured")
        except Exception as e:
            logger.warning(f"Could not ensure DQ_REVIEW_REGISTRY: {e}")


def _sf():
    if not _bdc_connection_manager:
        raise HTTPException(status_code=503, detail="Review service not available")
    return _bdc_connection_manager.get_client()


def _sf_write():
    if _write_connection_factory:
        from app import SnowflakeClient
        return SnowflakeClient(_write_connection_factory())
    return _sf()


# REVIEWED_AT as an unambiguous UTC instant ("2026-05-01T14:03:09Z"). A bare
# "YYYY-MM-DD HH24:MI:SS" was read by browsers as their own local time while
# the page labels times ET, so anyone outside Eastern saw the wrong time. An
# NTZ column is read in the session time zone, the same zone
# CURRENT_TIMESTAMP() was written in, so the conversion is exact.
_UTC_ISO = "TO_CHAR(CONVERT_TIMEZONE('UTC', {col}::TIMESTAMP_TZ), 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"


def _to_dict(columns, row) -> dict:
    return {
        col: (val.isoformat() if hasattr(val, "isoformat") else val)
        for col, val in zip(columns, row)
    }


# Delta-mode reviews are stored under the same registry table, keyed by a
# "DELTA_" prefix on RULE_ID. This avoids a schema migration (no MODE column)
# while keeping quarterly and delta sign-off history fully independent.
# Quarterly-mode rows are stored exactly as before (no prefix), so existing
# review history keeps working unchanged.
_DELTA_PREFIX = DELTA_PREFIX


def _storage_rule_id(rule_id: str, delta_mode: bool) -> str:
    # Always prefix in delta mode, even for an id that already starts with
    # DELTA_: the readers join on 'DELTA_' || RULE_ID, so skipping it here
    # meant such a rule could never be approved in delta mode.
    return f"{_DELTA_PREFIX}{rule_id}" if delta_mode else rule_id


def _display_rule_id(stored_rule_id: str) -> str:
    if stored_rule_id and stored_rule_id.startswith(_DELTA_PREFIX):
        return stored_rule_id[len(_DELTA_PREFIX):]
    return stored_rule_id


class ReviewSubmitRequest(BaseModel):
    rule_id:                str
    rule_title:             Optional[str] = None
    status:                 str
    note:                   Optional[str] = None
    sql_predicate_snapshot: Optional[str] = None
    failed_row_count:       Optional[int] = None


class BulkApproveRequest(BaseModel):
    rule_ids: List[str] = Field(..., min_length=1, max_length=500)
    note:     Optional[str] = Field(None, max_length=4000)


class UndoRequest(BaseModel):
    review_ids: List[str] = Field(..., min_length=1, max_length=500)


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
UNDO_WINDOW_MINUTES = 15


@review_router.get("/badge")
def get_review_badge(
    delta_mode: bool = Query(False, description="When True, count pending Delta-mode reviews"),
    auth: dict = Depends(require_permission("viewBusinessChecks")),
):
    """Count of Business Defined Checks rules nobody has decided on yet: the
    number on the Review tab's badge. Same "latest decision wins" rule as the
    queue, so it matches the queue's Pending filter (rejected rules have been
    reviewed and are not counted). Pass delta_mode for the Delta-mode queue."""
    sf  = _sf()
    cfg = _bdc_config_manager.get_dq_config()
    latest_key = cfg["rule_latest_view"]
    if delta_mode:
        latest_key = latest_key + "_DELTA" if not latest_key.endswith("_DELTA") else latest_key
    rule_view = _bdc_config_manager.get_full_table_name(latest_key)
    id_expr = f"'{_DELTA_PREFIX}' || v.RULE_ID" if delta_mode else "v.RULE_ID"
    try:
        res = sf.execute(f"""
            WITH latest_reviews AS (
                SELECT RULE_ID, STATUS
                FROM {_review_table()}
                WHERE {review_mode_sql(delta_mode)}
                QUALIFY ROW_NUMBER() OVER (PARTITION BY RULE_ID ORDER BY REVIEWED_AT DESC) = 1
            )
            SELECT COUNT(DISTINCT v.RULE_ID)
            FROM {rule_view} v
            LEFT JOIN latest_reviews lr ON lr.RULE_ID = {id_expr}
            WHERE COALESCE(lr.STATUS, 'NOT_REVIEWED') = 'NOT_REVIEWED'{dataset_sql(auth, "v.DATASET_TITLE")}
        """)
        return {"pending": int(res.rows[0][0]) if res.rows else 0}
    except Exception as e:
        # A silent 0 read as "nothing to review"; say it failed instead.
        logger.warning(f"Badge query failed: {e}")
        raise HTTPException(status_code=502, detail="Could not count the rules awaiting review")


@review_router.get("/rules")
def get_review_rules(
    delta_mode: bool = Query(False, description="When True, show Delta-mode review queue"),
    auth: dict = Depends(require_permission("viewBusinessChecks")),
):
    """Business Defined Checks rules with their SQL predicate, AI output, and
    latest review status (NOT_REVIEWED/ACCEPTED/REJECTED) — the Review tab's queue."""
    sf  = _sf()
    cfg = _bdc_config_manager.get_dq_config()
    latest_key = cfg["rule_latest_view"]
    if delta_mode:
        latest_key = latest_key + "_DELTA" if not latest_key.endswith("_DELTA") else latest_key
    rule_view  = _bdc_config_manager.get_full_table_name(latest_key)
    rule_input = _bdc_config_manager.get_full_table_name(cfg["rule_input_table"])
    rule_reg   = _bdc_config_manager.get_full_table_name(cfg["rule_registry_table"])

    # Delta-mode reviews are stored in the same table keyed by a "DELTA_"
    # prefix on RULE_ID, so we only look at rows matching the current mode
    # and join back by adding/stripping that prefix.
    review_filter = review_mode_sql(delta_mode)
    join_id_expr  = f"'{_DELTA_PREFIX}' || v.RULE_ID" if delta_mode else "v.RULE_ID"

    # Use a CTE with QUALIFY to get the latest review per rule — avoids correlated
    # subqueries in the SELECT/ORDER BY which Snowflake does not support.
    res = sf.execute(f"""
        WITH latest_reviews AS (
            SELECT
                RULE_ID,
                STATUS,
                REVIEWED_BY,
                {_UTC_ISO.format(col="REVIEWED_AT")} AS REVIEWED_AT_STR,
                REVIEW_NOTE
            FROM {_review_table()}
            WHERE {review_filter}
            QUALIFY ROW_NUMBER() OVER (PARTITION BY RULE_ID ORDER BY REVIEWED_AT DESC) = 1
        )
        SELECT
            v.RULE_ID,
            v.RULE_TITLE,
            v.RULE_DESCRIPTION,
            v.SEVERITY,
            v.TARGET_TABLE,
            v.DATASET_TITLE,
            v.PASS_PERCENTAGE,
            v.STATUS                                     AS EXEC_STATUS,
            i.COLOR_COLUMNS,
            reg.SQL_PREDICATE,
            reg.AI_OUTPUT,
            COALESCE(lr.STATUS,       'NOT_REVIEWED') AS REVIEW_STATUS,
            lr.REVIEWED_BY                               AS LAST_REVIEWED_BY,
            lr.REVIEWED_AT_STR                           AS LAST_REVIEWED_AT,
            lr.REVIEW_NOTE                               AS LAST_REVIEW_NOTE
        FROM {rule_view} v
        LEFT JOIN {rule_input}    i   ON v.RULE_ID = i.RULE_ID  AND i.IS_ACTIVE   = TRUE
        LEFT JOIN {rule_reg}      reg ON v.RULE_ID = reg.RULE_ID AND reg.IS_VALIDATED = TRUE
        LEFT JOIN latest_reviews  lr  ON {join_id_expr} = lr.RULE_ID
        WHERE 1 = 1{dataset_sql(auth, "v.DATASET_TITLE")}
        ORDER BY
            CASE COALESCE(lr.STATUS, 'NOT_REVIEWED')
                WHEN 'NOT_REVIEWED' THEN 0
                WHEN 'REJECTED'     THEN 1
                WHEN 'ACCEPTED'     THEN 2
            END,
            v.RULE_ID
    """)

    return {
        "rules": [
            {
                "rule_id":          r[0],
                "rule_title":       r[1],
                "rule_description": r[2] or "",
                "severity":         r[3],
                "target_table":     r[4],
                "dataset_title":    r[5] or "",
                "pass_percentage":  float(r[6]) if r[6] is not None else 0.0,
                "no_result":        r[6] is None,
                "exec_status":      r[7],
                "color_columns":    r[8] or "",
                "sql_predicate":    r[9] or "",
                "ai_output":        r[10] or "",
                "review_status":    r[11],
                "last_reviewed_by": r[12],
                "last_reviewed_at": r[13],
                "last_review_note": r[14],
            }
            for r in res.rows
        ]
    }


def _rules_in_view(ids: List[str], delta_mode: bool) -> list:
    """(RULE_ID, RULE_TITLE, SQL_PREDICATE) for the ids present in this cadence's latest view."""
    cfg = _bdc_config_manager.get_dq_config()
    latest_key = cfg["rule_latest_view"]
    if delta_mode and not latest_key.endswith("_DELTA"):
        latest_key += "_DELTA"
    rule_view = _bdc_config_manager.get_full_table_name(latest_key)
    rule_reg = _bdc_config_manager.get_full_table_name(cfg["rule_registry_table"])
    placeholders = ", ".join(f"%(r{i})s" for i in range(len(ids)))
    return _sf().execute(f"""
        SELECT v.RULE_ID, v.RULE_TITLE, reg.SQL_PREDICATE
        FROM {rule_view} v
        LEFT JOIN {rule_reg} reg ON v.RULE_ID = reg.RULE_ID AND reg.IS_VALIDATED = TRUE
        WHERE v.RULE_ID IN ({placeholders})
        QUALIFY ROW_NUMBER() OVER (PARTITION BY v.RULE_ID ORDER BY v.RULE_ID) = 1
    """, {f"r{i}": r for i, r in enumerate(ids)}).rows


@review_router.post("/submit")
def submit_review(
    body:         ReviewSubmitRequest,
    auth: dict = Depends(require_permission("viewBDCSuperuser")),
    delta_mode:   bool          = Query(False, description="When True, record this as a Delta-mode review"),
):
    """Record a reviewer's Accept/Reject decision (with an optional note) for
    one Business Defined Checks rule. Reviewer identity comes from the caller's
    verified token, not a request field."""
    # Reviewer identity comes from the verified token — a review can no longer
    # be recorded against someone else's name.
    x_user_email = auth["user"]["email"]

    if body.status not in ("ACCEPTED", "REJECTED"):
        raise HTTPException(status_code=400, detail="status must be 'ACCEPTED' or 'REJECTED'")

    if body.status == "REJECTED" and not (body.note or "").strip():
        raise HTTPException(status_code=400, detail="A reason is required to reject a rule")

    if len(body.note or "") > 4000:
        raise HTTPException(status_code=400, detail="The note can be at most 4000 characters")
    if not rule_allowed(auth, body.rule_id):
        raise HTTPException(status_code=404, detail=f"Rule '{body.rule_id}' not found")

    # Title and predicate snapshot are read here, like approve-bulk, so the
    # record shows what was actually approved rather than what the client sent.
    found = _rules_in_view([body.rule_id], delta_mode)
    if not found:
        raise HTTPException(status_code=404, detail=f"Rule '{body.rule_id}' not found")
    _, title, predicate = found[0]

    stored_rule_id = _storage_rule_id(body.rule_id, delta_mode)
    review_id = str(uuid.uuid4())

    # Bound parameters: the old hand-escaped literals only doubled quotes, and a
    # backslash in a note could end the string early.
    _sf_write().execute(f"""
        INSERT INTO {_review_table()}
            (REVIEW_ID, RULE_ID, RULE_TITLE, STATUS,
             REVIEWED_BY, REVIEWED_AT, REVIEW_NOTE,
             SQL_PREDICATE_SNAPSHOT, FAILED_ROW_COUNT)
        VALUES (%(id)s, %(rid)s, %(title)s, %(status)s, %(by)s, CURRENT_TIMESTAMP(), %(note)s, %(pred)s, %(failed)s)
    """, {
        "id": review_id, "rid": stored_rule_id, "title": title or body.rule_title or "",
        "status": body.status, "by": x_user_email, "note": body.note or "",
        "pred": predicate or "",
        "failed": int(body.failed_row_count) if body.failed_row_count is not None else None,
    })
    logger.info(f"[REVIEW] {body.status}  rule={stored_rule_id}  delta_mode={delta_mode}  by={x_user_email}")
    return {"success": True, "status": body.status, "review_id": review_id}


@review_router.post("/approve-bulk")
def approve_bulk(
    body: BulkApproveRequest,
    auth: dict = Depends(require_permission("viewBDCSuperuser")),
    delta_mode: bool = Query(False, description="When True, record these as Delta-mode reviews"),
):
    """Approve several rules in one statement. Title and SQL predicate snapshots
    are read server-side, so the record matches what was actually approved."""
    reviewer = auth["user"]["email"]
    ids = list(dict.fromkeys(r.strip() for r in body.rule_ids if r and r.strip()))
    if not ids or any(len(r) > 200 for r in ids):
        raise HTTPException(status_code=400, detail="rule_ids must be non-empty and at most 200 characters each")
    ids_ok = allowed_rule_ids(auth)
    if ids_ok is not None and set(ids) - ids_ok:
        raise HTTPException(status_code=404, detail=f"Unknown rule(s): {', '.join(sorted(set(ids) - ids_ok))}")

    found = _rules_in_view(ids, delta_mode)
    missing = set(ids) - {r[0] for r in found}
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown rule(s): {', '.join(sorted(missing))}")

    review_ids, values, insert_params = {}, [], {"by": reviewer, "note": body.note or ""}
    for i, (rule_id, title, predicate) in enumerate(found):
        review_ids[rule_id] = str(uuid.uuid4())
        insert_params.update({f"id{i}": review_ids[rule_id], f"rid{i}": _storage_rule_id(rule_id, delta_mode),
                              f"t{i}": title or "", f"p{i}": predicate or ""})
        values.append(f"(%(id{i})s, %(rid{i})s, %(t{i})s, 'ACCEPTED', %(by)s, CURRENT_TIMESTAMP(), %(note)s, %(p{i})s, NULL)")
    _sf_write().execute(f"""
        INSERT INTO {_review_table()}
            (REVIEW_ID, RULE_ID, RULE_TITLE, STATUS, REVIEWED_BY, REVIEWED_AT,
             REVIEW_NOTE, SQL_PREDICATE_SNAPSHOT, FAILED_ROW_COUNT)
        VALUES {", ".join(values)}
    """, insert_params)
    logger.info(f"[REVIEW] bulk ACCEPTED  {len(found)} rules  delta_mode={delta_mode}  by={reviewer}")
    return {"success": True, "review_ids": review_ids}


@review_router.post("/undo")
def undo_reviews(
    body: UndoRequest,
    auth: dict = Depends(require_permission("viewBDCSuperuser")),
):
    """Take back your own decisions from the last few minutes. Older decisions,
    and anyone else's, stay in the history."""
    reviewer = auth["user"]["email"]
    ids = list(dict.fromkeys(body.review_ids))
    if not all(_UUID_RE.match(r) for r in ids):
        raise HTTPException(status_code=400, detail="review_ids must be review UUIDs")
    params = {f"i{n}": r for n, r in enumerate(ids)}
    params["by"] = reviewer
    res = _sf_write().execute(f"""
        DELETE FROM {_review_table()}
        WHERE REVIEW_ID IN ({", ".join(f"%(i{n})s" for n in range(len(ids)))})
          AND REVIEWED_BY = %(by)s
          AND REVIEWED_AT >= DATEADD(minute, -{UNDO_WINDOW_MINUTES}, CURRENT_TIMESTAMP())
    """, params)
    removed = int(res.rows[0][0]) if res.rows else 0
    logger.info(f"[REVIEW] undo  {removed}/{len(ids)} decisions  by={reviewer}")
    if removed == 0:
        raise HTTPException(status_code=409, detail=f"Nothing to undo: decisions can only be taken back by their reviewer within {UNDO_WINDOW_MINUTES} minutes")
    return {"success": True, "removed": removed}


@review_router.get("/history")
def get_review_history(
    rule_id:    Optional[str] = Query(None),
    limit:      int           = Query(200, ge=1, le=1000),
    delta_mode: bool          = Query(False, description="When True, return Delta-mode review history"),
    auth: dict = Depends(require_permission("viewBusinessChecks")),
):
    """Past Accept/Reject decisions for Business Defined Checks rules, newest
    first — optionally filtered to one rule_id."""
    sf  = _sf()
    if rule_id and not rule_allowed(auth, rule_id):
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    params = {"rid": _storage_rule_id(rule_id, delta_mode)} if rule_id else {}
    rule_clause = "AND RULE_ID = %(rid)s" if rule_id else ""
    try:
        res = sf.execute(f"""
            SELECT r.REVIEW_ID, r.RULE_ID, r.RULE_TITLE, r.STATUS, r.REVIEWED_BY,
                   {_UTC_ISO.format(col="r.REVIEWED_AT")} AS REVIEWED_AT, r.REVIEW_NOTE, r.FAILED_ROW_COUNT
            FROM {_review_table()} r
            WHERE {review_mode_sql(delta_mode)} {rule_clause}
            ORDER BY r.REVIEWED_AT DESC
            LIMIT {int(limit)}
        """, params)
    except Exception as e:
        logger.warning(f"History query failed: {e}")
        raise HTTPException(status_code=502, detail="Could not load the review history")
    history = [_to_dict(res.columns, r) for r in res.rows]
    for h in history:
        h["RULE_ID"] = _display_rule_id(h["RULE_ID"])
    ids = allowed_rule_ids(auth)
    if ids is not None:
        history = [h for h in history if h["RULE_ID"] in ids]
    return {"history": history}