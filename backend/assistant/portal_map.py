"""
Every place in the portal the assistant can send someone, down to the tab.

Before this the model only knew page routes, so "take me to business defined
checks" landed on the bare /dqe picker and "show me the roles" on User
Management's default Groups tab. Each entry here is one exact destination:
the URL params the page actually reads, the permission(s) needed to see it,
and the names people use for it.

resolve() is plain string matching, no model involved, so the same request
always lands in the same place. The router uses it three ways: a hint for the
model, expanding navigate_to(destination=...) into a real URL, and bumping a
navigate_to that stopped at a parent page down to the tab the user asked for.

Params and tab ids have to match the frontend. tests/test_portal_map.py reads
the page sources and fails if they drift.
"""

import re
from difflib import get_close_matches
from dataclasses import dataclass, field
from urllib.parse import urlsplit, parse_qsl


@dataclass
class Dest:
    id: str
    name: str                      # label as the UI shows it
    route: str
    params: dict = field(default_factory=dict)
    parent: str | None = None
    perms: tuple = ()              # on top of the parent's
    aliases: tuple = ()
    about: str = ""
    then_click: str | None = None  # button to press after landing (not URL addressable)
    ambiguous_alone: bool = False  # generic words like "overview" - only win with context
    same_url_as: str | None = None # deliberately the same link as another destination


_D = []


def d(*args, **kw):
    _D.append(Dest(*args, **kw))


# ---- Dashboard ----
d("dashboard", "Dashboard", "/dashboard",
  aliases=("home", "home page", "landing page", "main page", "start page", "applications",
           "all apps", "all tools", "portal home", "toolkit", "your toolkit",
           "pick up where you left off"),
  about="Landing page: search for any tool or section, pick up where you left off, and a card "
        "per application you can open, filterable by Data, AI, Explorers and Admin.")

# ---- Data quality (DQE) ----
d("dqe", "Data quality", "/dqe", perms=("viewDQE",),
  aliases=("data quality", "data quality engine", "dq engine", "dqe", "dq", "quality engine",
           "check type picker", "select validation parameters", "validation parameters"),
  about="Data Quality Engine. Opens on 'Select Validation Parameters' where you pick IT Checks, "
        "Data Validation or Business Defined Checks.")

d("dqe.it", "IT Checks", "/dqe", {"check": "IT_Checks"}, parent="dqe", perms=("viewITChecks",),
  aliases=("it checks", "it check", "it validation", "row counts", "source row counts", "row count checks",
           "source to target checks", "migration checks", "schema validation", "constraint checks",
           "null counts", "validation report"),
  about="Source-to-target migration stats: row counts, schema, constraints, null/distinct/duplicate counts.")

d("dqe.dv", "Data Validation", "/dqe", {"check": "Data_Validation"}, parent="dqe",
  perms=("viewDataValidation",),
  aliases=("data validation", "guided validation", "source to target validation",
           "data validation beta", "compare source and target"),
  about="Guided source-to-target comparison (Beta).")

d("bdc", "Business Defined Checks", "/dqe", {"check": "Business_defined_checks"}, parent="dqe",
  perms=("viewBusinessChecks",),
  aliases=("business defined checks", "business defined check", "business checks", "bdc",
           "business rules", "dq rules", "data quality rules", "righttime", "right time"),
  about="Business-written rules turned into SQL by AI. Users with the executive permission land on "
        "the Executive View; everyone else lands on the Overview tab.")

# Tabs need view=standard: executive users get the tab bar hidden without it.
STD = {"check": "Business_defined_checks", "view": "standard"}

# Same link as bdc - which one shows depends on the permission, not the URL.
d("bdc.exec", "Executive View", "/dqe", {"check": "Business_defined_checks"}, parent="bdc",
  perms=("viewBDCExecutive",), same_url_as="bdc",
  aliases=("executive view", "exec view", "executive dashboard", "executive summary",
           "leadership view", "check dimensions"),
  about="One-screen summary for leadership: pass rate over time, check dimensions, trend, "
        "Quarterly vs Semi-Monthly switch. 'Standard View' button goes back to the tabs.")

d("bdc.standard", "Standard View", "/dqe", STD, parent="bdc",
  aliases=("standard view", "full workspace", "normal view", "classic view", "exit executive view"),
  about="The full Business Defined Checks workspace with its tabs (opens on Overview).")

d("bdc.overview", "Overview", "/dqe", {**STD, "tab": "overview"}, parent="bdc",
  aliases=("overview", "overview tab", "active rules", "active rules table", "kpi cards", "kpis"),
  ambiguous_alone=True,
  about="KPI cards (tables monitored, rules run, average pass rate) and the Active Rules table.")

d("bdc.datasets", "Dataset Info", "/dqe", {**STD, "tab": "dataset-health"}, parent="bdc",
  aliases=("dataset info", "dataset health", "dataset health overview", "datasets",
           "dataset", "health by dataset", "weakest dataset"),
  about="Rules grouped by business dataset with an average pass rate and status; click a row for its rules.")

d("bdc.trends", "Trends", "/dqe", {**STD, "tab": "trends"}, parent="bdc",
  aliases=("trends", "trend", "trends tab", "historical trend", "historical trend analysis",
           "pass rate history", "rule history", "pass rate over time"),
  about="Pick a rule and see its pass rate across every past execution.")

d("bdc.failed", "Failed Rows", "/dqe", {**STD, "tab": "failed-rows"}, parent="bdc",
  aliases=("failed rows", "failed row", "failures", "failing rows", "failed records",
           "delta report", "advanced delta report", "consolidated report",
           "consolidated failed rows", "sql predicate", "show failed rows"),
  about="Pick a rule, read its SQL predicate, load failing rows (colour-banded), Delta and Advanced "
        "Delta reports, and the Consolidated Report card.")

d("bdc.superuser", "Superuser", "/dqe", {**STD, "tab": "admin"}, parent="bdc",
  perms=("viewBDCSuperuser",),
  aliases=("superuser", "superuser tab", "superuser configuration", "admin tab", "bdc admin",
           "dq tables", "dq tables full visibility"),
  about="Admin-only 'Superuser Configuration': the DQ Tables section with Rule Governance, "
        "AI Registry and Date Input.")

d("bdc.superuser.governance", "DQ Rule Governance", "/dqe", {**STD, "tab": "admin", "dqtab": "rule-input"},
  parent="bdc.superuser",
  aliases=("dq rule governance", "rule governance", "rule input", "dq rule input", "create rule",
           "add rule", "new rule", "edit rule", "deactivate rule", "toggle rule"),
  about="Source-of-truth rule table: create, edit (versioned) and activate/deactivate rules.")

d("bdc.superuser.registry", "DQ Rules AI Registry", "/dqe", {**STD, "tab": "admin", "dqtab": "rule-registry"},
  parent="bdc.superuser",
  aliases=("dq rules ai registry", "ai registry", "rules ai registry", "ai rules registry",
           "rule registry", "sql predicates table"),
  about="Validated AI-generated SQL predicates for each rule.")

d("bdc.superuser.dates", "DQ Date Input", "/dqe", {**STD, "tab": "admin", "dqtab": "date-input"},
  parent="bdc.superuser",
  aliases=("dq date input", "date input", "date ranges", "reporting dates", "quarter dates"),
  about="Date ranges behind the Quarterly and Semi-Monthly datasets.")

d("bdc.review", "Review", "/dqe", {**STD, "tab": "review"}, parent="bdc",
  perms=("viewBDCSuperuser",),
  aliases=("review", "review tab", "rule review", "sign off", "signoff", "rule sign off",
           "approve rules", "reject rules", "accept rules", "pending review", "awaiting review",
           "review queue"),
  about="Admin-only sign-off queue: Accept or Reject each rule with a note; badge = rules still pending.")

# ---- Ingestion Lab ----
d("ingest", "Ingestion Lab", "/ingestion-lab", perms=("viewIngestionLab",),
  aliases=("ingestion lab", "ingestion", "ingest", "ingestion pipeline", "ingestion pipelines",
           "run ingestion", "run extraction", "extraction script", "ingestion runs", "ingestion history"),
  about="Run the configured ingestion steps on the server: inputs, live output, stop, run history.")

# ---- Fuzzy match ----
d("fuzzy", "Fuzzy match", "/fuzzy-match", perms=("viewFuzzyMatch",),
  aliases=("fuzzy match", "fuzzy matching", "fuzzy search", "catalog search", "denodo",
           "denodo catalog", "data catalog", "find a column", "search all databases",
           "search everywhere"),
  about="Typo-tolerant search across every database and view in the Denodo data catalog.")

# ---- Unstructured Explorer ----
d("unstructured", "Unstructured Explorer", "/unstructured-explorer", perms=("viewUnstructured",),
  aliases=("unstructured explorer", "unstructured", "unstructured data", "document explorer",
           "documents explorer", "document sets", "explorer picker"),
  about="Opens on a picker of nine document sets: Virtual Regulator, IMMBOT, MirAI (coming soon), "
        "RMWCreate (coming soon), Q!, Nipocalimab, RIMDocs, EPOD, FDA and Others. Each set and each of its "
        "tabs is its own permission, so a user only sees the ones their role allows.")

d("un.vr", "Virtual Regulator", "/unstructured-explorer", {"doctype": "vr"}, parent="unstructured", perms=("viewUnstructuredVR",),
  aliases=("virtual regulator", "virtual regulator explorer"),
  about="Virtual Regulator with an Internal / External switch. Internal: SUBX_FINAL documents and chunks "
        "from Snowflake with Overview, Documents and Data table tabs. External: FDA press releases and other "
        "health-authority tables.")
for tab_id, label, more in (
    ("explore", "Overview", ("virtual regulator explore", "virtual regulator overview")),
    ("library", "Documents", ("virtual regulator documents", "virtual regulator library", "virtual regulator document library")),
    ("table", "Data table", ("virtual regulator data table", "virtual regulator table", "virtual regulator chunks")),
):
    d(f"un.vr.{tab_id}", label, "/unstructured-explorer", {"doctype": "vr", "vrscope": "internal", "vrtab": tab_id},
      parent="un.vr", perms=("viewUnstructuredVRInternal",), aliases=more, ambiguous_alone=tab_id != "library")
d("un.vr.external", "External", "/unstructured-explorer", {"doctype": "vr", "vrscope": "external"},
  parent="un.vr", perms=("viewUnstructuredVRExternal",),
  aliases=("virtual regulator external", "external virtual regulator", "fda press releases", "health authority tables"),
  about="Pick a health authority, then a table. FDA press releases have Explore and Data table; other tables open as a data table.")

d("un.mirai", "MirAI", "/unstructured-explorer", {"doctype": "mirai"}, parent="unstructured", perms=("viewUnstructuredMirAI",),
  aliases=("mirai", "mir ai", "mirai explorer"), about="Coming soon - placeholder page for now.")
d("un.rmwcreate", "RMWCreate", "/unstructured-explorer", {"doctype": "rmwcreate"}, parent="unstructured",
  perms=("viewUnstructuredRMWCreate",), aliases=("rmwcreate", "rmw create", "rmwcreate explorer"),
  about="Coming soon - placeholder page for now.")

d("un.immbot", "IMMBOT", "/unstructured-explorer", {"doctype": "immbot"}, parent="unstructured", perms=("viewUnstructuredIMMBOT",),
  aliases=("immbot", "immbot explorer", "immbot final"),
  about="IMMBOT documents and chunks from Snowflake (IMMBOT_FINAL): Explore, Data table, Breakdown.")
for tab_id, label, more in (
    ("explore", "Explore", ("immbot explore",)),
    ("table", "Data table", ("immbot data table", "immbot table", "immbot chunks")),
    ("breakdown", "Breakdown", ("immbot breakdown",)),
):
    d(f"un.immbot.{tab_id}", label, "/unstructured-explorer", {"doctype": "immbot", "immbottab": tab_id},
      parent="un.immbot", perms=(f"viewUnstructuredIMMBOT{tab_id.title()}",), aliases=more, ambiguous_alone=tab_id in ("explore", "table"))

d("un.rimdocs", "RIMDocs", "/unstructured-explorer", {"doctype": "rimdocs"}, parent="unstructured", perms=("viewUnstructuredRimDocs",),
  aliases=("rimdocs", "rim docs", "rimdocs explorer", "rim documents"),
  about="RIMDocs metadata: Explore, Data table and Domain breakdown.")
for tab_id, label, extra, more in (
    ("overview", "Explore", ("viewUnstructuredRimDocsExplore",), ("explore", "explore tab", "charts")),
    ("table", "Data table", ("viewUnstructuredRimDocsTable",), ("data table", "table view", "records table")),
    ("breakdown", "Domain breakdown", ("viewUnstructuredRimDocsBreakdown",), ("domain breakdown", "breakdown", "by domain")),
):
    d(f"un.rimdocs.{tab_id}", label, "/unstructured-explorer", {"doctype": "rimdocs", "tab": tab_id},
      parent="un.rimdocs", perms=extra, aliases=more,
      ambiguous_alone=tab_id in ("overview", "table"))

d("un.fda", "FDA and Others", "/unstructured-explorer", {"doctype": "fda"}, parent="unstructured", perms=("viewUnstructuredFDA",),
  aliases=("fda and others", "fda & others", "fda", "fda explorer", "crl", "complete response letters"),
  about="FDA and other regulatory data from Postgres, with a dashboard picker.")
d("un.fda.explore", "Explore", "/unstructured-explorer", {"doctype": "fda", "fdatab": "explore"},
  parent="un.fda", perms=("viewUnstructuredFDAExplore",), aliases=("explore", "fda explore"), ambiguous_alone=True)
d("un.fda.table", "Data table", "/unstructured-explorer", {"doctype": "fda", "fdatab": "table"},
  parent="un.fda", perms=("viewUnstructuredFDATable",), aliases=("data table", "fda data table", "fda table"), ambiguous_alone=True)

FDA_DASHBOARDS = [
    ("crl_matches", "CRL AI Matches", ("crl ai matches", "ai matches", "crl matches")),
    ("crl_letters", "CRL Letters Overview", ("crl letters overview", "crl letters")),
    ("crl_pink_letters", "Pink Sheet Letters", ("pink sheet letters", "pink sheet", "pink letters")),
    ("ectd_docs", "eCTD Documents", ("ectd documents", "ectd docs", "ectd")),
    ("fda_letters_v2", "FDA CRL Letters", ("fda crl letters",)),
    ("milvexian_metadata", "Milvexian Metadata", ("milvexian metadata", "milvexian")),
    ("rim_facility_index", "RIM Facility Index", ("rim facility index", "facility index", "facilities")),
]
for dash_id, label, al in FDA_DASHBOARDS:
    d(f"un.fda.dash.{dash_id}", label, "/unstructured-explorer", {"doctype": "fda", "fdadashboard": dash_id},
      parent="un.fda", aliases=al, about=f"FDA dashboard: {label}.")

d("un.epod", "EPOD", "/unstructured-explorer", {"doctype": "epod"}, parent="unstructured", perms=("viewUnstructuredEPOD",),
  aliases=("epod", "e pod", "epod explorer", "epod documents"),
  about="EPOD documents from Postgres, with a dataset dropdown (EDQM, NDA, ...).")
d("un.epod.explore", "Explore", "/unstructured-explorer", {"doctype": "epod", "epodtab": "explore"},
  parent="un.epod", perms=("viewUnstructuredEPODExplore",), aliases=("explore", "epod explore"), ambiguous_alone=True)
d("un.epod.table", "Data table", "/unstructured-explorer", {"doctype": "epod", "epodtab": "table"},
  parent="un.epod", perms=("viewUnstructuredEPODTable",), aliases=("data table", "epod data table", "epod table"), ambiguous_alone=True)

d("un.haq", "Q!", "/unstructured-explorer", {"doctype": "haq"}, parent="unstructured", perms=("viewUnstructuredHAQ",),
  aliases=("haq", "q explorer", "haq explorer", "health authority questions", "ha questions"),
  about="Q! questions, answers and metadata.")

d("un.nipo", "Nipocalimab", "/unstructured-explorer", {"doctype": "nipo"}, parent="unstructured", perms=("viewUnstructuredNipo",),
  aliases=("nipocalimab", "nipo", "nipo explorer"),
  about="Nipocalimab documents. Slower than the others - it groups a very large chunk table.")
d("un.nipo.explore", "Explore", "/unstructured-explorer", {"doctype": "nipo", "nipotab": "explore"},
  parent="un.nipo", perms=("viewUnstructuredNipoExplore",), aliases=("explore", "nipo explore"), ambiguous_alone=True)
d("un.nipo.table", "Data table", "/unstructured-explorer", {"doctype": "nipo", "nipotab": "table"},
  parent="un.nipo", perms=("viewUnstructuredNipoTable",), aliases=("data table", "nipo data table", "nipo table"), ambiguous_alone=True)

# ---- Structured Explorer ----
d("structured", "Structured Explorer", "/gra-data-domains", perms=("viewGraDataDomains",),
  aliases=("structured explorer", "structured packages", "structured", "gra data domains",
           "data domains", "gra domains", "domains", "domain owners", "data stewards", "stewards",
           "sponsors", "who owns", "ownership", "orbit", "find it"),
  about="Who sponsors, stewards and owns each structured data domain. Orbit view by default.")
d("structured.directory", "Directory", "/gra-data-domains", {"view": "directory"}, parent="structured",
  aliases=("directory", "directory view", "domain directory", "domain list", "list of domains"),
  about="The same domains as a searchable directory list.")

# ---- Data Studio ----
d("studio", "Data Studio", "/data-studio", perms=("viewDataStudio",),
  aliases=("data studio", "studio"),
  about="Snowflake, Postgres, PG Index Health, S3 Explorer and StreamSets Extractor tabs. Without a tab it reopens whichever was used last.")
d("studio.snowflake", "Snowflake", "/data-studio", {"pane": "snowflake"}, parent="studio",
  perms=("runSnowflakeQueryRunner",),
  aliases=("snowflake", "snowflake worksheet", "snowflake worksheets", "worksheet", "worksheets",
           "sql worksheet", "run sql", "snowflake sql", "query snowflake", "data editor",
           "edit table rows", "edit rows", "saved queries", "query history", "switch snowflake role",
           "run all statements"),
  about="Worksheets with full SQL against Snowflake, object explorer, results grid, data editor. Each worksheet picks its login and role.")
d("studio.postgres", "Postgres", "/data-studio", {"pane": "postgres"}, parent="studio",
  perms=("runPostgresStudio",),
  aliases=("postgres worksheet", "postgres worksheets", "query postgres", "postgresql worksheet",
           "postgres data editor", "edit postgres rows", "postgres sql worksheet", "switch postgres user"),
  about="Worksheets with full SQL against PostgreSQL, same as the Snowflake tab: explorer, results grid, data editor. "
        "Each worksheet picks its login and database and keeps its own session, so SET and temp tables carry over between runs.")
d("studio.copier", "Copy tables", "/data-studio", {"pane": "snowflake"}, parent="studio.snowflake",
  aliases=("table copier", "copy tables", "copy table", "copy a table", "copy schema", "clone table"),
  then_click="Copy tables",
  about="Table Copier: copy a table or a whole schema (opens from the 'Copy tables' button).")
d("studio.indexer", "Index Health", "/data-studio", {"pane": "indexer"}, parent="studio",
  perms=("viewPGindexer",),
  aliases=("index health", "indexer", "postgres indexes", "missing indexes", "index check", "build indexes"),
  about="Pick a PostgreSQL table, say how its columns are queried, see missing or broken indexes and build them.")
d("studio.s3", "S3 Explorer", "/data-studio", {"pane": "s3"}, parent="studio",
  perms=("runS3Explorer",),
  aliases=("s3 explorer", "s3", "s3 buckets", "buckets", "bucket", "aws s3", "browse s3", "s3 files"),
  about="Browse S3 buckets and folders, preview, download, copy URIs, search keys.")
d("studio.streamsets", "StreamSets Extractor", "/data-studio", {"pane": "streamsets"}, parent="studio",
  perms=("runStreamsetsExtractor",),
  aliases=("streamsets", "streamsets extractor", "stream sets", "pipeline extractor", "extract pipelines",
           "pipeline report", "control hub", "sdc pipelines", "fragments"),
  about="Browse StreamSets pipelines, see sources, transformations, destinations and fragments, and extract reports for the ones you pick.")
d("studio.shuttle", "Data Shuttle", "/data-studio", {"pane": "shuttle"}, parent="studio",
  perms=("runDataShuttle",),
  aliases=("data shuttle", "shuttle", "copy postgres to snowflake", "copy snowflake to postgres",
           "move table to snowflake", "move table to postgres", "pg to snowflake", "snowflake to pg"),
  about="Copy tables between Postgres and Snowflake in either direction, in parallel, with an exact row count check before the target changes.")

# ---- Data API Export ----
d("dataapi", "Data API Export", "/data-api", perms=("manageDataAPIExports",),
  aliases=("data api export", "data api exports", "data api", "data apis", "api export",
           "api exports", "exports", "published apis", "table apis"),
  about="Publish tables as key-authenticated read-only APIs. Opens on the dashboard: traffic across every API, live calls and what needs attention.")
d("dataapi.new", "New API", "/data-api", {"new": "1"}, parent="dataapi",
  aliases=("new api wizard", "create api", "new api", "create data api", "new data api", "create export",
           "new export", "create wizard", "publish a table"),
  about="The create wizard.")
for tab_id, label, al in (
    ("overview", "Overview", ("api overview",)),
    ("tables", "Tables", ("api tables", "exposed tables")),
    ("search", "Semantic search", ("semantic search", "semantic search settings", "vector search settings",
                                   "embedding models", "response templates")),
    ("endpoints", "Endpoints", ("endpoints", "api endpoints", "api urls", "urls")),
    ("docs", "Docs", ("api docs", "api documentation", "docs", "documentation")),
    ("activity", "Activity", ("api activity", "api usage", "requests", "usage")),
):
    d(f"dataapi.{tab_id}", label, "/data-api", {"tab": tab_id}, parent="dataapi", aliases=al,
      ambiguous_alone=tab_id in ("docs", "overview", "requests", "usage"),
      about=f"{label} tab of an API (first API unless api=<slug> is passed).")

# ---- RAGs ----
d("rag", "RAGs", "/rag", perms=("viewRAG",),
  aliases=("rags", "rag", "retrieval augmented generation", "document q&a", "ask documents",
           "knowledge bases"),
  about="Chat over document sets. Opens on the first tab you have access to.")
d("rag.epod", "EPOD RAG", "/rag", {"tab": "epod"}, parent="rag", perms=("viewEPodRAG",),
  aliases=("epod rag", "epod chat", "ask epod"))
d("rag.rimdocs", "RIMDOCS RAG", "/rag", {"tab": "rimdocs"}, parent="rag", perms=("viewRimDocsRAG",),
  aliases=("rimdocs rag", "rim docs rag", "rimdocs chat", "ask rimdocs"))
d("rag.agentic", "RIMDocs Agentic", "/rag", {"tab": "rimdocs-agentic"}, parent="rag",
  perms=("viewRimDocsAgentic",),
  aliases=("rimdocs agentic", "agentic", "agentic rag", "rim docs agentic"))

# ---- AI assistant page ----
d("aichat", "AI assistant", "/ai-assistant", perms=("viewAIChatbot",),
  aliases=("ai assistant page", "ai assistant", "ai chatbot", "chatbot", "streamlit chatbot",
           "gradh ai assistant"),
  about="A separate Streamlit chatbot embedded in the page - not Regwiz.")

# ---- User Management ----
d("users", "User Management", "/user-management", perms=("viewUserManagement",),
  aliases=("user management", "manage users", "rbac", "access control", "user admin"),
  about="Access Map, Roles & Permissions and Users tabs. Opens on Access Map.")
d("users.groups", "Access Map", "/user-management", {"tab": "groups"}, parent="users",
  aliases=("groups", "ad groups", "ad group", "group mappings", "ad group mappings", "group mapping",
           "access map", "access flow", "security groups"),
  about="How access flows from AD groups and manual assignments to roles and features, "
        "plus the AD group to role mappings and their members.")
d("users.roles", "Roles & Permissions", "/user-management", {"tab": "roles"}, parent="users",
  aliases=("roles", "role", "roles and permissions", "roles & permissions", "permissions",
           "permission", "custom roles", "create role", "new role", "edit role", "role permissions"),
  about="Built-in and custom roles and which permissions each grants.")
d("users.users", "Users", "/user-management", {"tab": "users"}, parent="users",
  aliases=("users", "users tab", "user list", "people", "assign role", "user roles",
           "change someone's role", "find a user", "directory users",
           "custom access", "user permissions", "access matrix"),
  about="Search users, assign a role directly (overrides AD group) and give custom access "
        "on top of a role from the access matrix.")

# ---- Communication Center ----
d("comms", "Communication Center", "/communication-center", perms=("sendCommunications",),
  aliases=("communication center", "communications", "comms", "email center"),
  about="Email portal users. Tabs: Compose, What's new, Queue, Sent history.")
d("comms.compose", "Compose", "/communication-center", {"tab": "compose"}, parent="comms",
  aliases=("compose", "send email", "email users", "send an email", "new message", "write email",
           "announcement", "send announcement", "maintenance notice", "send update",
           "send warning", "broadcast", "templates"),
  about="Write and send (or schedule) an email to users, with templates and live preview.")
d("comms.whatsnew", "What's new", "/communication-center", {"tab": "whats-new"}, parent="comms",
  aliases=("what's new", "whats new", "release notes", "changelog", "new features"),
  about="Release notes; any entry can be turned into a draft.")
d("comms.queue", "Queue", "/communication-center", {"tab": "queue"}, parent="comms",
  aliases=("queue", "scheduled emails", "scheduled sends", "scheduled messages", "outbox",
           "cancel scheduled"),
  about="Scheduled sends; cancel any until it goes out.")
d("comms.history", "Sent history", "/communication-center", {"tab": "history"}, parent="comms",
  aliases=("sent history", "sent emails", "sent messages", "email history", "history of emails",
           "who did we email", "resend"),
  about="Everything sent: search, CSV export, resend, reopen as draft.")

# ---- Audit Log ----
d("audit", "Audit Log", "/audit", perms=("viewAudit",),
  aliases=("audit log", "audit", "audit trail", "activity log", "stream", "event stream",
           "who is online", "who's signed in", "active users"),
  about="Who is signed in now plus every sign-in, page view and role change. Stream view by default.")
d("audit.analytics", "Analytics", "/audit", {"view": "analytics"}, parent="audit",
  aliases=("audit analytics", "analytics", "activity analytics", "usage analytics", "usage charts",
           "portal usage"),
  about="Charts of portal activity.")
for key, label, patch, al in (
    ("security", "Security review", {"cat": "Roles", "range": "7d"},
     ("security review", "role changes", "role change log", "roles category")),
    ("access", "Access issues", {"cat": "Access", "q": "timeout"},
     ("access issues", "session timeouts", "timeouts", "login problems", "sign in issues")),
    ("admin", "Admin trail", {"cat": "Admin", "range": "all"},
     ("admin trail", "admin actions", "admin activity")),
    ("rules", "Rule decisions", {"cat": "Data", "range": "30d", "q": "rule"},
     ("rule decisions", "rule approvals", "review decisions")),
    ("nav", "Navigation", {"cat": "Navigation", "range": "24h"},
     ("page views", "navigation events", "navigation log")),
):
    d(f"audit.{key}", label, "/audit", patch, parent="audit", aliases=al,
      about=f"Saved view '{label}'.")


DESTINATIONS = {x.id: x for x in _D}
assert len(DESTINATIONS) == len(_D), "duplicate destination id"

# Query-string keys each route understands, and the allowed values where the
# set is fixed. Anything else the model tries to pass is dropped.
ALLOWED_PARAMS = {
    "/dqe": {"check": {"IT_Checks", "Data_Validation", "Business_defined_checks"},
             "tab": {"overview", "dataset-health", "trends", "failed-rows", "admin", "review"},
             "dqtab": {"rule-input", "rule-registry", "date-input"},
             "view": {"standard"}, "mode": {"delta"}},
    "/rag": {"tab": {"epod", "rimdocs", "rimdocs-agentic"}},
    "/user-management": {"tab": {"groups", "roles", "users"}},
    "/communication-center": {"tab": {"compose", "whats-new", "queue", "history"}},
    "/data-studio": {"pane": {"snowflake", "postgres", "indexer", "s3", "streamsets", "shuttle"}, "bucket": None, "account": None, "prefix": None,
                     "key": None, "q": None, "view": {"search"}},
    "/unstructured-explorer": {"doctype": {"vr", "immbot", "mirai", "rmwcreate", "rimdocs", "fda", "epod", "haq", "nipo"},
                               "tab": {"overview", "table", "breakdown", "query", "postgres"},
                               "epodtab": {"explore", "table"}, "nipotab": {"explore", "table"},
                               "fdatab": {"explore", "table"},
                               "immbottab": {"explore", "table", "breakdown"},
                               "vrtab": {"explore", "library", "table"},
                               "vrscope": {"internal", "external"},
                               "fdadashboard": {x[0] for x in FDA_DASHBOARDS},
                               "epoddataset": None},
    "/gra-data-domains": {"view": {"directory"}, "tab": {"people", "records"}, "domain": None,
                          "open": {"1"}, "q": None},
    "/data-api": {"api": None, "tab": {"overview", "tables", "search", "endpoints", "docs", "activity"},
                  "new": {"1"}},
    "/audit": {"view": {"analytics", "stream"}, "q": None, "cat": {"all", "Roles", "Access", "Data",
               "Navigation", "Admin"}, "range": {"24h", "7d", "30d", "all"}, "user": None,
               "action": None, "from": None, "to": None, "event": None},
    # q runs the search on arrival (with ~0.9 / ~0.8 for typos), m=meta searches the catalog metadata
    "/fuzzy-match": {"q": None, "m": {"meta"}}, "/dashboard": {}, "/ai-assistant": {},
    "/ingestion-lab": {},
}
ROUTES = set(ALLOWED_PARAMS)

# Words that flip a BDC destination into delta (Semi-Monthly) data.
# "delta report" is a report on Failed Rows in either mode, so it doesn't count
_DELTA_WORDS = re.compile(r"\b(delta(?! report)(?! all rules)|semi ?monthly|bi ?weekly|biweekly)\b")
_AUDIT_RANGE = [(re.compile(r"\b(today|last 24 ?h(ours)?|past day)\b"), "24h"),
                (re.compile(r"\b(last|past) (30 days|month)\b|\bthis month\b"), "30d"),
                (re.compile(r"\ball time\b|\bever\b"), "all")]


# ---------------------------------------------------------------- lookups

def lineage(dest_id):
    out, cur = [], DESTINATIONS.get(dest_id)
    while cur:
        out.append(cur)
        cur = DESTINATIONS.get(cur.parent) if cur.parent else None
    return list(reversed(out))


def breadcrumb(dest_id):
    return " › ".join(x.name for x in lineage(dest_id))


def required_perms(dest_id):
    perms = []
    for x in lineage(dest_id):
        perms.extend(p for p in x.perms if p not in perms)
    return perms


def can_access(dest_id, perms):
    return all(perms.get(p) is True for p in required_perms(dest_id))


def is_ancestor(maybe_parent, child_id):
    return any(x.id == maybe_parent for x in lineage(child_id)[:-1])


def url_for(dest_id, extra=None):
    x = DESTINATIONS[dest_id]
    params = dict(x.params)
    if extra:
        params.update(extra)
    return x.route, clean_params(x.route, params)


def clean_params(route, params):
    allowed = ALLOWED_PARAMS.get(route, {})
    out = {}
    for k, v in (params or {}).items():
        if k not in allowed or v is None:
            continue
        v = str(v)
        values = allowed[k]
        if values is not None and v not in values:
            continue
        out[k] = v
    return out


def match_url(route, params):
    """The most specific destination a route+params pair corresponds to, or None."""
    route = (route or "").split("?")[0].rstrip("/") or "/"
    params = {k: str(v) for k, v in (params or {}).items()}
    best, best_n = None, -1
    for x in _D:
        if x.route != route or x.then_click or x.same_url_as:
            continue
        if all(params.get(k) == v for k, v in x.params.items()) and len(x.params) > best_n:
            best, best_n = x, len(x.params)
    return best


def describe_url(url_or_path):
    if not url_or_path:
        return None
    parts = urlsplit(url_or_path)
    params = dict(parse_qsl(parts.query))
    x = match_url(parts.path, params)
    if not x:
        return None
    crumb = breadcrumb(x.id)
    # executive users on BDC without view=standard are really on the Executive View
    if x.id == "bdc" and "view" not in params:
        crumb += " (Executive View if the user has it, otherwise Overview)"
    if "bdc" in {y.id for y in lineage(x.id)}:
        crumb += " — showing " + ("Semi-Monthly (delta) data" if params.get("mode") == "delta" else "Quarterly data")
    return {"id": x.id, "breadcrumb": crumb, "params": params}


# ---------------------------------------------------------------- resolver

_FILLER = set("""
a an the to me my i we us our you your please pls can could would will just
take go goto bring show open navigate head jump get switch find see view where
is are was it that this there page tab tabs section screen area part menu place
of for on in at into from with about how do does let lets want need like
now quickly real quick hey hi okay ok regwiz up over
""".split())

_SYNONYMS = [
    (r"\bq!", "haq"),
    (r"&", " and "),
    (r"\bsuper[\s-]+user", "superuser"),
    (r"\bsign[\s-]*off\b", "sign off"),
    (r"\bdata[\s-]*type\b", "data type"),
    (r"\bdatatypes?\b", "data type"),
    (r"\bperms\b", "permissions"),
    (r"\buser[\s-]*mgmt\b", "user management"),
    (r"\bexec\b", "executive"),
    (r"\bdash\b", "dashboard"),
    (r"\bwhat'?s new\b", "whats new"),
    (r"\brim[\s-]*docs\b", "rimdocs"),
    (r"\be[\s-]*pod\b", "epod"),
    (r"\bs3\b", "s3"),
    (r"\bdb\b", "database"),
]


def _norm(text):
    t = (text or "").lower()
    for pat, rep in _SYNONYMS:
        t = re.sub(pat, rep, t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _words(text):
    return [w for w in _norm(text).split() if w]


def _phrase_at(phrase_words, query_words):
    """Positions of the first place the phrase occurs, or empty set."""
    n = len(phrase_words)
    for i in range(len(query_words) - n + 1):
        if query_words[i:i + n] == phrase_words:
            return set(range(i, i + n))
    return set()


def _stem(w):
    for suf in ("ies", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


_PHRASES = {}  # id -> list of (stemmed word list, weight)


def _phrases(x):
    if x.id not in _PHRASES:
        out = []
        for p in (x.name,) + tuple(x.aliases):
            ws = [_stem(w) for w in _norm(p).split()]
            # the query has filler stripped, so the alias must too
            core = [w for w in ws if w not in _FILLER]
            ws = core or ws
            if ws and ws not in out:
                out.append(ws)
        _PHRASES[x.id] = out
    return _PHRASES[x.id]


def _own_match(x, qwords, taken=frozenset()):
    """Score for how much of the request this place's names cover, as
    (score, query positions). Every non-overlapping name/alias hit counts, so
    'IT checks row counts' scores IT Checks on both parts and beats Data Type
    Validation, which only matches 'row counts'. Filler is already out of the
    query so 'take me to the roles tab' still hits 'roles'. Words an ancestor
    already claimed don't count - that stops 'user management' scoring for the
    Users tab."""
    hits = []
    for ws in _phrases(x):
        pos = _phrase_at(ws, qwords)
        if pos and not pos <= taken:
            hits.append((len(ws) + 0.01 * sum(len(w) for w in ws), pos))
    hits.sort(key=lambda h: -h[0])
    score, where = 0.0, set()
    for sc, pos in hits:          # longest first, skip overlaps
        if pos & where:
            continue
        # the first hit counts fully, extra coverage a bit less
        score += sc if not where else 0.8 * sc
        where |= pos
    return score, where


_VOCAB = None


def _fix_typos(words):
    """Snap a misspelt word to the closest word any alias uses ('buisness',
    'superusr'). Short words are left alone - too easy to snap wrongly."""
    global _VOCAB
    if _VOCAB is None:
        _VOCAB = sorted({w for x in _D for ws in _phrases(x) for w in ws})
    vocab = set(_VOCAB)
    out = []
    for w in words:
        if w in vocab or len(w) < 5 or w.isdigit():
            out.append(w)
            continue
        hit = get_close_matches(w, _VOCAB, n=1, cutoff=0.84)
        out.append(hit[0] if hit else w)
    return out


def resolve(text, perms=None, current=None, limit=5):
    """Rank destinations for a free-text request.

    Returns {"best": [...], "ambiguous": bool, "matches": [...]} where each
    match is {id, name, breadcrumb, score, allowed}. `current` is the
    destination id the user is on now; siblings of it win ties, so "go to
    trends" on the BDC page means BDC trends.
    """
    qwords_all = [_stem(w) for w in _words(text)]
    qwords = _fix_typos([w for w in qwords_all if w not in _FILLER] or qwords_all)
    if not qwords:
        return {"best": None, "ambiguous": False, "matches": []}

    raw = {x.id: _own_match(x, qwords) for x in _D}
    cur_line = {y.id for y in lineage(current)} if current in DESTINATIONS else set()

    scored = []
    for x in _D:
        if raw[x.id][0] <= 0:
            continue
        line = lineage(x.id)
        taken = set()
        for a in line[:-1]:
            taken |= raw[a.id][1]
        s, _ = _own_match(x, qwords, frozenset(taken))
        if s <= 0:
            continue
        # an ancestor also named in the request ("business checks trends")
        anc = sum(raw[a.id][0] for a in line[:-1] if raw[a.id][0] > 0)
        s += 0.8 * anc
        # "overview", "data table" etc. only count when the parent is named or
        # the user is already inside that parent
        if x.ambiguous_alone and anc == 0 and x.parent not in cur_line:
            s *= 0.4
        # closer to where the user is = more likely; deeper shared ancestry wins
        s += 0.2 * len(cur_line & {a.id for a in line[:-1]})
        # deeper wins over its own parent when both matched the same words
        s += 0.05 * (len(line) - 1)
        scored.append((s, x))

    # a parent that only matched because its child's alias overlaps is noise
    scored.sort(key=lambda p: -p[0])
    matches = []
    for s, x in scored[: limit * 3]:
        matches.append({
            "id": x.id, "name": x.name, "breadcrumb": breadcrumb(x.id),
            "score": round(s, 3),
            "allowed": can_access(x.id, perms) if perms is not None else True,
        })
    # collapse ancestors of a higher-ranked match that scored the same words
    pruned = []
    for m in matches:
        if any(is_ancestor(m["id"], p["id"]) and p["score"] >= m["score"] - 0.2 for p in pruned):
            continue
        pruned.append(m)
    pruned = pruned[:limit]

    if not pruned:
        return {"best": None, "ambiguous": False, "matches": []}
    # near-ties at the top: if only one of them is a place this user can open,
    # that's the answer - "overview" isn't ambiguous for someone who only has BDC
    close = [m for m in pruned if m["score"] >= pruned[0]["score"] - 0.15]
    if perms is not None:
        open_ones = [m for m in close if m["allowed"]]
        if open_ones:
            close = open_ones
            pruned = open_ones + [m for m in pruned if m not in open_ones]
    top = close[0]
    rivals = [m for m in close[1:]
              if not is_ancestor(m["id"], top["id"]) and not is_ancestor(top["id"], m["id"])]
    return {"best": top, "ambiguous": bool(rivals), "matches": pruned}


_QUARTERLY_WORDS = re.compile(r"\b(quarterly|quarter|normal data|standard data|turn off delta|exit delta)\b")


def modifiers_for(text, dest_id):
    """Extra params implied by the wording: delta for BDC, time range for audit.
    mode=None means 'switch back to Quarterly' - clean_params drops it."""
    t = _norm(text)
    extra = {}
    ids = {x.id for x in lineage(dest_id)}
    if "bdc" in ids and _QUARTERLY_WORDS.search(t) and not _DELTA_WORDS.search(t):
        extra["mode"] = None
    elif "bdc" in ids and _DELTA_WORDS.search(t):
        extra["mode"] = "delta"
    if "audit" in ids:
        for pat, rng in _AUDIT_RANGE:
            if pat.search(t):
                extra["range"] = rng
                break
    return extra


_NAV_INTENT = re.compile(
    r"\b(take me|bring me|go to|goto|go back to|navigate|open( up)?|show me|pull up|bring up|"
    r"jump to|switch to|head to|get me to|where (is|are|do i find|can i find)|how do i get to|"
    r"how do i (open|find|reach)|i want to (see|go|open)|let me see|can i see|take us|"
    r"send me to|direct me|point me)\b")


def looks_like_navigation(text):
    return bool(_NAV_INTENT.search((text or "").lower()))


# ---------------------------------------------------------------- text renderers

def catalog_lines(perms=None):
    """One line per destination the user can reach: id — breadcrumb — what it is."""
    lines = []
    for x in _D:
        if perms is not None and not can_access(x.id, perms):
            continue
        lines.append(f"{x.id} — {breadcrumb(x.id)}" + (f" — {x.about}" if x.about else ""))
    return lines


def knowledge_markdown():
    """The whole map as KB text, one '## ' section per top-level module."""
    out = ["# Portal navigation map (generated from assistant/portal_map.py)\n"]
    roots = [x for x in _D if x.parent is None]
    for r in roots:
        out.append(f"## Where things are: {r.name}\n")
        stack = [(r, 0)]
        while stack:
            x, depth = stack.pop(0)
            route, params = url_for(x.id)
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url = route + (f"?{qs}" if qs else "")
            perms = ", ".join(required_perms(x.id)) or "none"
            alias = ", ".join(x.aliases[:6])
            line = f"{'  ' * depth}- {breadcrumb(x.id)} — URL {url} — needs {perms}."
            if x.about:
                line += f" {x.about}"
            if x.then_click:
                line += f" (Opened by the '{x.then_click}' button on that page.)"
            if alias:
                line += f" Also called: {alias}."
            out.append(line)
            kids = [k for k in _D if k.parent == x.id]
            stack = [(k, depth + 1) for k in kids] + stack
        out.append("")
    return "\n".join(out)