import pandas as pd

GREEN = "green"
YELLOW = "yellow"
RED = "red"

# Worst-wins ordering, shared by every caller.
SEVERITY_RANK = {GREEN: 0, YELLOW: 1, RED: 2}

# mode:
#   "days" -> abs(diff) <= 7 green, 8-15 yellow, >= 16 red, null red
#   "sign" -> diff >= 0 green, otherwise red (no middle band, direction is
#             what matters here, not magnitude)
#
# require_not_null: extra columns that must be populated for a green. If any
#   of them is null the row goes red regardless of the diff value.
DIFF_COLOR_RULES = [
    {
        "diff_col": "DIFFERENCE_ACTUAL_SUBMISSION_VS_MODIFIED_SUBMISSION_DATE",
        "targets": ["ACTUAL_SUBMISSION_DATE", "ACTUAL_SUBMISSION_MODIFIED_DATE"],
        "require_not_null": ["ACTUAL_SUBMISSION_DATE"],
        "mode": "days",
    },
    {
        "diff_col": "DIFFERENCE_EVENT_CREATED_VS_SUBMISSION_DATE",
        "targets": ["ACTUAL_SUBMISSION_DATE"],
        "require_not_null": [],
        "mode": "sign",
    },
    {
        "diff_col": "DIFFERENCE_ACTUAL_APPROVAL_VS_MODIFIED_APPROVAL_DATE",
        "targets": ["ACTUAL_APPROVED_DATE", "ACTUAL_APPROVED_MODIFIED_DATE"],
        "require_not_null": [],
        "mode": "days",
    },
]


def build_col_index(columns):
    """Map stripped/uppercased column name -> position."""
    return {str(c).strip().upper(): i for i, c in enumerate(columns)}


def _is_null(v):
    try:
        return v is None or pd.isna(v)
    except (TypeError, ValueError):
        return v is None


def _to_number(v):
    """Diff columns come back as int, Decimal or string depending on driver."""
    if _is_null(v):
        return None
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _severity(spec, diff_val, row_vals, col_index):
    for col in spec["require_not_null"]:
        ci = col_index.get(col, -1)
        if ci == -1 or _is_null(row_vals[ci]):
            return RED

    n = _to_number(diff_val)
    if n is None:
        return RED

    if spec["mode"] == "sign":
        return GREEN if n >= 0 else RED

    days = abs(n)
    if days <= 7:
        return GREEN
    if days <= 15:
        return YELLOW
    return RED


def active_rules(col_index):
    """The subset of rules whose diff column and at least one target are present."""
    live = []
    for spec in DIFF_COLOR_RULES:
        if spec["diff_col"] not in col_index:
            continue
        target_idxs = [col_index[t] for t in spec["targets"] if t in col_index]
        if target_idxs:
            live.append((spec, target_idxs))
    return live


def target_col_indices(col_index):
    """Every column index these rules can paint — used for header highlighting."""
    idxs = set()
    for _spec, target_idxs in active_rules(col_index):
        idxs.update(target_idxs)
    return idxs


def describe(spec):
    """
    Plain-English wording for the legend sheet, derived from the spec itself so
    the documentation can't drift away from the behaviour.
    """
    painted = " and ".join(spec["targets"])
    plural = len(spec["targets"]) > 1
    verb = "turn" if plural else "turns"
    subject = "cells are" if plural else "cell is"

    if spec["mode"] == "sign":
        condition = (
            f"0 or greater the difference runs in the expected direction, so {painted} "
            f"{verb} green. A negative value is red."
        )
    else:
        condition = (
            f"7 or fewer (absolute) {painted} {verb} green. "
            "8-15 days is yellow, 16 or more is red."
        )

    null_note = ""
    if spec["require_not_null"]:
        cols = " and ".join(spec["require_not_null"])
        null_note = (
            f" If {cols} is empty the {subject} red regardless of the difference value."
        )

    return (
        f"Driven by {spec['diff_col']}. When that value is {condition}"
        f"{null_note} A missing difference value is treated as red."
    )


def accumulate(row_vals, col_index, accum, rules=None):
    """
    Fold this row's diff-rule verdicts into ``accum`` — a
    {column_index: [severity, ...]} dict shared with the COLOR_COLUMNS logic,
    so a cell hit by both paths ends up with both severities and merges the
    same way any multi-rule cell does.
    """
    for spec, target_idxs in (rules if rules is not None else active_rules(col_index)):
        sev = _severity(spec, row_vals[col_index[spec["diff_col"]]], row_vals, col_index)
        for ci in target_idxs:
            accum.setdefault(ci, []).append(sev)
    return accum