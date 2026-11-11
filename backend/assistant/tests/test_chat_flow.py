"""
The /assistant/chat endpoint end to end, with a scripted stand-in for the
model. Checks what the model is shown, and what reaches the browser when it
asks to navigate - including when it gets it wrong.
"""

import json
from types import SimpleNamespace as NS

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant import router as r, tools

ALL = {p: True for p in __import__("auth").ALL_PERMISSIONS}
IT_USER = {"viewDashboard": True, "viewDQE": True, "viewITChecks": True, "viewDataValidation": True,
           "viewBusinessChecks": True, "viewBDCExecutive": True, "viewAssistantAvatar": True}


def chunk(content=None, tool_calls=None, finish=None):
    return NS(choices=[NS(delta=NS(content=content, tool_calls=tool_calls), finish_reason=finish)])


def tool_call(name, args, idx=0, cid="call_1"):
    return chunk(tool_calls=[NS(index=idx, id=cid, function=NS(name=name, arguments=json.dumps(args)))],
                 finish="tool_calls")


class FakeModel:
    """Plays back one scripted reply per completion call and records what it was sent."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        if not kw.get("stream"):     # history summariser
            return NS(choices=[NS(message=NS(content="summary"))])
        step = self.script.pop(0) if self.script else [chunk("ok", finish="stop")]
        return iter(step)


@pytest.fixture
def run_chat(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fake")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("ASSISTANT_ACTION_ACK_TIMEOUT", "0.05")
    monkeypatch.delenv("AZURE_OPENAI_EMBED_ENDPOINT", raising=False)
    r._hits.clear()

    def go(script, text, perms=ALL, url="https://x/dashboard", history=()):
        model = FakeModel(script)
        monkeypatch.setattr(r, "_chat_client", lambda s: model)
        monkeypatch.setattr(r, "_embed_client", lambda s: None)
        app = FastAPI()
        app.include_router(r.router)

        async def fake_auth():
            return {"user": {"email": "t@x.com"}, "token": "tok", "role": "test_" + str(len(perms)),
                    "permissions": {**perms, "viewAssistantAvatar": True, "viewDashboard": True}}
        app.dependency_overrides[r._auth] = fake_auth
        body = {"messages": [*history, {"role": "user", "content": text}],
                "screen": {"route": url.split("x")[1].split("?")[0], "url": url}}
        res = TestClient(app).post("/assistant/chat", json=body)
        events = [json.loads(line[5:]) for line in res.text.split("\n\n") if line.startswith("data:")]
        return model, events
    return go


def actions(events):
    return [e["action"] for e in events if e["type"] == "action"]


def tool_results(model):
    last = model.calls[-1]["messages"]
    return [json.loads(m["content"]) for m in last if m["role"] == "tool"]


def system_text(model):
    return "\n".join(m["content"] for m in model.calls[0]["messages"] if m["role"] == "system")


def test_take_me_to_moves_the_page_before_the_model_even_answers(run_chat):
    # fast path: the page is on its way before the first model call returns,
    # and the model's own (parent-page) navigate_to is not sent a second time
    model, events = run_chat([[tool_call("navigate_to", {"route": "/dqe"})],
                              [chunk("Done.", finish="stop")]],
                             "Take me to Business Defined Checks")
    [act] = actions(events)
    assert act["args"]["params"] == {"check": "Business_defined_checks"}
    first_action = next(i for i, e in enumerate(events) if e["type"] == "action")
    first_token = next(i for i, e in enumerate(events) if e["type"] == "token")
    assert first_action < first_token
    # the model was shown the navigation as already done
    assert any(m.get("tool_call_id") == "fastnav" for m in model.calls[0]["messages"])


def test_parent_page_navigation_is_upgraded_for_questions(run_chat):
    # not an imperative, so no fast path - the model's parent-page call gets upgraded
    model, events = run_chat([[tool_call("navigate_to", {"route": "/dqe"})],
                              [chunk("It's here.", finish="stop")]],
                             "where are the business defined checks?")
    [act] = actions(events)
    assert act["args"]["params"] == {"check": "Business_defined_checks"}
    assert "upgraded" in tool_results(model)[0]["note"]


def test_roles_goes_to_the_roles_tab(run_chat):
    model, events = run_chat([[tool_call("navigate_to", {"destination": "users"})],
                              [chunk("Here you go.", finish="stop")]], "Show me the roles")
    assert actions(events)[0]["args"]["params"] == {"tab": "roles"}


def test_model_sees_resolver_hint_and_current_location(run_chat):
    model, _ = run_chat([[chunk("ok", finish="stop")]], "take me to the review tab",
                        url="https://x/dqe?check=Business_defined_checks&view=standard&tab=trends")
    sys = system_text(model)
    assert "USER IS CURRENTLY AT: Data quality › Business Defined Checks › Trends" in sys
    assert "Best match: bdc.review" in sys
    assert "EXACT DESTINATION" in sys


def test_no_access_means_nothing_is_sent(run_chat):
    model, events = run_chat([[tool_call("navigate_to", {"destination": "bdc.superuser"})],
                              [chunk("You don't have access.", finish="stop")]],
                             "take me to the superuser tab", perms=IT_USER)
    assert actions(events) == []
    res = tool_results(model)[0]
    assert "viewBDCSuperuser" in res["missing_permissions"]
    # and the hint told the model up front
    assert "NO ACCESS" in system_text(model)


def test_navigate_spec_hides_places_without_access(run_chat):
    model, _ = run_chat([[chunk("ok", finish="stop")]], "hi", perms=IT_USER)
    spec = next(t for t in model.calls[0]["tools"] if t["function"]["name"] == "navigate_to")
    ids = spec["function"]["parameters"]["properties"]["destination"]["enum"]
    assert "bdc.superuser" not in ids and "bdc.trends" in ids


def test_invented_route_never_reaches_the_browser(run_chat):
    model, events = run_chat([[tool_call("navigate_to", {"route": "/quarterly-reports"})],
                              [chunk("I couldn't find that page.", finish="stop")]],
                             "open the quarterly reports page")
    assert actions(events) == []
    assert tool_results(model)[0]["status"].startswith("not sent")
    assert "No page or tab name in this message matched" in system_text(model)


def test_already_there(run_chat):
    model, events = run_chat([[tool_call("navigate_to", {"destination": "users.roles"})],
                              [chunk("You're already on it.", finish="stop")]],
                             "show me the roles", url="https://x/user-management?tab=roles")
    assert actions(events) == []
    assert "NOTHING CHANGED" in tool_results(model)[0]["status"]


def test_table_copier_sends_the_follow_up_click(run_chat):
    _, events = run_chat([[tool_call("navigate_to", {"destination": "studio.copier"})],
                          [chunk("Opened.", finish="stop")]], "I need to copy a table")
    args = actions(events)[0]["args"]
    assert args["params"] == {"pane": "snowflake"} and args["then_click"] == "Copy tables"


def test_tour_steps_get_real_urls(run_chat):
    steps = [{"destination": "bdc.trends", "target": "Rule Selector", "say": "Pick a rule here."},
             {"destination": "bdc.failed", "target": "Show Failed Rows", "say": "Then load failures."}]
    _, events = run_chat([[tool_call("guide_tour", {"steps": steps})], [chunk("", finish="stop")]],
                         "walk me through checking a failing rule")
    sent = actions(events)[0]["args"]["steps"]
    assert sent[0]["params"]["tab"] == "trends" and sent[1]["params"]["tab"] == "failed-rows"


def test_off_topic_gets_no_navigation_hint(run_chat):
    model, _ = run_chat([[chunk("I can't tell the time.", finish="stop")]], "what time is it?")
    assert "NAVIGATION RESOLVER (" not in system_text(model)


def test_follow_up_uses_history(run_chat):
    history = [{"role": "user", "content": "what's on the review tab?"},
               {"role": "assistant", "content": "The Review tab is where admins accept or reject rules."}]
    model, events = run_chat([[tool_call("navigate_to", {"destination": "bdc.review"})],
                              [chunk("Opened.", finish="stop")]], "take me there", history=history)
    assert actions(events)[0]["args"]["params"]["tab"] == "review"
    assert "No page or tab name in this message matched" in system_text(model)  # tells the model to use the chat


def test_consolidated_tool_hits_the_real_endpoint(run_chat, monkeypatch):
    seen = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"found": True, "report_name": "R1", "summary": {"total_rows": 3},
                                "preview": {"columns": ["A"], "rows": [[1]]}}

    def fake_get(url, **kw):
        seen["url"] = url
        return Resp()
    monkeypatch.setattr(tools._session, "get", fake_get)
    model, _ = run_chat([[tool_call("get_consolidated_report_summary", {})],
                         [chunk("3 rows.", finish="stop")]], "what's in the consolidated report?")
    assert seen["url"].endswith("/api/consolidated-report/latest")
    assert tool_results(model)[0]["report_name"] == "R1"


def test_error_bubbles_in_history_are_dropped(run_chat):
    history = [{"role": "user", "content": "hi"}, {"role": "error", "content": "boom"}]
    model, events = run_chat([[chunk("hello", finish="stop")]], "hi again", history=history)
    assert all(m["role"] != "error" for m in model.calls[0]["messages"])
    assert not [e for e in events if e["type"] == "error"]


def test_screenshot_bug_yes_switches_to_semi_monthly(run_chat):
    """User on Failed Rows (Quarterly) said 'yes' to switching; the switch was
    dropped as 'already there' and the model still claimed it had switched."""
    history = [{"role": "user", "content": "show me the semi-monthly data"},
               {"role": "assistant", "content": "Would you like me to switch Failed Rows to Semi-Monthly?"}]
    model, events = run_chat([[tool_call("navigate_to", {"destination": "bdc.failed"})],
                              [chunk("Switched.", finish="stop")]], "yes", history=history,
                             url="https://x/dqe?check=Business_defined_checks&view=standard&tab=failed-rows")
    [act] = actions(events)
    assert act["args"]["params"]["mode"] == "delta"
    assert "showing Quarterly data" in system_text(model)
    assert "NEVER CLAIM A CHANGE THAT DIDN'T HAPPEN" in system_text(model)


def test_sources_are_listed_once(run_chat, monkeypatch):
    from assistant import retrieval
    hit = {"title": "Where things are: Data quality", "source": "assistant/portal_map.py", "text": "x", "score": 0.9}
    monkeypatch.setattr(r, "_embed_client", lambda s: object())
    monkeypatch.setattr(retrieval, "retrieve", lambda *a, **k: [hit, dict(hit), dict(hit)])
    monkeypatch.setenv("AZURE_OPENAI_EMBED_ENDPOINT", "https://fake")
    app_model = FakeModel([[chunk("ok", finish="stop")]])
    monkeypatch.setattr(r, "_chat_client", lambda s: app_model)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI(); app.include_router(r.router)

    async def fake_auth():
        return {"user": {"email": "t@x.com"}, "token": "t", "role": "x", "permissions": {**ALL}}
    app.dependency_overrides[r._auth] = fake_auth
    res = TestClient(app).post("/assistant/chat", json={"messages": [{"role": "user", "content": "what is dqe"}]})
    srcs = [json.loads(l[5:]) for l in res.text.split("\n\n") if l.startswith("data:") and '"sources"' in l]
    assert len(srcs[0]["sources"]) == 1


# --- "take me to X" safety net ---

def test_take_me_there_navigates_even_if_the_model_only_talks(run_chat):
    model, events = run_chat([[chunk("The Review tab is under Business Defined Checks. Want me to open it?",
                                     finish="stop")]], "take me to the review tab")
    [act] = actions(events)
    assert act["args"]["params"]["tab"] == "review"


def test_take_me_there_after_a_failed_lookup(run_chat, monkeypatch):
    import requests

    class Resp:
        status_code = 401
        def raise_for_status(self): raise requests.HTTPError(response=self)
    monkeypatch.setattr(tools._session, "get", lambda *a, **k: Resp())
    model, events = run_chat([[tool_call("get_dataset_health", {})],
                              [chunk("I couldn't fetch dataset health.", finish="stop")]],
                             "which dataset is weakest, take me there")
    assert actions(events)[0]["args"]["params"]["tab"] == "dataset-health"
    assert any("NOT a role/permission problem" in (t.get("error") or "") for t in tool_results(model))


def test_no_safety_net_for_questions_ambiguity_or_no_access(run_chat):
    for text, perms in (("what does the review tab do?", ALL),
                        ("take me to the overview", ALL),
                        ("take me to the superuser tab", IT_USER)):
        _, events = run_chat([[chunk("ok", finish="stop")]], text, perms=perms)
        assert actions(events) == [], text


def test_no_double_navigation(run_chat):
    _, events = run_chat([[tool_call("navigate_to", {"destination": "bdc.review"})],
                          [chunk("Opened.", finish="stop")]], "take me to the review tab")
    assert len(actions(events)) == 1


def test_no_safety_net_when_already_there(run_chat):
    _, events = run_chat([[chunk("You're already on it.", finish="stop")]], "go home",
                         url="https://x/dashboard")
    assert actions(events) == []


def test_prompt_says_where_questions_navigate(run_chat):
    model, _ = run_chat([[chunk("ok", finish="stop")]], "where do I approve rules?")
    assert "WHERE-IS QUESTIONS GO THERE" in system_text(model)



def test_safety_net_still_covers_a_non_fast_request(run_chat):
    # "go to ... and tell me" with an ambiguous-looking long ask the resolver can
    # still pin: no fast path when the model is the one refusing? - the net catches it
    _, events = run_chat([[chunk("Sure.", finish="stop")]], "take me to the review tab")
    assert len(actions(events)) == 1


def test_navigation_failure_is_corrected_in_the_reply(run_chat, monkeypatch):
    # the browser reports the page didn't open -> the reply says so instead of pretending
    orig = r._wait_ack
    monkeypatch.setattr(r, "_wait_ack", lambda ack_id, ev, t: {"success": False, "detail": "sent to /dashboard"})
    _, events = run_chat([[chunk("Opened it.", finish="stop")]], "take me to the review tab")
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "didn't open" in text and "sent to /dashboard" in text
    monkeypatch.setattr(r, "_wait_ack", orig)