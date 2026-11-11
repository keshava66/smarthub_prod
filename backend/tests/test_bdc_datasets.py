"""BDC dataset access rules (no database, no network).

Run from backend/:  python -m pytest tests/test_bdc_datasets.py -q
"""

import pytest

from auth import datasets_from_config, normalize_datasets, sql_literal, _perms

CONFIG = {
    "custom_roles": {
        "rt_viewer": {"name": "RT", "permissions": _perms(viewBusinessChecks=True), "bdc_datasets": ["RightTime"]},
        "everything": {"name": "All", "permissions": _perms(viewBusinessChecks=True), "bdc_datasets": ["*"]},
    },
    "user_permission_overrides": {"a@x.com": {"add": [], "remove": [], "datasets": ["MDM"]}},
}
VIEW = _perms(viewBusinessChecks=True)


def test_nothing_assigned_sees_nothing():
    assert datasets_from_config("b@x.com", ["it_viewer"], VIEW, CONFIG) == set()


def test_role_plus_user_datasets():
    assert datasets_from_config("a@x.com", ["rt_viewer"], VIEW, CONFIG) == {"RightTime", "MDM"}


def test_no_exemptions():
    assert datasets_from_config("b@x.com", ["everything"], VIEW, CONFIG) is None
    # admin starts with all datasets, but only because its role says so
    assert datasets_from_config("b@x.com", ["admin"], VIEW, CONFIG) is None
    limited_admin = {**CONFIG, "custom_roles": {**CONFIG["custom_roles"],
                     "admin": {"name": "Admin", "permissions": _perms(), "bdc_datasets": ["MDM"]}}}
    assert datasets_from_config("b@x.com", ["admin"], VIEW, limited_admin) == {"MDM"}
    su = _perms(viewBusinessChecks=True, viewBDCSuperuser=True)
    assert datasets_from_config("b@x.com", ["rt_viewer"], su, CONFIG) == {"RightTime"}


def test_normalize():
    assert normalize_datasets([" MDM ", "MDM", "Tiger"]) == ["MDM", "Tiger"]
    assert normalize_datasets(["MDM", "*"]) == ["*"]
    with pytest.raises(ValueError):
        normalize_datasets("MDM")


def test_literal_escapes_quotes():
    assert sql_literal("O'Brien\\x") == "'O''Brien\\\\x'"


def test_consolidated_report_cut_to_my_rules():
    import pandas as pd
    from consolidated_report import cut_report
    df = pd.DataFrame({
        "ID": ["1", "2", "3"],
        "MDM_ONLY": [None, None, "x"],
        "FAILURE_MODE": ["R1", "R1, R2", "R2"],
    })
    out, counts, colors, rules = cut_report(df, {"R1": 2, "R2": 2}, {"R1": ["A", None], "R2": ["B", None]},
                                            [["R1", "t", ""], ["R2", "t", ""]], {"R1"})
    assert list(out["ID"]) == ["1", "2"]
    assert list(out["FAILURE_MODE"]) == ["R1", "R1"]
    assert "MDM_ONLY" not in out.columns
    assert counts == {"R1": 2} and list(colors) == ["R1"] and rules == [["R1", "t", ""]]
    assert cut_report(df, {}, {}, [], None)[0] is df


def test_blank_dataset_title_is_its_own_dataset():
    import auth
    from auth import NO_DATASET, dataset_key, dataset_sql
    assert dataset_key(None) == dataset_key("  ") == NO_DATASET
    assert dataset_key("MDM") == "MDM"
    cfg = {"custom_roles": {"r": {"name": "r", "permissions": VIEW, "bdc_datasets": ["MDM", NO_DATASET]}}}
    auth.set_rbac_config_loader(lambda: cfg)
    who = {"user": {"email": "z@x.com"}, "roles": ["r"], "permissions": VIEW}
    assert dataset_sql(who, "v.DATASET_TITLE") == " AND (v.DATASET_TITLE IN ('MDM') OR NULLIF(TRIM(v.DATASET_TITLE), '') IS NULL)"


def test_old_roles_keep_downloads_new_ones_choose():
    from auth import get_role_permissions
    old = {"custom_roles": {"o": {"name": "o", "permissions": {"viewDashboard": True, "viewDQE": True, "viewBusinessChecks": True}}}}
    assert get_role_permissions("o", old)["downloadBDCData"] is True
    new = {"custom_roles": {"n": {"name": "n", "permissions": {**_perms(viewDQE=True, viewBusinessChecks=True)}}}}
    assert get_role_permissions("n", new)["downloadBDCData"] is False