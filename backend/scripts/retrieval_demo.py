"""Local demo and evidence tool for Data API semantic / hybrid retrieval.

Run from backend/:

  python scripts/retrieval_demo.py local-db [--stop]        # no Docker? embedded PostgreSQL via pip install pgserver
  python scripts/retrieval_demo.py setup    --dsn postgresql://postgres:localdev@localhost:5433/retrieval_demo
  python scripts/retrieval_demo.py serve    [--port 8000] [--mcp]
  python scripts/retrieval_demo.py evidence [--port 8000] [--runs 20] [--with-tests] [--mcp]

Everything lives in backend/.retrieval_demo/ (gitignored): source credentials
file, state and evidence folders. The Data API metadata goes in its own schema
(retrieval_demo_meta) inside the demo database, so the portal's real
smarthub_core and data_validation_config.ini are never read or written.
Semantic search is configured through the same admin API the portal uses.

Embeddings: by default the real Azure OpenAI deployment is used, configured
exactly like the portal (backend/.env, AZURE_OPENAI_EMBED_* falling back to
AZURE_OPENAI_*). --fake-embeddings (setup only; serve follows the choice)
uses deterministic word-hashing vectors and never calls Azure. That mode
proves the wiring, not semantic quality, and every output says so.

The demo corpus is synthetic regulatory-style text generated below. It is
not real product data.
"""

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import platform
import re
import secrets
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

BACKEND = Path(__file__).resolve().parent.parent
DEMO_DIR = BACKEND / ".retrieval_demo"
STATE = DEMO_DIR / "state.json"
SCHEMA = "retrieval_demo"
META_SCHEMA = "retrieval_demo_meta"
SLUG = "retrieval-demo"
FAKE_DIMS = 256

sys.path.insert(0, str(BACKEND))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _isolate_env(dsn):
    """Point the Data API at the demo database and folder before data_api is
    imported, so a shared SmartHub database is never touched."""
    os.environ["SMARTHUB_DB_DSN"] = dsn
    os.environ["SMARTHUB_DB_SCHEMA"] = META_SCHEMA
    os.environ["DATA_VALIDATION_CONFIG_PATH"] = str(DEMO_DIR / "data_validation_config.ini")
    os.environ.pop("DATA_API_METADATA_DSN", None)


def _load_dotenv():
    try:
        import assistant.env  # noqa: F401  loads backend/.env the same way the portal does
    except Exception:
        pass


def fake_embed(model, inputs):
    """Deterministic hashed bag-of-words vectors. Texts sharing words get
    similar vectors, which is enough to exercise every code path. Not a
    semantic model."""
    out = []
    for text in inputs:
        vec = [0.0] * model.dimensions
        for word in re.findall(r"[a-z0-9]{3,}", text.lower()):
            h = hashlib.sha256(word.encode()).digest()
            vec[int.from_bytes(h[:4], "big") % model.dimensions] += 1.0 if h[4] % 2 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        if norm == 1.0 and not any(vec):
            vec[0] = 1.0
        out.append([v / norm for v in vec])
    return out


def _state():
    if not STATE.exists():
        sys.exit("No demo found. Run: python scripts/retrieval_demo.py setup --dsn ...")
    return json.loads(STATE.read_text(encoding="utf-8"))


def _parse_dsn(dsn):
    u = urlparse(dsn)
    if u.scheme not in ("postgresql", "postgres") or not u.path.strip("/"):
        sys.exit("--dsn must look like postgresql://user:password@host:port/database")
    # A Unix-socket server (e.g. pgserver on macOS/Linux) passes its socket
    # directory as ?host=/path instead of a hostname.
    query_host = parse_qs(u.query).get("host", [None])[0]
    return {"host": u.hostname or query_host or "localhost", "port": u.port or 5432,
            "user": unquote(u.username or "postgres"), "password": unquote(u.password or ""),
            "database": u.path.strip("/")}


def _keyword_dsn(p):
    # libpq keyword form; values quoted so paths with spaces survive.
    q = lambda v: "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"
    return (f"host={q(p['host'])} port={p['port']} user={q(p['user'])} "
            f"password={q(p['password'])} dbname={q(p['database'])}")


# --------------------------------------------------------------------------
# synthetic corpus (regulatory-style, fictional)
# --------------------------------------------------------------------------

TOPICS = [
    ("Sterility assurance report", "Oncology", [
        "Sterility failure observed in the aseptic filling line during media fill run {n}.",
        "Environmental monitoring showed Grade A excursions near the filling needles.",
        "Root cause investigation identified a torn glove port on isolator {n}.",
        "Corrective actions include revised gowning qualification and additional smoke studies.",
        "Three consecutive successful media fills were completed before batch release resumed.",
        "Sterility test results for the affected lots were re-reviewed by quality assurance.",
    ]),
    ("Complete response letter summary", "Immunology", [
        "The agency issued a complete response letter citing manufacturing facility deficiencies.",
        "Inspection observations referenced inadequate cleaning validation for shared equipment.",
        "A Class 2 resubmission is planned after the facility is re-inspected.",
        "Deficiencies in the drug substance impurity profile must also be addressed.",
        "The sponsor requested a Type A meeting to discuss the resubmission timeline.",
        "No clinical deficiencies were cited in the complete response letter.",
    ]),
    ("Stability data summary", "Cardiology", [
        "Long-term stability at 25C and 60 percent relative humidity supports a 24-month shelf life.",
        "Accelerated stability at 40C and 75 percent relative humidity showed a slight assay decline.",
        "One out-of-specification result for dissolution was traced to a sampling error.",
        "Photostability testing confirmed the need for light-protective packaging.",
        "Shelf life extrapolation follows the ICH Q1E statistical approach.",
        "In-use stability after reconstitution is limited to 8 hours at room temperature.",
    ]),
    ("Container closure integrity assessment", "Oncology", [
        "Container closure integrity was verified by helium leak testing on all vial configurations.",
        "Dye ingress testing was retained as an orthogonal method for the prefilled syringe.",
        "Stopper compression set was evaluated after 12 months of storage.",
        "No leaks were detected above the maximum allowable leakage limit.",
        "The crimping process parameters were tightened after a capping deviation.",
        "Deterministic methods replace probabilistic microbial ingress testing going forward.",
    ]),
    ("Labeling update", "Nephrology", [
        "The labeling update adds a dose reduction for patients with severe renal impairment.",
        "A boxed warning for hepatotoxicity remains unchanged in this revision.",
        "Drug interaction language with strong CYP3A4 inhibitors was expanded.",
        "Pediatric use information was updated with the results of the recent study.",
        "Patient information leaflet readability testing was repeated after the changes.",
        "The revised label will be implemented within 90 days of approval.",
    ]),
    ("Health authority query response", "Neurology", [
        "The health authority asked for justification of nitrosamine impurity limits.",
        "A risk assessment concluded that NDSRI formation is unlikely under storage conditions.",
        "Confirmatory testing on three commercial batches found no nitrosamines above the limit.",
        "The acceptable intake was derived using the carcinogenic potency categorization approach.",
        "Control strategy updates include supplier qualification for nitrite-containing excipients.",
        "The response was submitted within the 30-day deadline requested by the agency.",
    ]),
    ("Clinical protocol amendment", "Oncology", [
        "Protocol amendment {n} broadens inclusion criteria to patients with prior immunotherapy.",
        "The dose escalation schedule was modified after the safety review committee meeting.",
        "An additional pharmacokinetic sampling timepoint was added on day 15.",
        "Exclusion criteria now include active hepatitis B infection.",
        "The primary endpoint of progression-free survival is unchanged.",
        "Sites must obtain ethics committee approval before implementing the amendment.",
    ]),
    ("Periodic safety update report", "Immunology", [
        "The periodic safety update report identified a new signal of interstitial lung disease.",
        "Cumulative exposure since approval is estimated at 48,000 patient-years.",
        "No new safety concerns were identified for injection site reactions.",
        "The benefit-risk balance remains positive for the approved indications.",
        "Signal detection used disproportionality analysis of spontaneous reports.",
        "Risk minimisation measures will be reviewed at the next update.",
    ]),
]
REGIONS = ["US", "EU", "JP"]


def build_corpus():
    docs, chunks = [], []
    for d in range(16):
        title, indication, texts = TOPICS[d % len(TOPICS)]
        region = REGIONS[d % 3]
        n = d + 1
        app_no = {"US": f"BLA 7612{n:02d}", "EU": f"EMEA/H/C/00{5100 + n}", "JP": f"JP-{30400 + n}"}[region]
        src = f"src-{n:03d}"
        docs.append({
            "chunk_source_id": src, "object_name": f"{title} {n:03d}", "jj_region_country": region,
            "a_status": "Approved" if d % 4 else "Draft", "r_version_label": f"{1 + d % 3}.0",
            "approved_date": (dt.date(2025, 1, 1) + dt.timedelta(days=17 * d)).isoformat(),
            "indication": None if d % 5 == 0 else indication, "application_number": app_no,
            "title": f"{title} ({region})",
        })
        for c, text in enumerate(texts):
            body = text.format(n=n)
            if c == 0:
                body = f"Application {app_no}. {body}"
            chunks.append({
                "chunk_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"retrieval-demo/{src}/{c}")),
                "chunk_source_id": src, "chunk_number": c + 1, "page_number": None if c == 5 else 1 + c // 2,
                "chunk_text": body, "document_id": f"DOC-{n:04d}", "region": region,
            })
    return docs, chunks


# --------------------------------------------------------------------------
# local-db (no Docker, no admin rights)
# --------------------------------------------------------------------------

LOCAL_PASSWORD = "localdev"


def cmd_local_db(args):
    """Embedded PostgreSQL 16 + pgvector from the `pgserver` wheel, stored in
    backend/.retrieval_demo/pgdata. For local testing only: pgserver bundles
    pgvector 0.6.2 and has not been released since June 2024."""
    try:
        import pgserver
    except ImportError:
        sys.exit("pgserver is not installed. Run: pip install pgserver   (wheels exist for Python 3.9-3.12 "
                 "on Windows, macOS and Linux)")
    import psycopg2
    DEMO_DIR.mkdir(exist_ok=True)
    data = DEMO_DIR / "pgdata"
    if args.stop:
        if not data.exists():
            sys.exit("No local database found.")
        pgserver.get_server(str(data), cleanup_mode="stop").cleanup()
        print("Local database stopped. Data kept in", data)
        return
    server = pgserver.get_server(str(data), cleanup_mode=None)  # keeps running after this command exits
    admin = server.get_uri("postgres")
    conn = psycopg2.connect(admin)
    conn.autocommit = True
    with conn.cursor() as cur:
        # The portal's PostgreSQL connector requires a password setting; the
        # embedded server trusts local connections, so this is only a formality.
        cur.execute(f"ALTER USER postgres PASSWORD '{LOCAL_PASSWORD}'")
        cur.execute("SELECT 1 FROM pg_database WHERE datname = 'retrieval_demo'")
        if not cur.fetchone():
            cur.execute("CREATE DATABASE retrieval_demo")
    conn.close()
    uri = server.get_uri("retrieval_demo").replace("postgres:@", f"postgres:{LOCAL_PASSWORD}@", 1)
    if "?host=" in uri:  # socket directory may contain spaces
        base, host = uri.split("?host=", 1)
        uri = base + "?host=" + quote(host, safe="/:\\")
    conn = psycopg2.connect(uri)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("SELECT version(), (SELECT extversion FROM pg_extension WHERE extname = 'vector')")
        version, pgv = cur.fetchone()
    conn.close()
    p = _parse_dsn(uri)
    print(f"Local database running: {version.split(' on ')[0]}, pgvector {pgv}")
    print("NOTE: pgvector 0.6.2 predates iterative index scans (0.8.0); the API warns about filtered")
    print("      semantic search on it. Fine for this demo; production should run 0.8.6.")
    print(f"\nDSN for setup:\n  {uri}")
    print(f"\nDSN for the test suite (DATA_API_TEST_PG_DSN):\n  {_keyword_dsn(p)}")
    print("\nStop it later with: python scripts/retrieval_demo.py local-db --stop")


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------

def cmd_setup(args):
    import psycopg2
    from psycopg2.extras import execute_values

    DEMO_DIR.mkdir(exist_ok=True)
    _load_dotenv()
    p = _parse_dsn(args.dsn)
    fake = args.fake_embeddings
    dims = FAKE_DIMS if fake else args.dimensions
    deployment = "fake-word-hashing" if fake else (args.deployment or os.environ.get("AZURE_OPENAI_EMBED_DEPLOYMENT")
                                                   or "text-embedding-3-small")

    from data_api_retrieval import EmbeddingModel, _azure_embed
    model = EmbeddingModel("demo-embed", {"deployment": deployment, "dimensions": dims,
                                          "send_dimensions": args.send_dimensions})
    docs, chunks = build_corpus()
    print(f"Embedding {len(chunks)} synthetic chunks with {'FAKE vectors' if fake else deployment} ({dims} dims)...")
    embed = fake_embed if fake else _azure_embed
    vectors = []
    for i in range(0, len(chunks), 64):
        batch = [c["chunk_text"] for c in chunks[i:i + 64]]
        got = embed(model, batch)
        for v in got:
            if len(v) != dims:
                sys.exit(f"Deployment returned {len(v)} dimensions, expected {dims}. Pass --dimensions {len(v)}.")
        vectors.extend(got)

    conn = psycopg2.connect(args.dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"""CREATE TABLE {SCHEMA}.rim_docs (
            chunk_source_id text PRIMARY KEY, object_name text, jj_region_country text, a_status text,
            r_version_label text, approved_date date, indication text, application_number text, title text)""")
        cur.execute(f"""CREATE TABLE {SCHEMA}.rim_chunks (
            chunk_id uuid PRIMARY KEY, chunk_source_id text REFERENCES {SCHEMA}.rim_docs, chunk_number int,
            page_number int, chunk_text text NOT NULL, document_id text, region text, embedding vector({dims}))""")
        execute_values(cur, f"INSERT INTO {SCHEMA}.rim_docs VALUES %s",
                       [tuple(d.values()) for d in docs])
        execute_values(cur, f"INSERT INTO {SCHEMA}.rim_chunks VALUES %s",
                       [(c["chunk_id"], c["chunk_source_id"], c["chunk_number"], c["page_number"], c["chunk_text"],
                         c["document_id"], c["region"], "[" + ",".join(repr(float(x)) for x in v) + "]")
                        for c, v in zip(chunks, vectors)])
        cur.execute(f"CREATE INDEX rim_chunks_embedding_hnsw ON {SCHEMA}.rim_chunks USING hnsw (embedding vector_cosine_ops)")
        cur.execute(f"CREATE INDEX rim_chunks_text_gin ON {SCHEMA}.rim_chunks USING gin (to_tsvector('english', chunk_text))")
        cur.execute(f"ANALYZE {SCHEMA}.rim_docs, {SCHEMA}.rim_chunks")
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        pgvector = cur.fetchone()[0]
    conn.close()

    (DEMO_DIR / "data_validation_config.ini").write_text(
        "; Written by scripts/retrieval_demo.py for the local demo only.\n"
        f"[postgresql]\nhost = {p['host']}\nport = {p['port']}\nuser = {p['user']}\npassword = {p['password']}\n", encoding="utf-8")
    _isolate_env(args.dsn)
    import data_api  # noqa: F401  applies the schema to the demo metadata
    import db as core_db
    key = "demo-" + secrets.token_urlsafe(24)
    api_id = str(uuid.uuid4())
    with core_db.transaction() as db:
        # Tables and the request log go with it (ON DELETE CASCADE).
        db.execute("DELETE FROM data_apis WHERE slug = ?", (SLUG,))
        db.execute("INSERT INTO data_apis (id, slug, name, description, api_key, created_by, created_at, is_active) "
                   "VALUES (?,?,?,?,?,?,?,true)", (api_id, SLUG, "Retrieval demo", "Local demo of semantic retrieval",
                                                   key, "retrieval_demo.py", dt.datetime.now().astimezone()))
        table_ids = {}
        for alias, cols in DEMO_ALIASES:
            table_ids[alias] = str(uuid.uuid4())
            db.execute("INSERT INTO data_api_tables (id, api_id, source_system, env, database_name, schema_name, "
                       "table_name, alias, row_limit, columns_csv) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (table_ids[alias], api_id, "postgresql", "DEV", p["database"], SCHEMA, "rim_chunks",
                        alias, 1000, cols))
    model_name = _configure_search(api_id, table_ids, deployment, dims, args.send_dimensions, fake)
    state = {"dsn": args.dsn, "db": p, "api_id": api_id, "slug": SLUG, "api_key": key, "fake_embeddings": fake,
             "table_ids": table_ids, "model_name": model_name, "deployment": deployment, "dimensions": dims, "pgvector": pgvector, "chunks": len(chunks),
             "documents": len(docs), "created_at": dt.datetime.now().isoformat(timespec="seconds")}
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"\nDemo ready: {len(docs)} documents, {len(chunks)} chunks, pgvector {pgvector}.")
    print(f"API: /data-api/{SLUG}/rim_chunks   key: {key}")
    if fake:
        print("NOTE: FAKE embeddings - wiring test only, not semantic quality.")
    print("Next: python scripts/retrieval_demo.py serve")


DEMO_ALIASES = [
    ("rim_chunks", ""),                                    # contract + exact allowed (tiny demo table)
    ("rim_chunks_b", ""),                                  # a second alias over the SAME table
    ("rim_chunks_narrow", "chunk_id,chunk_text,embedding"),  # exposes few columns
]
DEMO_CONTRACT = {
    "lookups": {"doc": {"schema": SCHEMA, "table": "rim_docs", "key": "chunk_source_id", "on": "chunk_source_id"}},
    "fields": {"chunk_id": "chunk_id", "chunk_source_id": "chunk_source_id", "chunk_number": "chunk_number",
               "page_number": "page_number", "chunk_text": "chunk_text", "document_id": "document_id"},
    "objects": {
        "rimdocs_metadata": {"object_name": "doc.object_name", "jj_region_country": "doc.jj_region_country",
                             "a_status": "doc.a_status", "r_version_label": "doc.r_version_label",
                             "approved_date": "doc.approved_date"},
        "document_metadata": {"indication": "doc.indication", "application_number": "doc.application_number",
                              "title": "doc.title"},
    },
}


def _admin_client(fake):
    """The portal's admin API, in-process, as a local admin. Used only by this
    demo tool; the demo server itself exposes no unauthenticated admin API."""
    import auth
    import data_api
    import data_api_retrieval
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    if fake:
        data_api_retrieval._azure_embed = fake_embed
    app = FastAPI()
    app.include_router(data_api.data_api_router)
    app.dependency_overrides[auth.require_auth] = lambda: {
        "user": {"email": "retrieval_demo.py", "name": "retrieval_demo.py"}, "role": "admin",
        "permissions": {"manageDataAPIExports": True}}
    return TestClient(app)


def _configure_search(api_id, table_ids, deployment, dims, send_dimensions, fake):
    c = _admin_client(fake)

    def ok(r, what):
        if r.status_code != 200:
            sys.exit(f"Could not {what}: HTTP {r.status_code} {r.text[:400]}")
        return r.json()
    name = f"demo-{'fake' if fake else 'embed'}-{dims}"
    lib = ok(c.get("/data-api/search/library"), "read the search library")
    model = next((m for m in lib["models"] if m["name"] == name), None)
    if model is None:
        model = ok(c.post("/data-api/search/models", json={"name": name, "deployment": deployment, "dimensions": dims,
                                                          "send_dimensions": send_dimensions}), "create the model")
    contract = ok(c.post("/data-api/search/contracts", json={"name": "rimdocs_chunks_demo", "definition": DEMO_CONTRACT}),
                  "create the response template")
    both = {"vector_column": "embedding", "text_column": "chunk_text"}
    for alias, settings, register in [
        ("rim_chunks", dict(both, contract_id=contract["id"], allow_exact=True), True),
        ("rim_chunks_b", dict(both), False),                 # reuses the shared registration
        ("rim_chunks_narrow", dict(both), False),
    ]:
        body = {"settings": settings, "replace_registration": True}   # vectors were just rebuilt
        if register:
            body["registration"] = {"embedding": model["id"]}
        ok(c.put(f"/data-api/{api_id}/tables/{table_ids[alias]}/search", json=body), f"configure /{alias}")
    return name


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------

def build_app(state, mcp=False, port=8000):
    _isolate_env(state["dsn"])
    _load_dotenv()
    if mcp:
        os.environ["DATA_API_MCP_ENABLED"] = "1"
        # Origin/Host validation ON, as the MCP spec requires.
        os.environ["DATA_API_MCP_ALLOWED_HOSTS"] = f"127.0.0.1:{port},localhost:{port}"
    import data_api
    import data_api_retrieval
    if state["fake_embeddings"]:
        data_api_retrieval._azure_embed = fake_embed
    from fastapi import FastAPI
    app = FastAPI(title="SmartHub Data API - local retrieval demo")
    app.include_router(data_api.data_api_router)
    if mcp:
        from data_api_mcp import install_data_api_mcp
        if not install_data_api_mcp(app):
            sys.exit("MCP requested but not mounted: pip install -r requirements-mcp.txt")
    return app


def cmd_serve(args):
    import uvicorn
    state = _state()
    app = build_app(state, args.mcp, args.port)
    print(f"Serving the Data API only (not the full portal) on http://127.0.0.1:{args.port}")
    print(f"Embeddings: {'FAKE (wiring test only)' if state['fake_embeddings'] else state['deployment']}"
          f"   MCP: {'on' if args.mcp else 'off'}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


# --------------------------------------------------------------------------
# evidence
# --------------------------------------------------------------------------

class Evidence:
    def __init__(self, state, base_url, out):
        import requests
        self.http = requests.Session()
        self.state, self.base, self.out = state, base_url.rstrip("/"), out
        self.results, self.n, self.ok = [], 0, 0

    def call(self, name, method, path, body=None, headers=None, raw=None):
        h = {"X-API-Key": self.state["api_key"]}
        h.update(headers or {})
        t = time.perf_counter()
        if raw is not None:
            r = self.http.request(method, self.base + path, data=raw, headers=h, timeout=60)
        else:
            r = self.http.request(method, self.base + path, json=body, headers=h, timeout=60)
        ms = (time.perf_counter() - t) * 1000
        try:
            data = r.json()
        except ValueError:
            data = r.text
        self.n += 1
        self.ok += r.status_code == 200
        safe_headers = {k: ("***" if k.lower() == "x-api-key" else v) for k, v in h.items()}
        record = {"request": {"method": method, "path": path, "headers": safe_headers, "body": body if raw is None else raw},
                  "status": r.status_code, "client_ms": round(ms, 1), "response": data}
        (self.out / "responses" / f"{self.n:02d}_{name}.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        return r.status_code, data, ms

    def check(self, name, description, ok, detail=""):
        self.results.append({"check": name, "description": description, "passed": bool(ok), "detail": detail})
        print(f"  [{'PASS' if ok else 'FAIL'}] {description}" + (f" - {detail}" if detail else ""))


def _pct(values, q):
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


def cmd_evidence(args):
    import psycopg2
    state = _state()
    base = f"http://127.0.0.1:{args.port}"
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = DEMO_DIR / "evidence" / stamp
    (out / "responses").mkdir(parents=True)
    (out / "plans").mkdir()
    ev = Evidence(state, base, out)
    api = f"/data-api/{state['slug']}"
    marker = "evidence marker " + secrets.token_hex(6)
    _isolate_env(state["dsn"])
    import db as core_db
    with core_db.transaction(read_only=True) as db:
        # Id watermark (the log's ids only go up), so nothing depends on the
        # machine's clock or zone.
        log_watermark = db.execute("SELECT COALESCE(MAX(id), 0) FROM data_api_requests").fetchone()[0]
    try:
        ev.http.get(base + "/docs", timeout=5)
    except Exception:
        sys.exit(f"No server on {base}. Start it first: python scripts/retrieval_demo.py serve")

    print("Functional checks")
    st, body, _ = ev.call("columns", "GET", f"{api}/rim_chunks/columns")
    ev.check("discovery", "/columns advertises text queries and retrieval",
             st == 200 and body.get("text_query_columns") == ["embedding"] and body.get("retrieval", {}).get("modes") == ["hybrid", "dense", "sparse"],
             f"HTTP {st}")

    conn = psycopg2.connect(state["dsn"])
    with conn.cursor() as cur:
        cur.execute(f"SELECT chunk_id::text, embedding::text FROM {SCHEMA}.rim_chunks "
                    "WHERE chunk_text LIKE 'Application BLA 761201%%' LIMIT 1")
        target_id, target_vec = cur.fetchone()
    conn.close()
    st, body, _ = ev.call("vector_search_raw_vector", "POST", f"{api}/rim_chunks/vector-search",
                          {"vector_column": "embedding", "query_vector": json.loads(target_vec), "top_k": 3,
                           "search_mode": "exact"})
    top = (body.get("matches") or [{}])[0] if isinstance(body, dict) else {}
    ev.check("backward_compat", "/vector-search with a raw vector still works (existing behaviour)",
             st == 200 and top.get("row", {}).get("chunk_id") == target_id and body.get("query_source") == "vector",
             f"top distance {top.get('distance')}")

    st, body, _ = ev.call("vector_search_text", "POST", f"{api}/rim_chunks/vector-search",
                          {"vector_column": "embedding", "query_text": "sterility failure in aseptic filling", "top_k": 5})
    ev.check("text_query", "/vector-search accepts query_text and embeds it server-side",
             st == 200 and body.get("query_source") == "text" and body.get("embedding_model") == state["model_name"]
             and len(body.get("matches", [])) == 5, f"HTTP {st}")

    st, body, _ = ev.call("retrieve_hybrid_contract", "POST", f"{api}/rim_chunks/retrieve",
                          {"retrieval_queries": [{"query": "sterility failure in aseptic filling", "k": 5}]})
    m = body["results"][0]["matches"] if st == 200 else []
    contract_keys = {"rank", "score", "dense_score", "sparse_score", "chunk_id", "chunk_source_id", "chunk_number",
                     "page_number", "chunk_text", "document_id", "rimdocs_metadata", "document_metadata"}
    ok = (st == 200 and m and all(set(x) == contract_keys for x in m)
          and [x["rank"] for x in m] == list(range(1, len(m) + 1))
          and all(m[i]["score"] >= m[i + 1]["score"] for i in range(len(m) - 1))
          and all(x["rimdocs_metadata"]["object_name"] for x in m))
    ev.check("hybrid_contract", "/retrieve returns the nested contract, ranked by fused score, with metadata joined",
             ok, f"top: {m[0]['chunk_text'][:60] if m else '-'}")
    both = sum(1 for x in m if x["dense_score"] is not None and x["sparse_score"] is not None)
    ev.check("hybrid_fusion", "hybrid results carry both semantic and keyword scores where both legs matched",
             both >= 1, f"{both} of {len(m)} matched by both legs")

    st, body, _ = ev.call("retrieve_identifier", "POST", f"{api}/rim_chunks/retrieve",
                          {"retrieval_queries": [{"query": "BLA 761207", "k": 5}]})
    m = body["results"][0]["matches"] if st == 200 else []
    hit = next((x["rank"] for x in m if "BLA 761207" in x["chunk_text"]), None)
    ev.check("identifier_recall", "exact identifiers are found (keyword leg), which embeddings alone handle poorly",
             hit is not None and hit <= 2, f"identifier chunk at rank {hit}")

    st, body, _ = ev.call("retrieve_batch", "POST", f"{api}/rim_chunks/retrieve", {"retrieval_queries": [
        {"query": "complete response letter facility deficiencies", "k": 4, "where": ["region:eq:EU"]},
        {"query": "nitrosamine impurity limits", "k": 3, "mode": "sparse"},
        {"query": "shelf life stability", "k": 2, "mode": "dense"},
        {"query": marker, "k": 2}]})
    r = body.get("results", []) if st == 200 else []
    ok = (len(r) == 4 and [x["k"] for x in r] == [4, 3, 2, 2]
          and all(x["rimdocs_metadata"]["jj_region_country"] == "EU" for x in r[0]["matches"])
          and all(x["dense_score"] is None for x in r[1]["matches"])
          and all(x["sparse_score"] is None for x in r[2]["matches"]))
    ev.check("batch_modes_filters", "batch of 4 queries: per-query k, filter, sparse-only and dense-only modes", ok, f"HTTP {st}")

    st, body, _ = ev.call("shared_registration", "POST", f"{api}/rim_chunks_b/retrieve",
                          {"retrieval_queries": [{"query": "sterility failure", "k": 3}]})
    ok = st == 200 and body.get("embedding_model") == state["model_name"] and "row" in body["results"][0]["matches"][0]
    ev.check("shared_registration", "a second alias over the same table reuses the shared model, with its own "
             "settings (default response shape)", ok, f"HTTP {st}")
    st, body, _ = ev.call("exact_refused", "POST", f"{api}/rim_chunks_b/retrieve",
                          {"search_mode": "exact", "retrieval_queries": [{"query": "x", "mode": "dense"}]})
    ev.check("exact_policy", "exact search is refused where the alias does not allow it (409)", st == 409, str(body)[:90])
    st, body, _ = ev.call("error_hidden_filter", "POST", f"{api}/rim_chunks_narrow/retrieve",
                          {"retrieval_queries": [{"query": "x", "where": ["region:eq:EU"]}]})
    ev.check("err_hidden_filter", "filters cannot reach columns the alias does not expose (400)", st == 400, str(body)[:90])
    _isolate_env(state["dsn"])
    admin = _admin_client(state["fake_embeddings"])
    tid = state["table_ids"]["rim_chunks_narrow"]
    lib = admin.get("/data-api/search/library").json()
    contract_id = next(c["id"] for c in lib["contracts"] if c["name"] == "rimdocs_chunks_demo")
    r = admin.put(f"/data-api/{state['slug']}/tables/{tid}/search",
                  json={"settings": {"vector_column": "embedding", "text_column": "chunk_text", "contract_id": contract_id}})
    (out / "responses" / "save_rejected.json").write_text(json.dumps({"status": r.status_code, "body": r.json()}, indent=2), encoding="utf-8")
    ev.check("save_validation", "portal save is rejected when the template needs columns the alias hides (422)",
             r.status_code == 422 and "does not expose" in r.text, f"HTTP {r.status_code}")
    st, body, _ = ev.call("error_k_too_large", "POST", f"{api}/rim_chunks/retrieve",
                          {"retrieval_queries": [{"query": "x", "k": 101}]})
    ev.check("err_400", "out-of-range k is rejected (400)", st == 400, str(body)[:90])
    st, body, _ = ev.call("error_content_type", "POST", f"{api}/rim_chunks/retrieve", raw="x",
                          headers={"Content-Type": "text/plain"})
    ev.check("err_415", "non-JSON body is rejected (415)", st == 415, f"HTTP {st}")
    st, body, _ = ev.call("error_bad_key", "POST", f"{api}/rim_chunks/retrieve",
                          {"retrieval_queries": [{"query": "x"}]}, headers={"X-API-Key": "wrong"})
    ev.check("err_401", "wrong API key is rejected (401)", st == 401, f"HTTP {st}")

    print("Recall of the approximate index vs exact search")
    probes = ["sterility media fill", "facility inspection deficiencies", "accelerated stability humidity",
              "helium leak testing", "renal impairment dose", "nitrosamine risk assessment",
              "dose escalation safety review", "interstitial lung disease signal", "boxed warning hepatotoxicity",
              "Type A meeting resubmission"]
    strict, tie_aware = [], []
    for q in probes:
        got = {}
        for mode in ("auto", "exact"):
            st, body, _ = ev.call(f"recall_{mode}", "POST", f"{api}/rim_chunks/retrieve",
                                  {"search_mode": mode, "retrieval_queries": [{"query": q, "k": 10, "mode": "dense"}]})
            got[mode] = body["results"][0]["matches"] if st == 200 else []
        exact_ids = {x["chunk_id"] for x in got["exact"]}
        kth = min((x["dense_score"] for x in got["exact"]), default=1.0)
        strict.append(len(exact_ids & {x["chunk_id"] for x in got["auto"]}) / max(1, len(exact_ids)))
        # Tie-aware: an approximate result is correct if it is at least as
        # similar as the 10th exact result. Items tied at that cut-off are
        # equally valid answers, so plain set overlap under-counts recall.
        tie_aware.append(sum(1 for x in got["auto"] if x["dense_score"] >= kth - 1e-9) / max(1, len(exact_ids)))
    recall = sum(tie_aware) / len(tie_aware)
    strict_recall = sum(strict) / len(strict)
    ev.check("ann_recall", "approximate (HNSW) results are as close as exact search (tie-aware recall@10)",
             recall >= 0.95, f"tie-aware {recall:.2f}; strict set overlap {strict_recall:.2f}")

    print(f"Latency ({args.runs} warm runs per scenario after 2 warm-ups; embeddings cached after first call)")
    scenarios = {
        "vector-search, raw vector": ("vector-search", {"vector_column": "embedding", "query_vector": json.loads(target_vec), "top_k": 10}),
        "vector-search, text": ("vector-search", {"vector_column": "embedding", "query_text": "container closure integrity", "top_k": 10}),
        "retrieve, 1 hybrid query, k=10": ("retrieve", {"retrieval_queries": [{"query": "labeling renal impairment", "k": 10}]}),
        "retrieve, batch of 5 hybrid queries, k=10": ("retrieve", {"retrieval_queries": [
            {"query": q, "k": 10} for q in probes[:5]]}),
    }
    latency = []
    for label, (endpoint, payload) in scenarios.items():
        client, server = [], []
        for i in range(args.runs + 2):
            st, body, ms = ev.call(f"latency_{endpoint}", "POST", f"{api}/rim_chunks/{endpoint}", payload)
            if i >= 2 and st == 200:
                client.append(ms)
                server.append(body.get("elapsed_ms", 0))
        row = {"scenario": label, "runs": len(client), "p50_ms": round(_pct(client, 50), 1),
               "p95_ms": round(_pct(client, 95), 1), "max_ms": round(max(client), 1),
               "server_p50_ms": round(_pct(server, 50), 1)}
        latency.append(row)
        print(f"  {label}: p50 {row['p50_ms']} ms, p95 {row['p95_ms']} ms")
    cold = []
    for i in range(min(5, args.runs)):
        st, body, ms = ev.call("latency_cold", "POST", f"{api}/rim_chunks/retrieve",
                               {"retrieval_queries": [{"query": f"sterility investigation corrective action {secrets.token_hex(3)}", "k": 10}]})
        if st == 200:
            cold.append(ms)
    if cold:
        label = ("retrieve, 1 hybrid query, NEW text (fake embedder: no network call)" if state["fake_embeddings"]
                 else "retrieve, 1 hybrid query, NEW text (includes the embedding API call)")
        latency.append({"scenario": label, "runs": len(cold),
                        "p50_ms": round(_pct(cold, 50), 1), "p95_ms": round(_pct(cold, 95), 1),
                        "max_ms": round(max(cold), 1), "server_p50_ms": None})
        print(f"  cold (new text, embedding call): p50 {latency[-1]['p50_ms']} ms")

    print("Index usage (EXPLAIN ANALYZE of the exact SQL the API runs)")
    from data_validation_connectors import PostgreSQLConnector
    p = state["db"]
    src = PostgreSQLConnector({"host": p["host"], "port": p["port"], "user": p["user"], "password": p["password"]})
    vmeta = next(v for v in src.get_vector_columns(p["database"], SCHEMA, "rim_chunks") if v["name"] == "embedding")
    import data_api_retrieval
    from data_api_retrieval import EmbeddingModel
    if state["fake_embeddings"]:
        data_api_retrieval._azure_embed = fake_embed
    _load_dotenv()
    qvec = data_api_retrieval.embed_queries(EmbeddingModel("demo-embed", {"deployment": state["deployment"],
                                                                           "dimensions": state["dimensions"]}),
                                            ["sterility failure in aseptic filling"])[0]
    dense_sql, dense_params = src._vector_statement(SCHEMA, "rim_chunks", ["chunk_id"], vmeta, qvec, 50, "cosine",
                                                    None, "auto", ["chunk_id"])
    sparse_sql, sparse_params = src._sparse_statement(SCHEMA, "rim_chunks", ["chunk_id"], "chunk_text", "english",
                                                      False, "sterility aseptic", 50, None)
    plans = {}
    with src.read_only_transaction(p["database"], 10000) as cur:
        # The demo table is tiny, so the planner would rightly prefer a
        # sequential scan. Disabling it proves the API's SQL shape CAN use
        # each index; production-size tables choose the index on cost.
        cur.execute("SELECT set_config('enable_seqscan', 'off', true)")
        for name, (sql, params) in {"dense": (dense_sql, dense_params), "keyword": (sparse_sql, sparse_params)}.items():
            cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, params)
            plans[name] = "\n".join(r[0] for r in cur.fetchall())
            (out / "plans" / f"{name}_plan.txt").write_text(sql + "\n\n" + plans[name], encoding="utf-8")
    src.disconnect()
    ev.check("index_dense", "semantic leg uses the HNSW index", "rim_chunks_embedding_hnsw" in plans["dense"])
    ev.check("index_keyword", "keyword leg uses the GIN full-text index", "rim_chunks_text_gin" in plans["keyword"])

    print("Activity log")
    with core_db.transaction(read_only=True) as db:
        rows = [(r[0], r[1], r[2]) for r in db.execute(
            "SELECT endpoint, status, COUNT(*) FROM data_api_requests WHERE api_id = ? AND id > ? "
            "GROUP BY endpoint, status ORDER BY endpoint, status", (state["api_id"], log_watermark)).fetchall()]
        dump = json.dumps(db.execute("SELECT * FROM data_api_requests WHERE api_id = ?", (state["api_id"],)).fetchall())
    (out / "activity_log.json").write_text(json.dumps([{"endpoint": e, "status": s, "count": c} for e, s, c in rows], indent=2), encoding="utf-8")
    logged_ok = sum(c for _, s, c in rows if s == 200)
    ev.check("activity_logged", "every successful call is in the activity log", logged_ok >= ev.ok,
             f"{logged_ok} logged for {ev.ok} successful calls ({sum(c for _, _, c in rows)} rows incl. errors)")
    ev.check("no_query_text_logged", "query text is never written to the activity log", marker not in dump)

    if args.mcp:
        print("MCP")
        _mcp_checks(ev, state, args.port)

    tests = None
    if args.with_tests:
        print("Automated test suites")
        env = dict(os.environ, DATA_API_TEST_PG_DSN=_keyword_dsn(state["db"]))
        env.pop("SMARTHUB_DB_SCHEMA", None)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run([sys.executable, "-m", "pytest", "tests", "assistant/tests", "-q", "-p", "no:cacheprovider",
                               f"--junitxml={out / 'tests_junit.xml'}"], cwd=BACKEND, env=env,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        (out / "tests_output.txt").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        tests = (proc.stdout.strip().splitlines() or ["(no output)"])[-1]
        ev.check("tests", "automated suites (unit + PostgreSQL integration + existing assistant tests)",
                 proc.returncode == 0, tests)

    _write_summary(ev, out, latency, recall, plans, rows, tests)
    failed = [r for r in ev.results if not r["passed"]]
    print(f"\n{len(ev.results) - len(failed)}/{len(ev.results)} checks passed. Evidence: {out}")
    sys.exit(1 if failed else 0)


def _mcp_checks(ev, state, port):
    import asyncio
    try:
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:
        ev.check("mcp", "MCP client available", False, "pip install -r requirements-mcp.txt")
        return

    async def run(headers):
        async with httpx2.AsyncClient(headers=headers) as http:
            async with streamable_http_client(f"http://127.0.0.1:{port}/data-api/mcp/{state['slug']}",
                                              http_client=http) as (r, w, *_):
                async with ClientSession(r, w) as s:
                    init = await s.initialize()
                    tools = await s.list_tools()
                    found = await s.call_tool("search", {"alias": "rim_chunks", "query": "sterility failure", "k": 3})
                    return init, tools, found

    try:
        init, tools, found = asyncio.run(run({"Authorization": f"Bearer {state['api_key']}"}))
        names = sorted(t.name for t in tools.tools)
        result = json.loads(found.content[0].text)
        (ev.out / "responses" / "mcp_search.json").write_text(json.dumps(
            {"protocol_version": getattr(init, "protocol_version", None), "tools": names, "search": result}, indent=2, default=str), encoding="utf-8")
        ev.check("mcp_tools", "MCP exposes list_tables, describe_table and search (read-only)",
                 names == ["describe_table", "list_tables", "search"] and all(t.annotations.read_only_hint for t in tools.tools),
                 f"protocol {getattr(init, 'protocol_version', '?')}")
        ev.check("mcp_search", "MCP search returns contract-shaped matches", bool(result.get("matches")))
    except Exception as e:
        ev.check("mcp", "MCP session", False, f"{type(e).__name__}: {e}"[:120])
    try:
        import requests
        r = requests.post(f"http://127.0.0.1:{port}/data-api/mcp/{state['slug']}",
                          headers={"Origin": "https://evil.example.com", "Content-Type": "application/json",
                                   "Accept": "application/json, text/event-stream",
                                   "Authorization": f"Bearer {state['api_key']}"},
                          data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}), timeout=10)
        ev.check("mcp_origin", "MCP rejects a foreign Origin header (DNS-rebinding protection)", r.status_code == 403,
                 f"HTTP {r.status_code}")
    except Exception as e:
        ev.check("mcp_origin", "MCP origin check", False, str(e)[:120])


def _write_summary(ev, out, latency, recall, plans, activity, tests):
    s = ev.state
    import psycopg2
    conn = psycopg2.connect(s["dsn"])
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        pg = cur.fetchone()[0]
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        pgv = cur.fetchone()[0]
    conn.close()
    env_rows = [
        ("Run at", dt.datetime.now().isoformat(timespec="seconds")),
        ("Machine", f"{platform.system()} {platform.release()}, {os.cpu_count()} CPUs, Python {platform.python_version()}"),
        ("PostgreSQL / pgvector", f"{pg} / {pgv}"),
        ("Embeddings", "FAKE word-hashing vectors - wiring test only, not semantic quality"
         if s["fake_embeddings"] else f"Azure OpenAI deployment {s['deployment']} ({s['dimensions']} dims)"),
        ("Corpus", f"{s['documents']} synthetic documents, {s['chunks']} chunks"),
    ]
    passed = sum(r["passed"] for r in ev.results)
    lines = ["# Retrieval evidence pack", "", f"**Result: {passed}/{len(ev.results)} checks passed.**", "",
             "| Item | Value |", "| --- | --- |"] + [f"| {k} | {v} |" for k, v in env_rows]
    lines += ["", "## Checks", "", "| Check | Result | Detail |", "| --- | --- | --- |"]
    lines += [f"| {r['description']} | {'PASS' if r['passed'] else 'FAIL'} | {r['detail']} |" for r in ev.results]
    lines += ["", "## Latency (client-measured, milliseconds)", "",
              "Local machine, small demo corpus: indicative only, not a production benchmark.", "",
              "| Scenario | Runs | p50 | p95 | Max | Server p50 |", "| --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {l['scenario']} | {l['runs']} | {l['p50_ms']} | {l['p95_ms']} | {l['max_ms']} | "
              f"{'' if l['server_p50_ms'] is None else l['server_p50_ms']} |" for l in latency]
    lines += ["", f"Approximate-index recall@10 vs exact search (10 probe queries): **{recall:.2f}** tie-aware. "
              "Strict set overlap is in the checks table; it is lower only when several chunks tie at the 10th "
              "distance, where any of them is a correct answer.", "",
              "## Files", "", "- `responses/`: every request (API key masked) and response",
              "- `plans/`: EXPLAIN ANALYZE of the exact semantic and keyword SQL",
              "- `activity_log.json`: activity-log rows written during this run"]
    if tests:
        lines += ["- `tests_output.txt`, `tests_junit.xml`: automated suites"]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "summary.json").write_text(json.dumps({"environment": dict(env_rows), "checks": ev.results,
                                                  "latency": latency, "ann_recall_at_10": recall,
                                                  "activity": activity, "tests": tests}, indent=2, default=str), encoding="utf-8")


# --------------------------------------------------------------------------

def main():
    # Windows consoles often use a legacy code page; never crash on a character it lacks.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("local-db", help="start an embedded PostgreSQL + pgvector (pip install pgserver), no Docker")
    d.add_argument("--stop", action="store_true", help="stop the embedded database")
    s = sub.add_parser("setup", help="create demo tables, embeddings, indexes, config and a demo API")
    s.add_argument("--dsn", required=True, help="postgresql://user:password@host:port/database")
    s.add_argument("--deployment", help="embedding deployment (default: AZURE_OPENAI_EMBED_DEPLOYMENT)")
    s.add_argument("--dimensions", type=int, default=1536, help="dimensions the deployment returns (default 1536)")
    s.add_argument("--send-dimensions", action="store_true", help="request --dimensions from text-embedding-3 models")
    s.add_argument("--fake-embeddings", action="store_true", help="no Azure calls; wiring test only")
    v = sub.add_parser("serve", help="run the Data API only, against the demo")
    v.add_argument("--port", type=int, default=8000)
    v.add_argument("--mcp", action="store_true", help="also mount the MCP endpoint (needs requirements-mcp.txt)")
    e = sub.add_parser("evidence", help="run every check against the running demo server and save an evidence pack")
    e.add_argument("--port", type=int, default=8000)
    e.add_argument("--runs", type=int, default=20, help="warm runs per latency scenario (default 20)")
    e.add_argument("--with-tests", action="store_true", help="also run the automated test suites")
    e.add_argument("--mcp", action="store_true", help="also check the MCP endpoint (server started with --mcp)")
    args = ap.parse_args()
    {"local-db": cmd_local_db, "setup": cmd_setup, "serve": cmd_serve, "evidence": cmd_evidence}[args.cmd](args)


if __name__ == "__main__":
    main()