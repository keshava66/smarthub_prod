"""
The portal map against the real frontend source. If a page renames a tab id,
drops a route or changes a permission, these fail before the assistant starts
sending people to places that don't exist.

Run from backend/:  python -m pytest assistant/tests -q
"""

import re
from pathlib import Path

import pytest

from assistant import portal_map as pm

FRONT = Path(__file__).resolve().parents[3] / "frontend" / "src"
PAGES = FRONT / "pages"


def src(rel):
    return (FRONT / rel).read_text(encoding="utf-8")


def ids_in(text):
    return set(re.findall(r"'([A-Za-z0-9_-]+)'", text))


def block(text, start_pat, end="]"):
    m = re.search(start_pat, text)
    assert m, f"couldn't find {start_pat!r} in frontend source - update this test"
    tail = text[m.end():]
    return tail[: tail.index(end) + 1]


# what each page actually accepts, read out of its source
def frontend_values():
    dqe = src("pages/DQEngine.jsx")
    bdc = src("pages/BusinessDefinedChecks.jsx")
    um = src("pages/UserManagement.jsx")
    rag = src("pages/RAGPage.jsx")
    cc = src("pages/CommunicationCenter.jsx")
    ds = src("pages/dataStudio/panes.js")
    un = src("pages/Unstructured.jsx")
    api = src("pages/dataApi/DataAPIExport.jsx")
    api_lib = src("pages/dataApi/lib.js")
    audit = src("pages/AuditPage.jsx")
    gra = src("pages/GraDataDomains.jsx")
    fz = src("pages/FuzzyMatch.jsx")
    return {
        ("/fuzzy-match", "m"): {"meta"} if "sp.get('m') === 'meta'" in fz else set(),
        ("/dqe", "check"): ids_in(block(dqe, r"const VALID_CHECKS\s*=\s*\[")),
        ("/dqe", "tab"): ids_in(block(bdc, r"const VALID_TABS\s*=\s*\[")),
        ("/dqe", "dqtab"): set(re.findall(r"id:\s*'([a-z-]+)'", block(bdc, r"const DQ_TABLE_TABS\s*=\s*\["))),
        ("/dqe", "view"): {"standard"} if "searchParams.get('view') !== 'standard'" in bdc else set(),
        ("/dqe", "mode"): {"delta"} if "searchParams.get('mode') === 'delta'" in bdc else set(),
        ("/user-management", "tab"): ids_in(block(um, r"const VALID_TABS\s*=\s*\[")),
        ("/rag", "tab"): set(re.findall(r"id:\s*'([a-z-]+)'", block(rag, r"const ALL_TABS\s*=\s*\["))),
        ("/communication-center", "tab"): ids_in(block(cc, r"const TABS\s*=\s*\[")),
        ("/data-studio", "pane"): set(re.findall(r"id:\s*'([a-z0-9-]+)'", ds)),
        ("/unstructured-explorer", "doctype"): ids_in(block(un, r"const docTypeIds\s*=\s*\[")),
        ("/unstructured-explorer", "tab"): ids_in(block(un, r"const tabIds\s*=\s*\[", end=";")),
        ("/unstructured-explorer", "epodtab"): ids_in(block(un, r"const epodTabIds\s*=\s*\[")),
        ("/unstructured-explorer", "nipotab"): ids_in(block(un, r"const nipoTabIds\s*=\s*\[")),
        ("/unstructured-explorer", "fdatab"): ids_in(block(un, r"const fdaTabIds\s*=\s*\[")),
        ("/unstructured-explorer", "immbottab"): ids_in(block(un, r"const immbotTabIds\s*=\s*\[")),
        ("/unstructured-explorer", "vrtab"): ids_in(block(un, r"const vrTabIds\s*=\s*\[")),
        ("/unstructured-explorer", "vrscope"): ids_in(block(un, r"const vrScopeIds\s*=\s*\[")),
        ("/unstructured-explorer", "fdadashboard"):
            set(re.findall(r"id:\s*'([a-z0-9_]+)'", block(un, r"const FDA_DASHBOARDS\s*=\s*\["))),
        ("/data-api", "tab"): set(re.findall(r"\['([a-z]+)',", block(api_lib, r"export const TABS\s*=\s*\[", end="];"))),
        ("/data-api", "new"): {"1"} if "sp.get('new') === '1'" in api else set(),
        ("/audit", "view"): {"analytics", "stream"} if "params.get('view') === 'analytics'" in audit else set(),
        ("/audit", "cat"): ids_in(block(audit, r"const CAT_NAMES\s*=\s*\[")) | {"all"},
        ("/audit", "range"): ids_in(block(audit, r"\['24h'", end="]")) | {"24h"},
        ("/gra-data-domains", "view"): {"directory"} if "params.get('view') === 'directory'" in gra else set(),
        ("/gra-data-domains", "tab"): {"records", "people"} if "params.get('tab') === 'records'" in gra else set(),
    }


FRONT_VALUES = frontend_values()


@pytest.mark.parametrize("dest_id", sorted(pm.DESTINATIONS))
def test_every_destination_param_is_one_the_page_reads(dest_id):
    route, params = pm.url_for(dest_id)
    raw = pm.DESTINATIONS[dest_id].params
    assert params == raw, f"{dest_id}: clean_params dropped something from {raw}"
    for k, v in params.items():
        accepted = FRONT_VALUES.get((route, k))
        if accepted is None and pm.ALLOWED_PARAMS[route].get(k, 1) is None:
            continue  # free text (q, user...) - covered by test_free_text_params_are_read
        assert accepted is not None, f"{dest_id}: no frontend check for {route}?{k} - add one here"
        assert v in accepted, f"{dest_id}: {route}?{k}={v} but the page only accepts {sorted(accepted)}"


PAGE_FILES = {
    "/audit": ["pages/AuditPage.jsx"],
    "/data-api": ["pages/dataApi/DataAPIExport.jsx", "pages/dataApi/Workspace.jsx"],
    "/gra-data-domains": ["pages/GraDataDomains.jsx"],
    "/data-studio": ["pages/dataStudio/s3/S3Pane.jsx", "pages/dataStudio/DataStudio.jsx"],
    "/unstructured-explorer": ["pages/Unstructured.jsx"],
    "/fuzzy-match": ["pages/FuzzyMatch.jsx"],
}


def test_free_text_params_are_read():
    for route, keys in pm.ALLOWED_PARAMS.items():
        for k, values in keys.items():
            if values is not None:
                continue
            text = "".join(src(f) for f in PAGE_FILES[route])
            assert re.search(r"\.get\('" + k + r"'\)", text), f"{route} never reads ?{k}"


def test_allowed_params_match_frontend():
    for (route, key), accepted in FRONT_VALUES.items():
        ours = pm.ALLOWED_PARAMS[route][key]
        assert ours is not None
        assert ours == accepted, f"{route}?{key}: map allows {sorted(ours)}, page accepts {sorted(accepted)}"


def test_routes_match_app_jsx():
    app = src("App.jsx")
    paths = set(re.findall(r'path="(/[a-z0-9-]+)"', app)) - {"/login", "/logout", "/audit-edit", "/audit-edit-force"}
    assert paths == pm.ROUTES


def test_route_permissions_match_app_jsx():
    app = src("App.jsx")
    for path, perm in re.findall(r'path="(/[a-z0-9-]+)"\s*element=\{\s*<PermissionRoute permission="(\w+)"', app):
        if path.startswith("/audit-edit"):
            continue
        root = next(x for x in pm.DESTINATIONS.values() if x.route == path and x.parent is None)
        assert perm in pm.required_perms(root.id), f"{path} needs {perm} in the frontend"


def test_tab_permissions_match_frontend():
    rag = src("pages/RAGPage.jsx")
    for tab, perm in re.findall(r"id:\s*'([a-z-]+)'[^}]*permission:\s*'(\w+)'", block(rag, r"const ALL_TABS\s*=\s*\[")):
        dest = pm.match_url("/rag", {"tab": tab})
        assert perm in pm.required_perms(dest.id)
    for pane, perm in re.findall(r"id:\s*'([a-z0-9]+)'.*?permission:\s*'(\w+)'", src("pages/dataStudio/panes.js")):
        dest = pm.match_url("/data-studio", {"pane": pane})
        assert perm in pm.required_perms(dest.id)
    bdc = src("pages/BusinessDefinedChecks.jsx")
    assert "const isAdmin = hasPermission('viewBDCSuperuser')" in bdc
    for tab in ("admin", "review"):
        dest = pm.match_url("/dqe", {**pm.STD, "tab": tab})
        assert "viewBDCSuperuser" in pm.required_perms(dest.id)


def test_every_permission_is_real():
    import auth
    for x in pm.DESTINATIONS.values():
        for p in x.perms:
            assert p in auth.ALL_PERMISSIONS, f"{x.id} uses unknown permission {p}"


def test_sidebar_and_dashboard_links_all_resolve():
    side = src("components/Sidebar.jsx")
    for path in re.findall(r'path:\s*"(/[a-z0-9-]+)"', side):
        assert pm.match_url(path, {}) is not None, path
    tools = src("config/tools.js")
    for link in re.findall(r"path:\s*'(/[^']+)'", tools):
        path, _, qs = link.partition("?")
        params = dict(p.split("=") for p in qs.split("&") if p)
        hit = pm.match_url(path, params)
        assert hit is not None, link
        assert all(hit.params.get(k) == v for k, v in params.items()), f"{link} lands on {hit.id}"


LABEL_EXCEPTIONS = {"dqe.dv", "un.haq"}  # labels built with escapes/unicode in the source


@pytest.mark.parametrize("dest_id", sorted(pm.DESTINATIONS))
def test_name_is_what_the_ui_shows(dest_id):
    """The name is what we tell users to look for, so it has to be on screen."""
    x = pm.DESTINATIONS[dest_id]
    if dest_id in LABEL_EXCEPTIONS:
        return
    everything = "\n".join(p.read_text(encoding="utf-8") for p in FRONT.rglob("*.js*"))
    name = x.name.replace("'", "\\u2019") if x.name == "What's new" else x.name
    assert name in everything or x.name in everything, f"{dest_id}: '{x.name}' isn't a label anywhere in the UI"


def test_then_click_targets_exist():
    everything = "\n".join(p.read_text(encoding="utf-8") for p in FRONT.rglob("*.jsx"))
    for x in pm.DESTINATIONS.values():
        if x.then_click:
            assert x.then_click in everything


def test_every_route_has_a_root_and_parents_exist():
    for x in pm.DESTINATIONS.values():
        if x.parent:
            assert x.parent in pm.DESTINATIONS, x.id
            assert pm.DESTINATIONS[x.parent].route == x.route, x.id
    roots = {x.route for x in pm.DESTINATIONS.values() if x.parent is None}
    assert roots == pm.ROUTES


def test_no_two_destinations_share_a_url():
    seen = {}
    for x in pm.DESTINATIONS.values():
        if x.then_click or x.same_url_as:
            continue
        key = (x.route, tuple(sorted(x.params.items())))
        assert key not in seen, f"{x.id} and {seen[key]} are the same URL"
        seen[key] = x.id


def test_describe_url_round_trips():
    for x in pm.DESTINATIONS.values():
        if x.then_click or x.same_url_as:
            continue
        route, params = pm.url_for(x.id)
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        got = pm.describe_url(f"https://portal.example{route}{'?' + qs if qs else ''}")
        assert got and got["id"] == x.id, f"{x.id} -> {got}"


def test_frontend_route_aliases_cover_every_route():
    js = src("assistant/assistantActions.js")
    table = block(js, r"const ROUTE_ALIASES\s*=\s*\{", end="};")
    assert set(re.findall(r"'(/[a-z0-9-]+)'\s*:", table)) == pm.ROUTES


def test_no_alias_repeats_an_ancestors_alias():
    """If a tab and its page share an alias the page swallows the words and the
    tab can never win - give the alias to the most specific place only."""
    for x in pm.DESTINATIONS.values():
        mine = {a.lower() for a in (x.name,) + tuple(x.aliases)}
        for anc in pm.lineage(x.id)[:-1]:
            theirs = {a.lower() for a in (anc.name,) + tuple(anc.aliases)}
            assert not (mine & theirs), f"{x.id} and {anc.id} both use {sorted(mine & theirs)}"