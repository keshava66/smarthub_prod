"""
How people actually ask to go places, and where they should land.

Each case is (what the user typed, destination id it must resolve to). No
model involved - this is the deterministic layer the router leans on, so it
has to be right on its own.
"""

import pytest

from assistant import navigation, portal_map as pm

ALL = {p: True for p in __import__("auth").ALL_PERMISSIONS}

CASES = [
    # --- the two examples from the brief ---
    ("Take me to Business Defined Checks", "bdc"),
    ("Show me the roles", "users.roles"),
    # --- DQE and check types ---
    ("open the data quality engine", "dqe"),
    ("go to DQE", "dqe"),
    ("take me to the dq engine", "dqe"),
    ("open IT checks", "dqe.it"),
    ("show me the IT checks row counts", "dqe.it"),
    ("where are the IT checks row counts", "dqe.it"),
    ("show me the IT validation report", "dqe.it"),
    ("take me to data validation", "dqe.dv"),
    ("open the guided source to target validation", "dqe.dv"),
    ("go to business checks", "bdc"),
    ("open BDC", "bdc"),
    ("bring up the business rules", "bdc"),
    ("take me to buisness defined checks", "bdc"),
    ("open the executive view", "bdc.exec"),
    ("show me the exec dashboard", "bdc.exec"),
    ("switch to the standard view", "bdc.standard"),
    ("show me the active rules table", "bdc.overview"),
    ("open business defined checks overview", "bdc.overview"),
    ("go to dataset info", "bdc.datasets"),
    ("show me dataset health", "bdc.datasets"),
    ("which dataset is weakest, take me there", "bdc.datasets"),
    ("open trends", "bdc.trends"),
    ("show me the pass rate history", "bdc.trends"),
    ("take me to the BDC trends tab", "bdc.trends"),
    ("open failed rows", "bdc.failed"),
    ("show me the failing rows", "bdc.failed"),
    ("where is the delta report", "bdc.failed"),
    ("take me to the consolidated report", "bdc.failed"),
    ("show me the sql predicate", "bdc.failed"),
    ("take me to the superuser tab", "bdc.superuser"),
    ("open super user", "bdc.superuser"),
    ("go to the super-user area", "bdc.superuser"),
    ("open the superusr tab", "bdc.superuser"),
    ("open DQ tables", "bdc.superuser"),
    ("where do I create a new rule", "bdc.superuser.governance"),
    ("open rule governance", "bdc.superuser.governance"),
    ("take me to the DQ rule governance table", "bdc.superuser.governance"),
    ("open the AI registry", "bdc.superuser.registry"),
    ("show me the DQ rules AI registry", "bdc.superuser.registry"),
    ("open DQ date input", "bdc.superuser.dates"),
    ("where do I set the reporting dates", "bdc.superuser.dates"),
    ("take me to the review tab", "bdc.review"),
    ("where do I sign off rules", "bdc.review"),
    ("open the rule sign-off queue", "bdc.review"),
    ("show me what's awaiting review", "bdc.review"),
    ("where do I approve rules", "bdc.review"),
    # --- Ingestion Lab ---
    ("open ingestion lab", "ingest"),
    ("go to the ingestion lab", "ingest"),
    ("take me to ingestion", "ingest"),
    # --- Fuzzy match ---
    ("open fuzzy match", "fuzzy"),
    ("take me to fuzzy search", "fuzzy"),
    ("search the denodo catalog", "fuzzy"),
    # --- Unstructured ---
    ("open the unstructured explorer", "unstructured"),
    ("go to unstructured data", "unstructured"),
    ("open rimdocs", "un.rimdocs"),
    ("show me the rim docs explorer", "un.rimdocs"),
    ("open the rimdocs domain breakdown", "un.rimdocs.breakdown"),
    ("take me to the SF query runner", "un.rimdocs.query"),
    ("open the postgres query runner", "un.rimdocs.postgres"),
    ("open the PG query runner", "un.rimdocs.postgres"),
    ("open rimdocs data table", "un.rimdocs.table"),
    ("take me to FDA and others", "un.fda"),
    ("open the FDA explorer", "un.fda"),
    ("open the pink sheet letters dashboard", "un.fda.dash.crl_pink_letters"),
    ("show me CRL AI matches", "un.fda.dash.crl_matches"),
    ("open eCTD documents", "un.fda.dash.ectd_docs"),
    ("take me to the RIM facility index", "un.fda.dash.rim_facility_index"),
    ("open milvexian", "un.fda.dash.milvexian_metadata"),
    ("open fda data table", "un.fda.table"),
    ("open epod", "un.epod"),
    ("open the epod data table", "un.epod.table"),
    ("open Q!", "un.haq"),
    ("take me to HAQ", "un.haq"),
    ("open nipocalimab", "un.nipo"),
    ("show me the nipo data table", "un.nipo.table"),
    ("open virtual regulator", "un.vr"),
    ("show me the virtual regulator explore tab", "un.vr.explore"),
    ("open the virtual regulator data table", "un.vr.table"),
    ("open the virtual regulator documents tab", "un.vr.library"),
    ("open virtual regulator external", "un.vr.external"),
    ("open mirai", "un.mirai"),
    ("take me to rmwcreate", "un.rmwcreate"),
    ("open immbot", "un.immbot"),
    ("show me the immbot explore tab", "un.immbot.explore"),
    ("open the immbot data table", "un.immbot.table"),
    ("take me to the immbot breakdown", "un.immbot.breakdown"),
    # --- Structured Explorer ---
    ("open structured explorer", "structured"),
    ("take me to structured packages", "structured"),
    ("who are the data stewards", "structured"),
    ("open the GRA data domains", "structured"),
    ("show me the domain directory", "structured.directory"),
    # --- Data Studio ---
    ("open data studio", "studio"),
    ("take me to snowflake", "studio.snowflake"),
    ("I want to run SQL", "studio.snowflake"),
    ("open a snowflake worksheet", "studio.snowflake"),
    ("where can I edit table rows", "studio.snowflake"),
    ("open a postgres worksheet", "studio.postgres"),
    ("I want to query postgres", "studio.postgres"),
    ("open the table copier", "studio.copier"),
    ("I need to copy a table", "studio.copier"),
    ("open s3", "studio.s3"),
    ("browse the s3 buckets", "studio.s3"),
    ("open the streamsets extractor", "studio.streamsets"),
    ("I want to extract a streamsets pipeline", "studio.streamsets"),
    ("open data shuttle", "studio.shuttle"),
    ("take me to the data shuttle", "studio.shuttle"),
    ("go to data studo snowflake", "studio.snowflake"),
    # --- Data API Export ---
    ("open data api export", "dataapi"),
    ("take me to the data APIs", "dataapi"),
    ("create a new API", "dataapi.new"),
    ("start the new api wizard", "dataapi.new"),
    ("show me the api endpoints", "dataapi.endpoints"),
    ("open the semantic search settings", "dataapi.search"),
    ("where do i manage embedding models", "dataapi.search"),
    ("open the api docs", "dataapi.docs"),
    ("show me api usage", "dataapi.activity"),
    # --- RAGs ---
    ("open the RAGs", "rag"),
    ("take me to epod rag", "rag.epod"),
    ("open rimdocs rag", "rag.rimdocs"),
    ("open rimdocs agentic", "rag.agentic"),
    ("go to the agentic rag", "rag.agentic"),
    # --- AI assistant page ---
    ("open the AI assistant page", "aichat"),
    ("take me to the chatbot", "aichat"),
    # --- User Management ---
    ("open user management", "users"),
    ("go to user mgmt", "users"),
    ("show me the AD group mappings", "users.groups"),
    ("take me to groups", "users.groups"),
    ("open roles and permissions", "users.roles"),
    ("show me the permissions", "users.roles"),
    ("where do I create a custom role", "users.roles"),
    ("open the users tab", "users.users"),
    ("where do I change someone's role", "users.users"),
    ("show me the user list", "users.users"),
    ("user management roles", "users.roles"),
    # --- Communication Center ---
    ("open the communication center", "comms"),
    ("I want to send an email to users", "comms.compose"),
    ("take me to compose", "comms.compose"),
    ("send a maintenance notice", "comms.compose"),
    ("show me what's new", "comms.whatsnew"),
    ("open the release notes", "comms.whatsnew"),
    ("show me scheduled emails", "comms.queue"),
    ("open the queue in communication center", "comms.queue"),
    ("show me sent history", "comms.history"),
    ("who did we email last", "comms.history"),
    # --- Audit ---
    ("open the audit log", "audit"),
    ("show me the audit trail", "audit"),
    ("who is online right now", "audit"),
    ("open audit analytics", "audit.analytics"),
    ("show me portal usage analytics", "audit.analytics"),
    ("show me role changes", "audit.security"),
    ("open the security review", "audit.security"),
    ("show me session timeouts", "audit.access"),
    ("open the admin trail", "audit.admin"),
    ("show me rule decisions", "audit.rules"),
    ("show me page views", "audit.nav"),
    # --- dashboard ---
    ("go home", "dashboard"),
    ("take me to the dashboard", "dashboard"),
    ("back to the landing page", "dashboard"),
    ("take me to my toolkit", "dashboard"),
    ("pick up where I left off", "dashboard"),
]


@pytest.mark.parametrize("text,want", CASES)
def test_phrasing_resolves_to_exact_place(text, want):
    r = pm.resolve(text, ALL)
    assert r["best"], f"{text!r}: nothing matched"
    assert r["best"]["id"] == want, f"{text!r}: got {r['best']['id']}, candidates {[m['id'] for m in r['matches']]}"
    # and it has to be a clear answer - a coin-flip that happens to land right isn't
    assert not r["ambiguous"], f"{text!r}: resolved to {want} but flagged ambiguous: {r['matches'][:3]}"


def test_every_destination_has_at_least_one_case():
    covered = {want for _, want in CASES}
    missing = sorted(set(pm.DESTINATIONS) - covered)
    # fda explore/table + epod/nipo explore and a few api tabs are covered by
    # context tests below; everything else needs a phrasing case
    allowed_gaps = {"un.fda.explore", "un.epod.explore", "un.nipo.explore", "un.rimdocs.overview",
                    "dataapi.overview", "dataapi.tables", "un.fda.dash.crl_letters",
                    "un.fda.dash.fda_letters_v2"}
    assert set(missing) <= allowed_gaps, f"no phrasing test for: {sorted(set(missing) - allowed_gaps)}"


# --- things that must NOT turn into navigation ---

@pytest.mark.parametrize("text", [
    "what time is it", "what's the weather", "tell me a joke", "what is 2+2",
    "hello", "thanks!", "how many rules are failing",
])
def test_off_portal_or_data_questions_dont_resolve(text):
    assert pm.resolve(text, ALL)["best"] is None


# --- generic words need context ---

def test_generic_tab_names_are_ambiguous_without_context():
    for text in ("take me to the overview", "open the data table", "open explore"):
        r = pm.resolve(text, ALL)
        assert r["ambiguous"], f"{text!r} should be ambiguous, got {r['best']}"


@pytest.mark.parametrize("current,text,want", [
    ("bdc.trends", "go to the overview tab", "bdc.overview"),
    ("bdc.trends", "now the failed rows", "bdc.failed"),
    ("un.epod", "open the data table", "un.epod.table"),
    ("un.nipo.table", "back to explore", "un.nipo.explore"),
    ("un.fda.table", "open explore", "un.fda.explore"),
    ("un.immbot.table", "back to explore", "un.immbot.explore"),
    ("un.rimdocs.table", "go to explore", "un.rimdocs.overview"),
    ("dataapi.docs", "show me the overview", "dataapi.overview"),
    ("dataapi.docs", "open the tables tab", "dataapi.tables"),
    ("un.fda", "open crl letters overview", "un.fda.dash.crl_letters"),
    ("un.fda", "open the fda crl letters dashboard", "un.fda.dash.fda_letters_v2"),
])
def test_current_page_settles_generic_names(current, text, want):
    r = pm.resolve(text, ALL, current=current)
    assert r["best"]["id"] == want and not r["ambiguous"], r


# --- permissions ---

VIEWER = {"viewDashboard": True}
IT_USER = {"viewDashboard": True, "viewDQE": True, "viewITChecks": True, "viewDataValidation": True,
           "viewBusinessChecks": True, "viewBDCExecutive": True}


def test_resolver_marks_places_the_user_cant_open():
    r = pm.resolve("take me to the superuser tab", IT_USER)
    assert r["best"]["id"] == "bdc.superuser" and r["best"]["allowed"] is False


def test_navigate_refuses_without_access():
    nav, res = navigation.prepare_navigation({"destination": "bdc.review"}, IT_USER, "open review")
    assert nav is None
    assert "viewBDCSuperuser" in res["missing_permissions"]


def test_viewer_cant_open_dqe_at_all():
    nav, res = navigation.prepare_navigation({"destination": "bdc"}, VIEWER, "open bdc")
    assert nav is None and set(res["missing_permissions"]) == {"viewDQE", "viewBusinessChecks"}


def test_navigate_spec_only_lists_reachable_places():
    from assistant import tools
    spec = next(s for s in tools.build_tool_specs(IT_USER) if s["function"]["name"] == "navigate_to")
    ids = set(spec["function"]["parameters"]["properties"]["destination"]["enum"])
    assert "bdc.trends" in ids and "dqe.it" in ids
    assert "bdc.superuser" not in ids and "users.roles" not in ids and "studio" not in ids


def test_tool_spec_cache_notices_permission_change():
    from assistant import tools
    before = tools.build_tool_specs(VIEWER, role="custom_x")
    after = tools.build_tool_specs(IT_USER, role="custom_x")
    ids = lambda specs: set(next(s for s in specs if s["function"]["name"] == "navigate_to")
                            ["function"]["parameters"]["properties"]["destination"]["enum"])
    assert "bdc" not in ids(before) and "bdc" in ids(after)


# --- server-side navigation guard ---

def test_destination_expands_to_exact_url():
    nav, res = navigation.prepare_navigation({"destination": "bdc.trends"}, ALL, "open trends")
    assert nav["route"] == "/dqe"
    assert nav["params"] == {"check": "Business_defined_checks", "view": "standard", "tab": "trends"}
    assert res["status"] == "sent"


def test_parent_page_is_upgraded_to_what_the_user_named():
    # the model stopped at /dqe - the user said Business Defined Checks
    nav, res = navigation.prepare_navigation({"route": "/dqe"}, ALL, "Take me to Business Defined Checks")
    assert nav["params"] == {"check": "Business_defined_checks"}
    assert "upgraded" in res["note"]
    nav, res = navigation.prepare_navigation({"destination": "users"}, ALL, "Show me the roles")
    assert nav["params"] == {"tab": "roles"}


def test_no_upgrade_when_request_is_ambiguous():
    nav, _ = navigation.prepare_navigation({"destination": "dataapi"}, ALL, "take me to the overview")
    assert nav["params"] == {}


def test_no_upgrade_into_a_place_without_access():
    nav, res = navigation.prepare_navigation({"destination": "bdc"}, IT_USER, "take me to the superuser tab")
    assert nav["params"] == {"check": "Business_defined_checks"}
    assert "note" not in res


def test_old_style_route_and_params_still_work():
    nav, res = navigation.prepare_navigation({"route": "/user-management", "params": {"tab": "users"}}, ALL, "")
    assert nav["destination"] == "users.users"
    nav, _ = navigation.prepare_navigation({"route": "/structured-explorer"}, ALL, "")
    assert nav["route"] == "/gra-data-domains"
    nav, _ = navigation.prepare_navigation({"route": "/dqe?check=IT_Checks"}, ALL, "")
    assert nav["destination"] == "dqe.it"


def test_made_up_places_are_refused():
    for args in ({"route": "/reports"}, {"destination": "reports.q3"}, {"route": "dqe"}, {}):
        nav, res = navigation.prepare_navigation(args, ALL, "")
        assert nav is None, args
        assert res["status"].startswith("not sent")


def test_bad_param_values_are_dropped():
    nav, _ = navigation.prepare_navigation(
        {"destination": "users", "params": {"tab": "passwords", "evil": "1"}}, ALL, "")
    assert nav["params"] == {}


def test_extra_params_pass_through():
    nav, _ = navigation.prepare_navigation(
        {"destination": "dataapi.docs", "params": {"api": "shipments"}}, ALL, "")
    assert nav["params"] == {"tab": "docs", "api": "shipments"}
    nav, _ = navigation.prepare_navigation(
        {"destination": "structured", "params": {"domain": "Regulatory Submissions", "open": "1"}}, ALL, "")
    assert nav["params"] == {"domain": "Regulatory Submissions", "open": "1"}


def test_delta_wording_switches_bdc_to_semi_monthly():
    nav, _ = navigation.prepare_navigation({"destination": "bdc.failed"}, ALL, "show failed rows in semi-monthly")
    assert nav["params"]["mode"] == "delta"
    nav, _ = navigation.prepare_navigation({"destination": "bdc.failed"}, ALL, "where is the delta report")
    assert "mode" not in nav["params"]


def test_audit_time_words_set_the_range():
    nav, _ = navigation.prepare_navigation({"destination": "audit"}, ALL, "show me the audit log for today")
    assert nav["params"]["range"] == "24h"


def test_button_destinations_carry_the_click():
    nav, _ = navigation.prepare_navigation({"destination": "studio.copier"}, ALL, "")
    assert nav["params"] == {"pane": "snowflake"} and nav["then_click"] == "Copy tables"


def test_already_there_is_not_sent_again():
    nav, res = navigation.prepare_navigation({"destination": "users.roles"}, ALL, "show me roles", current_id="users.roles")
    assert nav is None and "NOTHING CHANGED" in res["status"]


def test_tour_steps_are_expanded_and_filtered():
    args = {"steps": [
        {"destination": "bdc.trends", "target": "Rule Selector", "say": "a"},
        {"destination": "users.roles", "say": "b"},
        {"route": "/nowhere", "say": "c"},
        {"target": "x", "say": "d"},
    ]}
    out, notes = navigation.prepare_tour(args, IT_USER)
    assert [s["say"] for s in out["steps"]] == ["a", "d"]
    assert out["steps"][0]["params"]["tab"] == "trends"
    assert len(notes) == 2


# --- where the user is ---

@pytest.mark.parametrize("url,want", [
    ("https://x/dqe?check=Business_defined_checks&view=standard&tab=trends", "bdc.trends"),
    ("https://x/dqe?check=Business_defined_checks&view=standard&tab=admin&dqtab=rule-registry", "bdc.superuser.registry"),
    ("https://x/dqe?check=Business_defined_checks&view=standard&tab=failed-rows&mode=delta", "bdc.failed"),
    ("https://x/user-management?tab=roles", "users.roles"),
    ("https://x/unstructured-explorer?doctype=fda&fdadashboard=ectd_docs", "un.fda.dash.ectd_docs"),
    ("https://x/data-api?api=shipments&tab=docs", "dataapi.docs"),
    ("https://x/audit?cat=Roles&range=7d", "audit.security"),
    ("https://x/dqe", "dqe"),
    ("https://x/dashboard", "dashboard"),
])
def test_current_location(url, want):
    assert navigation.current_destination({"url": url})["id"] == want


def test_current_location_without_url_uses_route_and_search():
    got = navigation.current_destination({"route": "/rag", "search": "?tab=rimdocs"})
    assert got["id"] == "rag.rimdocs"


def test_hint_block_says_where_and_what():
    cur = navigation.current_destination({"url": "https://x/dqe?check=Business_defined_checks&view=standard&tab=trends"})
    hint = navigation.hint_block("take me to the review tab", IT_USER, cur)
    assert "USER IS CURRENTLY AT" in hint and "Trends" in hint
    assert "bdc.review" in hint and "NO ACCESS" in hint
    hint = navigation.hint_block("take me to the overview", ALL, None)
    assert "AMBIGUOUS" in hint
    hint = navigation.hint_block("take me to the quarterly budget page", ALL, None)
    assert "No page or tab name in this message matched" in hint
    # a data question phrased with "show me" must be told to just answer
    hint = navigation.hint_block("show me failing rules", ALL, None)
    assert "ignore this block and answer normally" in hint
    assert navigation.hint_block("how many rules failed?", ALL, None) is None


def test_knowledge_markdown_covers_every_destination():
    md = pm.knowledge_markdown()
    for x in pm.DESTINATIONS.values():
        assert pm.breadcrumb(x.id) in md


def test_no_upgrade_when_user_steers_away():
    nav, res = navigation.prepare_navigation({"destination": "dqe"}, ALL,
                                             "just the dqe page, not business checks")
    assert nav["params"] == {} and "note" not in res


# --- Quarterly / Semi-Monthly (the "said it switched but didn't" bug) ---

FAILED_Q = {"check": "Business_defined_checks", "view": "standard", "tab": "failed-rows"}
FAILED_D = {**FAILED_Q, "mode": "delta"}


def test_switch_to_semi_monthly_on_the_same_tab_is_sent():
    # user on Failed Rows (Quarterly) asks for Semi-Monthly: same tab, different view -> must go out
    nav, res = navigation.prepare_navigation({"destination": "bdc.failed", "params": {"mode": "delta"}}, ALL,
                                             "yes", current_id="bdc.failed", current_params=FAILED_Q)
    assert nav is not None and nav["params"]["mode"] == "delta", res


def test_yes_to_an_offer_carries_the_offer():
    history = [{"role": "user", "content": "can I see semi-monthly data here?"},
               {"role": "assistant", "content": "Yes - want me to switch this tab to Semi-Monthly?"},
               {"role": "user", "content": "yes"}]
    q = navigation.effective_request(history)
    assert "Semi-Monthly" in q
    # even if the model forgets the param, the offer it answered supplies it
    nav, _ = navigation.prepare_navigation({"destination": "bdc.failed"}, ALL, q,
                                           current_id="bdc.failed", current_params=FAILED_Q)
    assert nav["params"]["mode"] == "delta"


def test_effective_request_leaves_real_questions_alone():
    history = [{"role": "assistant", "content": "Semi-Monthly is on."},
               {"role": "user", "content": "open trends"}]
    assert navigation.effective_request(history) == "open trends"


def test_nothing_to_change_is_reported_as_nothing_changed():
    nav, res = navigation.prepare_navigation({"destination": "bdc.failed", "params": {"mode": "delta"}}, ALL,
                                             "switch to semi-monthly", current_id="bdc.failed", current_params=FAILED_D)
    assert nav is None and "NOTHING CHANGED" in res["status"] and "do NOT say" in res["tell_user"]


def test_switch_back_to_quarterly():
    nav, _ = navigation.prepare_navigation({"destination": "bdc.failed"}, ALL, "go back to quarterly",
                                           current_id="bdc.failed", current_params=FAILED_D)
    assert nav is not None and "mode" not in nav["params"]
    nav, _ = navigation.prepare_navigation({"destination": "bdc.failed", "params": {"mode": "quarterly"}}, ALL, "",
                                           current_id="bdc.failed", current_params=FAILED_D)
    assert nav is not None and "mode" not in nav["params"]


def test_changing_tab_keeps_semi_monthly():
    nav, _ = navigation.prepare_navigation({"destination": "bdc.trends"}, ALL, "open trends",
                                           current_id="bdc.failed", current_params=FAILED_D)
    assert nav["params"]["mode"] == "delta" and nav["params"]["tab"] == "trends"


def test_changing_api_tab_keeps_the_selected_api():
    nav, _ = navigation.prepare_navigation({"destination": "dataapi.endpoints"}, ALL, "show me the endpoints",
                                           current_id="dataapi.docs", current_params={"api": "shipments", "tab": "docs"})
    assert nav["params"] == {"tab": "endpoints", "api": "shipments"}


def test_location_says_which_cadence_is_showing():
    q = navigation.current_destination({"url": "https://x/dqe?check=Business_defined_checks&view=standard&tab=failed-rows"})
    d = navigation.current_destination({"url": "https://x/dqe?check=Business_defined_checks&view=standard&tab=failed-rows&mode=delta"})
    assert "Quarterly data" in q["breadcrumb"] and "Semi-Monthly" in d["breadcrumb"]



def test_options_the_user_cant_open_dont_make_it_ambiguous():
    it_user = {"viewDashboard": True, "viewDQE": True, "viewBusinessChecks": True, "viewBDCExecutive": True}
    r = pm.resolve("take me to the overview", it_user)
    assert r["best"]["id"] == "bdc.overview" and not r["ambiguous"]
    # admin can open both, so it's still a question
    assert pm.resolve("take me to the overview", ALL)["ambiguous"]
    # nobody-can-open-any still reports the (closed) best so the hint can say "no access"
    r = pm.resolve("open the data table", {"viewDashboard": True})
    assert r["best"] and not r["best"]["allowed"]


def test_ambiguous_hint_tells_the_model_not_to_add_options():
    hint = navigation.hint_block("take me to the overview", ALL, None)
    assert "bdc.overview" in hint and "dataapi.overview" in hint and "Offer EXACTLY these options" in hint