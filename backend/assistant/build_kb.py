"""
Build the assistant's knowledge base.

Run this whenever the portal changes (and in CI on merge to main):

    python -m assistant.build_kb

What it does:
  1. Harvests structured facts straight out of the code so they can't go stale:
       - every backend route (method + path + docstring) from app.py
       - every frontend page/route from App.jsx
       - the sidebar labels, and every page/tab/sub-tab from portal_map.py
  2. Reads the hand-written docs in knowledge/*.md (the stuff code can't tell you:
     what a feature is *for*, business meaning of a rule, how-tos).
  3. Splits it all into chunks, embeds each with Azure OpenAI, writes portal_kb.jsonl.

The harvested-from-code part is the thing that keeps the KB honest as the portal
evolves — you don't re-document routes by hand, you regenerate.
"""

import json
import os
import re
import ast
from pathlib import Path

from openai import AzureOpenAI

from . import env  # noqa: F401  -- loads backend/.env into os.environ

HERE = Path(__file__).parent
BACKEND = HERE.parent
REPO = BACKEND.parent
KB_OUT = HERE / "knowledge" / "portal_kb.jsonl"

EMBED_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBED_DEPLOYMENT", "text-embedding-3-large")


def _embed_env(name, default=None):
    # prefer the embedding-specific var, fall back to the generic/chat one
    return os.getenv(f"AZURE_OPENAI_EMBED_{name}") or os.getenv(f"AZURE_OPENAI_{name}", default)


def client():
    endpoint = _embed_env("ENDPOINT")
    key = _embed_env("API_KEY")
    version = _embed_env("API_VERSION", "2024-08-01-preview")
    if not endpoint:
        raise RuntimeError(
            "Embedding config missing. Set AZURE_OPENAI_EMBED_ENDPOINT (or the generic "
            "AZURE_OPENAI_ENDPOINT) in backend/.env"
        )
    if key:
        return AzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=version)
    # No key set -> try AAD/managed identity (DefaultAzureCredential) before giving up.
    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        provider = get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
        return AzureOpenAI(azure_endpoint=endpoint, azure_ad_token_provider=provider, api_version=version)
    except Exception as e:
        raise RuntimeError(
            "No AZURE_OPENAI_EMBED_API_KEY set and no usable managed identity/AAD credential "
            f"found ({e}). Either set the API key in backend/.env, or run this where a managed "
            "identity / az login is available."
        )


def _router_prefixes(tree) -> dict:
    """{router_var_name: prefix} for every `x = APIRouter(prefix="...")` in a
    file, plus "app" mapped to "" so the main FastAPI() app is covered by the
    same lookup as every feature router."""
    prefixes = {"app": ""}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "APIRouter"):
            continue
        prefix = ""
        for kw in call.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                prefix = kw.value.value
        for target in node.targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = prefix
    return prefixes


def harvest_backend_routes() -> list[dict]:
    """Pull (method, path, docstring) for every route across the WHOLE backend,
    not just app.py. Most feature endpoints actually live in their own file's
    APIRouter (audit.py, data_api.py, review.py, unstructured.py, ...) and get
    wired in with app.include_router(...) — those routers used to be invisible
    to this harvester entirely, silently dropping most of the portal's routes
    from the KB. Walking every top-level backend/*.py file and resolving each
    router's own `prefix=` means a brand-new router is picked up automatically
    here too, no per-file wiring needed.

    Parsed with ast, not regex — function signatures here span multiple lines and
    contain nested parens (Query(...), Depends(...)) that a regex chokes on, so we
    here we let Python's own parser find the decorated functions and their docstrings.
    """
    out = []
    for py_file in sorted(BACKEND.glob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        prefixes = _router_prefixes(tree)

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            method = path = None
            for dec in node.decorator_list:
                # looking for @app.get("/path"...) / @some_router.post(...) etc.
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                    continue
                verb = dec.func.attr.lower()
                if verb not in ("get", "post", "put", "delete", "patch"):
                    continue
                obj = dec.func.value
                if not (isinstance(obj, ast.Name) and obj.id in prefixes):
                    continue
                if dec.args and isinstance(dec.args[0], ast.Constant):
                    method, path = verb.upper(), prefixes[obj.id] + dec.args[0].value
                break

            if not path:
                continue
            doc = (ast.get_docstring(node) or "").strip()
            if not doc:
                continue
            out.append({
                "title": f"API {method} {path}",
                "source": f"backend/{py_file.name}",
                "text": f"Endpoint {method} {path}. {doc}",
            })
    return out


def harvest_frontend_routes() -> list[dict]:
    """Pull the route -> page mapping out of App.jsx.

    The element for a route is usually wrapped (<PermissionRoute><DQEngine/></...>),
    so we grab the whole element={...} block and take the last/innermost component
    that isn't one of those wrappers — that's the actual page.
    """
    src = (REPO / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")
    wrappers = {"PermissionRoute", "ProtectedRoute", "Suspense", "Routes", "Route",
                "Navigate", "Fragment", "Layout"}
    out = []
    # capture: path="/x" ... element={ ...block... }
    for m in re.finditer(r'path="(/[^"]*)"\s*\n?\s*element=\{(.*?)\}\s*\n?\s*/>', src, re.DOTALL):
        path, block = m.group(1), m.group(2)
        if path in ("/", "/*"):
            continue
        comps = re.findall(r'<(\w+)', block)
        real = next((c for c in reversed(comps) if c not in wrappers), None)
        if not real:
            continue
        out.append({
            "title": f"Page {path}",
            "source": "frontend/src/App.jsx",
            "text": f"The portal route {path} renders the {real} page.",
        })
    return out


def harvest_sidebar() -> list[dict]:
    """The sidebar as a user sees it: label, route and the permission behind it.

    App.jsx only gives component names, and nobody calls it "the GraDataDomains
    page" — they call it Structured Explorer, because that is what the sidebar
    says. Anything writing to users needs the label, not the component.
    """
    sidebar = REPO / "frontend" / "src" / "components" / "Sidebar.jsx"
    try:
        src = sidebar.read_text(encoding="utf-8")
    except OSError:
        return []

    items = []
    for m in re.finditer(
        r'\{\s*id:\s*"([^"]+)"\s*,\s*label:\s*"([^"]+)"[^}]*?path:\s*"([^"]+)"'
        r'[^}]*?permission:\s*"([^"]+)"', src):
        items.append({"id": m.group(1), "label": m.group(2),
                      "path": m.group(3), "permission": m.group(4)})
    if not items:
        return []

    lines = ["The SmartHub sidebar, top to bottom. This is what each page is "
             "called in the UI, where it lives, and the permission needed to see it."]
    for i in items:
        lines.append(f"- {i['label']} ({i['path']}) — permission {i['permission']}.")
    return [{
        "title": "Sidebar navigation and page names",
        "source": "frontend/src/components/Sidebar.jsx",
        "text": "\n".join(lines),
    }]


def harvest_seed_docs() -> list[dict]:
    """Hand-written markdown in knowledge/. Each '## heading' becomes one chunk.

    Only reviewed, trusted markdown is embedded. Anything quarantined (the raw
    pending_corrections queue) is a .jsonl, not .md, so it's already excluded; we
    also skip any file whose name starts with '_' as a scratch/private convention.
    """
    out = []
    for md in sorted((HERE / "knowledge").glob("*.md")):
        if md.name.startswith("_"):
            continue
        text = md.read_text(encoding="utf-8")
        # split on h2s, keep the heading with its body
        blocks = re.split(r'\n(?=## )', text)
        for b in blocks:
            b = b.strip()
            if not b:
                continue
            heading = b.splitlines()[0].lstrip("# ").strip()
            out.append({"title": heading or md.stem, "source": f"knowledge/{md.name}", "text": b})
    return out


def harvest_portal_map() -> list[dict]:
    """Every page, tab and sub-tab with its URL, permission and other names,
    straight from portal_map.py - the same table navigate_to uses, so what the
    assistant reads and where it sends people can't disagree."""
    from . import portal_map
    out = []
    for block in re.split(r'\n(?=## )', portal_map.knowledge_markdown()):
        block = block.strip()
        if not block.startswith("## "):
            continue
        out.append({"title": block.splitlines()[0].lstrip("# ").strip(),
                    "source": "assistant/portal_map.py", "text": block})
    return out


def chunk(text: str, size: int = 1200) -> list[str]:
    if len(text) <= size:
        return [text]
    parts, cur = [], ""
    pieces = [p for para in text.split("\n\n") for p in (para.split("\n") if len(para) > size else [para])]
    heading = text.splitlines()[0] if text.startswith("#") else ""
    for para in pieces:
        if len(cur) + len(para) > size and cur:
            parts.append(cur.strip())
            # keep the section heading on every piece so a chunk still says what it's about
            cur = (heading + "\n") if heading and not para.startswith("#") else ""
        cur += para + "\n\n"
    if cur.strip():
        parts.append(cur.strip())
    return parts


def main():
    docs = (harvest_backend_routes() + harvest_frontend_routes()
            + harvest_sidebar() + harvest_portal_map() + harvest_seed_docs())

    records = []
    for d in docs:
        for piece in chunk(d["text"]):
            records.append({"title": d["title"], "source": d["source"], "text": piece})

    print(f"harvested {len(records)} chunks; embedding with {EMBED_DEPLOYMENT}...")
    c = client()
    # embed in batches
    for i in range(0, len(records), 64):
        batch = records[i:i + 64]
        resp = c.embeddings.create(model=EMBED_DEPLOYMENT, input=[r["text"] for r in batch])
        for r, e in zip(batch, resp.data):
            r["embedding"] = e.embedding

    KB_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(KB_OUT, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    print(f"wrote {len(records)} chunks -> {KB_OUT}")


if __name__ == "__main__":
    main()