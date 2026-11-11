"""
Basic tests for the assistant's security-critical behaviour:
  - tools are filtered/enforced by the user's permissions
  - the rate limiter actually blocks after the limit

Run from backend/:  python -m pytest assistant/tests/test_assistant.py -q
(or: python assistant/tests/test_assistant.py)
"""

import json

import pytest

from assistant import rbac, tools


ADMIN = {"viewDQE": True, "viewDashboard": True, "viewUserManagement": True}
NONE = {}


def test_rbac_has():
    assert rbac.has(ADMIN, "viewDQE") is True
    assert rbac.has(NONE, "viewDQE") is False
    assert rbac.has(ADMIN, "nonexistent") is False


def test_tool_specs_filtered_by_permission():
    admin_specs = {s["function"]["name"] for s in tools.build_tool_specs(ADMIN)}
    none_specs = {s["function"]["name"] for s in tools.build_tool_specs(NONE)}
    assert "list_rules" in admin_specs          # needs viewDQE
    assert "list_rules" not in none_specs       # no permission -> not offered
    assert none_specs == set() or "navigate_to" not in none_specs


def test_run_tool_refuses_without_permission():
    # even if the model asks, a user without the permission gets refused, not data
    out = tools.run_tool("list_rules", {}, NONE, "http://localhost:8000", "tok")
    assert "error" in out


def test_data_tool_forwards_user_token(monkeypatch):
    # the whole security model rests on the user's own bearer reaching the endpoint,
    # so the assistant never sees more than the user could. Pin that down.
    seen = {}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return [{"severity": "HIGH", "status": "ACCEPTED", "target_table": "T1"}]

    def fake_get(url, headers=None, params=None, timeout=None):
        seen["url"] = url
        seen["auth"] = headers.get("Authorization")
        return FakeResp()

    monkeypatch.setattr(tools._session, "get", fake_get)
    out = tools.run_tool("list_rules", {"severity": "high"}, ADMIN, "http://host", "tok123")
    assert seen["auth"] == "Bearer tok123"          # user's token, not a service one
    assert seen["url"] == "http://host/api/rules"
    assert out["count"] == 1


def test_rule_filter_survives_null_fields(monkeypatch):
    # a rule with null severity/status must not blow up the filter
    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return [{"severity": None, "status": None, "target_table": None}]

    monkeypatch.setattr(tools._session, "get", lambda *a, **k: FakeResp())
    out = tools.run_tool("list_rules", {"severity": "HIGH"}, ADMIN, "http://host", "tok")
    assert out["count"] == 0   # null severity simply doesn't match, no crash


def test_search_catalog_posts_query(monkeypatch):
    rows = [{"ID": str(i), "NOTE": "x" * 500} for i in range(10)]
    answer = {"term": "patient_id", "fuzzy": False, "edit_distance": 0, "total_records": 1520, "view_count": 20,
              "notes": [], "cached": False,
              "results": {f"bv_rdh_grail_v{i}": {"view": f"bv_rdh_grail_v{i}", "database": "db", "datasource": "grail",
                                                 "record_count": i * 10, "total_capped": False, "records": rows}
                          for i in range(20)}}

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return answer

    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["json"] = json
        seen["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr(tools._session, "post", fake_post)
    perms = {"viewData": True}
    out = tools.run_tool("search_catalog", {"query": "patient_id"}, perms, "http://host", "tok")
    assert seen["url"] == "http://host/search"
    assert seen["json"] == {"search_text": "patient_id", "max_records_per_view": 10}
    assert seen["timeout"] >= 120          # Denodo needs up to ~110s for a new term
    assert out["total_matches"] == 1520 and out["view_count"] == 20
    assert len(out["views"]) == tools.SEARCH_VIEWS_SHOWN and out["views_not_shown"] == 5
    assert out["views"][0]["view"] == "bv_rdh_grail_v19"          # biggest first
    assert len(out["views"][0]["sample_rows"]) == tools.SEARCH_ROWS_SHOWN
    assert len(out["views"][0]["sample_rows"][0]["NOTE"]) == tools.SEARCH_VALUE_CHARS
    assert out["page"] == "/fuzzy-match?q=patient_id"


def test_search_catalog_slow_denodo(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        raise tools.requests.Timeout()

    monkeypatch.setattr(tools._session, "post", fake_post)
    out = tools.run_tool("search_catalog", {"query": "x"}, {"viewData": True}, "http://host", "tok")
    assert "error" in out and out["page"] == "/fuzzy-match"


def test_search_catalog_requires_query():
    out = tools.run_tool("search_catalog", {}, {"viewData": True}, "http://host", "tok")
    assert "error" in out


def test_pending_reviews_filters_and_trims(monkeypatch):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {"rules": [
                {"rule_id": "R1", "review_status": "NOT_REVIEWED", "sql_predicate": "x" * 500, "ai_output": "y"},
                {"rule_id": "R2", "review_status": "ACCEPTED", "sql_predicate": "z", "ai_output": "w"},
            ]}

    monkeypatch.setattr(tools._session, "get", lambda *a, **k: FakeResp())
    out = tools.run_tool("get_pending_reviews", {}, {"viewBusinessChecks": True}, "http://host", "tok")
    assert out["pending_count"] == 1
    assert out["rules"][0]["rule_id"] == "R1"
    assert "sql_predicate" not in out["rules"][0]   # trimmed, not needed for a queue summary


def test_consolidated_summary_caps_preview_rows(monkeypatch):
    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {
                "found": True,
                "report_name": "RPT_1",
                "summary": {"total_rows": 9000},
                "preview": {"columns": ["A", "B"], "rows": [[i, i] for i in range(50)]},
            }

    monkeypatch.setattr(tools._session, "get", lambda *a, **k: FakeResp())
    out = tools.run_tool("get_consolidated_report_summary", {}, {"viewBusinessChecks": True}, "http://host", "tok")
    assert out["found"] is True
    assert len(out["preview_sample_rows"]) == 5   # capped, not the full 50


def test_new_tools_respect_permission_gating():
    # a user without the right permission never reaches the handler at all
    for name, perm in [
        ("search_catalog", "viewData"),
        ("get_pending_reviews", "viewBusinessChecks"),
        ("get_consolidated_report_summary", "viewBusinessChecks"),
        ("get_unstructured_summary", "viewUnstructuredRimDocs"),
        ("get_gra_data_domains_summary", "viewGraDataDomains"),
        ("get_rbac_overview", "viewUserManagement"),
        ("get_audit_stats", "viewAudit"),
    ]:
        out = tools.run_tool(name, {"query": "x"}, {}, "http://host", "tok")
        assert "error" in out, f"{name} should be denied without {perm}"


def test_rate_limit_blocks():
    from fastapi import HTTPException
    from assistant import router
    router._hits.clear()
    # chat bucket default is 30/min; the 31st call must raise
    for _ in range(30):
        router._rate_limit("chat", "u@x.com")
    with pytest.raises(HTTPException) as e:
        router._rate_limit("chat", "u@x.com")
    assert e.value.status_code == 429
    # a different user is unaffected
    router._rate_limit("chat", "other@x.com")


def test_retrieval_query_expands_short_followups():
    from assistant import router
    history = [
        {"role": "user", "content": "how are we doing on the RightTime dataset?"},
        {"role": "assistant", "content": "RightTime has 5 rules failing, pass rates from 44.84% to 99.89%."},
        {"role": "user", "content": "and QA?"},
    ]
    q = router._retrieval_query(history, "and QA?")
    # the bare follow-up alone has nothing for the embedder to match on — it must
    # pull in the prior turn so retrieval has something to work with
    assert "RightTime" in q
    assert "and QA?" in q


def test_retrieval_query_leaves_standalone_questions_alone():
    from assistant import router
    history = [
        {"role": "user", "content": "how are we doing on the RightTime dataset?"},
        {"role": "assistant", "content": "RightTime has 5 rules failing."},
        {"role": "user", "content": "what's the average pass rate across all datasets in the portal right now?"},
    ]
    q = router._retrieval_query(history, history[-1]["content"])
    # a full, self-contained question shouldn't get muddied with unrelated prior context
    assert q == history[-1]["content"]


def test_retrieval_query_handles_first_turn():
    from assistant import router
    # no prior turn to borrow context from — must not crash, just pass the question through
    assert router._retrieval_query([{"role": "user", "content": "hi"}], "hi") == "hi"
    assert router._retrieval_query([], "hi") == "hi"


def test_screen_block_keeps_tables_over_elements_when_over_budget():
    from assistant import router
    # a huge 'elements' list (serialized before 'tables') used to be able to eat the
    # entire truncation budget and silently drop 'tables' — the block the system
    # prompt tells the model to read exact numbers from. Confirm tables survives.
    screen = {
        "route": "/dqe",
        "elements": [{"ref": f"e{i}", "tag": "td", "text": "x" * 200} for i in range(500)],
        "tables": [{"headers": ["Rule", "Pass %"], "rows": [["RightTime submission window", "44.84%"]]}],
        "text": "y" * 50000,
    }
    block = router._screen_block(screen)
    assert "44.84%" in block
    assert "RightTime submission window" in block


def test_screen_block_keeps_tour_outline_when_over_budget():
    from assistant import router
    # a busy page (the dashboard: a dozen cards plus header and sidebar) used to lose
    # its whole tour_outline before elements were trimmed, so "give me a tour" said the
    # page had no outline. Elements shrink first; the outline and valid JSON survive.
    screen = {
        "route": "/dashboard",
        "controls": [{"ref": f"c{i}", "kind": "button", "label": "Open tool", "sel": "div > " * 15} for i in range(40)],
        "elements": [{"ref": f"e{i}", "tag": "div", "text": "x" * 160, "sel": "div > " * 15} for i in range(120)],
        "tour_outline": [{"ref": f"t{i}", "label": f"Block {i}", "desc": "what it does " * 6} for i in range(8)],
        "text": "y" * 12000,
    }
    block = router._screen_block(screen)
    body = json.loads(block.split("\n", 1)[1])
    assert [t["label"] for t in body["tour_outline"]] == [f"Block {i}" for i in range(8)]
    assert len(body["elements"]) < 120


def test_screen_block_empty_screen_says_so():
    from assistant import router
    block = router._screen_block({"route": "/dashboard"})
    assert "/dashboard" in block
    assert "can't see" in block.lower()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))