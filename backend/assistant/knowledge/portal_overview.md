# GRA-SmartHub portal knowledge

Hand-written knowledge the code can't express on its own: what each feature is
for and how people get things done. The assistant treats this as ground truth,
so every line here was checked against the frontend source. Exact URLs for
every page, tab and sub-tab come from assistant/portal_map.py (embedded as the
"Where things are" sections) - don't restate them by hand here.

## What the portal is

GRA-SmartHub is a data quality and governance portal owned by the RDI AI SQUAD,
for internal J&J use only. It validates data migrated from source systems into
Snowflake, runs business-defined data-quality rules, and gives explorers over
structured and unstructured regulatory data. Every page is permission-gated, so
a user only sees the pages and tabs their role allows. The in-portal assistant
(the floating avatar) is called Regwiz.

## Sidebar layout

The sidebar is grouped, top to bottom:
- Workspace: Dashboard, Data quality, Ingestion Lab, Fuzzy match
- Explorers: Unstructured Explorer, Structured Explorer, Data Studio, Data API Export
- Intelligence: RAGs, AI assistant
- Admin: User Management, Communication Center, Audit Log

Inside a group, tools marked NEW come first. A group only shows if the user can
open something in it.

## Dashboard

The landing page after sign-in. Top to bottom:
- A greeting with the user's first name, and today's date and time top right.
- A search bar ("Search tools, sections, workspaces…"). Typing lists matching
  tools and the sections inside them (e.g. "snowflake" jumps straight to Data
  Studio's Snowflake tab); Enter or a click opens the result. Ctrl K (⌘ K on a
  Mac), or the search button in the header, opens the same search as a pop-up
  from anywhere on the page.
- "Pick up where you left off" - up to three places the user visited in the
  last 30 days, most recent first, each reopening the exact tab they were on.
  It only appears once they've opened a tool from this browser.
- An animated ring of every tool the user can open. Hovering a tool pauses the
  ring and shows its description; clicking opens it.
- "Your toolkit" - one card per tool the user can open, NEW tools first. Filter
  pills across the top: All, Data, AI, Explorers, Admin (a pill only shows if
  the user has a tool in it). Some cards have shortcut chips straight to a tab.
- "What's new" - the latest announced release notes. People who can send
  communications also get an "All release notes" link to the Communication
  Center.
- "Move faster" - the keyboard shortcuts for the search.

The dashboard categories group tools as: Data (Data Quality Engine, Data API
Export, Fuzzy Match, Ingestion Lab), AI (RAG, AI Assistant), Explorers
(Unstructured Explorer, Structured Explorer, Data Studio) and Admin (User
Management, Communication Center, Audit Log). The sidebar keeps its own
grouping, listed above.

The account menu in the Dashboard's top-right header holds the "AI assistant"
toggle that turns the Regwiz avatar on or off.

## Data quality (DQE) - choosing a check type

Data quality opens on "Select Validation Parameters", where the user picks one
of three check types (only the ones their role allows are shown) and clicks
"Load Validation Analysis". A user who can only see one check type skips this
screen and goes straight in. Inside any check type, "Switch Check Type" goes
back to the picker.

- IT Checks: source-to-target migration statistics per table - row counts,
  schema/column comparison (source-only and Snowflake-only columns), constraint
  checks, null count and duplicate count analysis, distinct counts. You can
  filter by source/target database, schema, table and column, filter by status,
  pick which past runs to load, and refresh a single table. Users with the
  runITChecks permission also get "Refresh" and "Run Validation" buttons
  (Run Validation lets them pick which Oracle databases to validate).
- Data Validation (labelled Beta): a guided source-to-target comparison. You pick
  the source system, environment, database, schema and table, run the
  comparison, then download the report. Source systems offered: Insight7,
  RIMDocs, Snowflake, PostgreSQL, DocuBridge, Denodo, Lift, Grail and Databricks.
- Business Defined Checks (BDC): see the next sections.

## Business Defined Checks - what it is

The business writes a rule in plain English and picks the dataset/table it
applies to. AI reads sample rows from that table, links the rule to it and
generates a SQL predicate that decides which rows fail. Rules run against live
data; results include pass rates, failed rows and trends over time. Each rule
shows the AI-interpreted rule, an AI confidence score, the columns used, its
assumptions and the generated SQL predicate.

Two data cadences: Quarterly (the default) and Semi-Monthly (also called delta
mode). The toggle for Semi-Monthly lives in the page header; the Executive View
has a Quarterly / Semi-Monthly switch too.

Users with the viewBDCExecutive permission land on the Executive View first: a
one-screen summary with pass rate over time, check dimensions and trend. Its
"Standard View" button opens the full workspace with tabs. Everyone else lands
straight on the tabs.

## Business Defined Checks - tabs

- Overview: three KPI cards (tables monitored, rules run, average pass rate) and
  the Active Rules table - rule ID, title, severity, target Snowflake table, a
  colour-coded pass-rate bar (green at 95% or more, blue at 80% or more, red
  below) and status.
- Dataset Info: rules grouped by business dataset with an average pass rate and
  a status badge (Healthy at 95% or more, Warning at 80% or more, Critical
  below). Click a row to see every rule in that dataset.
- Trends: pick a rule and see its pass rate across every historical execution.
- Failed Rows: pick a rule, read its SQL predicate (expand + Copy), click "Show
  Failed Rows" to load failures 100 per page. If the rule has COLOR_COLUMNS set,
  rows are banded by days between two dates: green 7 or less, yellow 8-15, red
  16 or more, and you can filter by band. Delta Report compares the current run
  with the previous one (New Failures and Resolved Rows sheets, Excel download).
  Advanced Delta Report compares against any archived run. The Consolidated
  Report card builds one report of failed rows across rules, with a history of
  earlier consolidated reports.
- Superuser (admins only, viewBDCSuperuser): "Superuser Configuration" with the
  DQ Tables section and three sub-tabs - DQ Rule Governance (the source-of-truth
  rule table: create rules, edit them - edits create a new versioned row and
  deactivate the old one - and toggle rules active/inactive), DQ Rules AI
  Registry (validated AI SQL predicates) and DQ Date Input (the date ranges
  behind the Quarterly and Semi-Monthly data). "Superuser" is a tab on this
  page, not a role.
- Review (admins only, viewBDCSuperuser): the sign-off queue. Every active rule
  with its latest result; Accept or Reject each one with a note. A progress bar
  tracks how many are reviewed, pending rules are listed first, and the tab
  badge shows how many still need a decision.

Header buttons (users with runBDCOperations): "Run Compilation" builds the rule
logic in Snowflake; "Run Validation" executes rules and streams the output live,
with modes New Rules Only, All Rules, Specific Rule and Delta - All Rules;
"Refresh" reloads rule metadata. "Take a Tour" starts the page's own guided tour.

## How to do common Business Defined Checks tasks

- Create a new rule: Data quality › Business Defined Checks › Superuser › DQ Rule
  Governance, then create the rule there. Needs the Superuser tab (viewBDCSuperuser).
- Approve or reject rules: Business Defined Checks › Review, Accept or Reject
  each rule and leave a note.
- See why a rule fails: Failed Rows tab, pick the rule, read the SQL predicate,
  then Show Failed Rows.
- See what changed since the last run: Failed Rows tab, Delta Report.
- Track a rule over time: Trends tab, pick the rule.
- Find the weakest dataset: Dataset Info tab.
- Switch to Semi-Monthly data: the Semi-Monthly toggle in the header (or the
  Quarterly / Semi-Monthly switch on the Executive View).

## Where to find row counts

Row counts are shown in Data quality › IT Checks - source vs target (Snowflake) row counts for each migrated table.
Separately, a query you run in Data Studio › Snowflake shows how many rows the result returned,
and Structured Explorer shows a record count per domain - those are not table row-count checks.

## Rule concepts

- Rule: one data-quality check against a target table, with a severity, a review
  status, a target table and a pass percentage. Its logic is a SQL predicate.
- Pass percentage: share of rows that satisfy the rule on its latest run.
- Review status: Approved (Accepted), Rejected, or Awaiting Review (not yet
  reviewed). Only Approved rules show to regular users in the Active Rules
  table; Awaiting Review and Rejected rules are visible only in the Review tab
  to admins. Quarterly and Semi-Monthly rules are signed off separately.
- Severity: chosen when the rule is created (High, Medium, Low or None;
  Critical can also appear). The AI does not use severity in its logic - it is
  there so people can gauge importance.
- Dataset status: Healthy, Warning or Critical from the dataset's average pass
  rate (thresholds above).

## Ingestion Lab

Runs the ingestion steps set up in backend/ingestion_lab.yaml (for example the
document ingestion pipeline) on the portal server or on other servers over SSH. The server switcher at
the top picks where to run; each server shows whether the portal can reach it and
how many runs it has going. A step runs once at a time, per server or across all
servers for steps that write to a shared database. The user
picks a step, fills in its inputs (environment, table and so on) and clicks Run.
Steps on other servers keep running if the connection or the portal drops and are
picked up again automatically. The command's output streams live; the run keeps going on the
server if the page is closed, and reopening the page reattaches to it. Stop ends
the command and everything it started. Run history lists every run with who
started it, its inputs, status, duration and exit code; any run's log can be
reopened or downloaded, and Rerun fills the form with that run's inputs. Viewing
needs Ingestion Lab access; starting or stopping needs Run Ingestion Steps.

## Fuzzy match

The page is titled "Fuzzy Search". It searches the Denodo data catalog across
the connected databases and views for a word or value.
- Records or Metadata: Records finds rows that contain the term; Metadata finds
  views whose name, description or field names contain every word typed.
- "How close a match" slider (Records only), strict to loose, each stop adding
  to the one before: Exact word; Longer words (also words that start with it,
  e.g. customers, customer_id); 1 typo; 2 typos. Denodo can't match more than
  2 letters off. It can also be typed: "stelara*" for longer words,
  "stelara~0.9" for 1 typo, "stelara~0.8" for 2, combined as "stelara*~0.9".
  The card under the search box previews which spellings the setting finds.
- Sources (GRAIL, Insight, Insight7, LIFT, RIMDocs, URP) narrow the views.
  Databases works the same way for records: the Denodo databases with a
  search index (by default only gra_dh_urp_analysis_src). Metadata searches
  always cover every database, so there is no database choice for them.
  Both can also be changed from the results page.
- A new term usually takes 30 to 60 seconds (Denodo is slow); a repeat within
  30 minutes is instant from the cache. "Refresh from source" asks Denodo again.
  The search can be stopped with the Stop button.
- Results: totals, a bar of matches by source (click to filter), and a list of
  views on the left. Each view shows its records with hits underlined (red for
  exact, yellow for a typo match). Denodo returns 10 rows per view and cuts text
  at 300 characters: "Load 200 rows" reads the rest from the view, the open
  button on a row shows the full record read live, and an orange dot means the
  match is in text past the cut. "Where it matched" shows hits per column.
- Switches: Only rows with a highlighted hit, Focus matching columns (fades
  columns with no hit), Compact rows. The Metadata button shows the view's
  catalog entry.
- Export: Excel with Records only, All matching rows (up to 5,000 per view,
  slower) or Records and metadata. Metadata results export to CSV.
- The search is in the page link (?q=term, &m=meta for metadata), so it can be
  shared or bookmarked. Needs Fuzzy Match access, plus View Data for records
  and View Metadata for metadata.

## Unstructured Explorer

Opens on a picker of nine document sets, in this order (only the ones your
role allows are shown, numbered by what you see):
1. Virtual Regulator - an Internal / External switch in the header.
   Internal: Snowflake (DEV_RAW.UNSTRUCTURED_EXPLORER.SUBX_FINAL), chunk-level,
   each chunk repeated per document version (latest version is shown). Tabs:
   Overview (product portfolio cards and charts), Documents (one row per
   document with its version history) and Data table (Documents or Chunks view).
   External: pick a health authority, then a table. FDA press releases (from the
   FDA RSS feed) have Explore and Data table; other external tables open as a
   plain data table.
2. IMMBOT - Snowflake (DEV_RAW.UNSTRUCTURED_EXPLORER.IMMBOT_FINAL),
   chunk-level. Tabs: Explore, Data table (Documents or Chunks view; click a row
   to open the document with its chunks and full text), Breakdown (pick any
   three levels, default Therapeutic area → Compound → Protocol). Explore shows
   compound × phase, primary indication, trial status and ingestion trends.
   Counts are distinct documents (DOC_ID).
3. MirAI - coming soon, placeholder page.
4. RMWCreate - coming soon, placeholder page.
5. Q! - Q! questions, answers and metadata. One view, no sub-tabs.
6. Nipocalimab - Explore / Data table sub-tabs. Noticeably slower than the rest
   because it groups a very large chunk-level table; slow is expected, not broken.
7. RIMDocs - Tabs: Explore, Data table and Domain breakdown. To write your own
   SQL, use Data Studio (Snowflake or Postgres tab).
8. EPOD - Postgres. Explore / Data table sub-tabs and a dataset dropdown.
9. FDA and Others - Postgres. Explore / Data table sub-tabs and a dashboard
   picker: CRL AI Matches, CRL Letters Overview, Pink Sheet Letters, eCTD
   Documents, FDA CRL Letters, Milvexian Metadata, RIM Facility Index.

The explorers have summary cards, charts, filters, a searchable data table, a
row-detail panel, and CSV download of the current page or the whole filtered
set. The whole page needs viewUnstructured. Each document set is its own
permission too (Virtual Regulator, IMMBOT, MirAI, RMWCreate, Q!, Nipocalimab,
RIMDocs, EPOD, FDA and Others), and so is each sub-tab inside it (for example
EPOD Explore vs EPOD Data table, or Virtual Regulator Internal vs External).
Tabs a user can't open are hidden. The two RIMDocs query runners sit under the
RIMDocs permission. Missing a tab means asking an admin in User Management.

## Structured Explorer

Sidebar label "Structured Explorer" (route /gra-data-domains). Shows who
sponsors, stewards and owns every structured GRA data domain, pulled live from
Snowflake. Two views: Orbit (default) and Directory. Selecting a domain opens a
panel with Ownership and Records tabs. Users with editGraDataDomains can edit.

## Data Studio

Tabs across the top, each with its own permission; the page needs viewDataStudio.
Without a tab chosen it reopens whichever tab the user used last.
- Snowflake (runSnowflakeQueryRunner): object explorer on the left, worksheets
  in the middle over a results grid, query profile on the right. Full SQL
  including writes and DDL, with confirmation before destructive statements.
  Ctrl/Cmd + Enter runs the selection or the statement at the cursor;
  Ctrl/Cmd + Shift + Enter (or "Run all") runs every statement in order and shows
  the last result, with each statement listed under Details. Each worksheet has a
  connection dropdown (when several logins are configured) and a role dropdown.
  USE / SET / ALTER SESSION last until the end of that run. The grid has column
  filters, multi-column sort, paging and CSV/Excel/JSON export. Queries can be
  saved and there is run history. Opening a table in the data editor lets you
  edit, add and delete rows and apply the changes. The "Copy tables" button opens
  the Table Copier to copy a table or a whole schema. A deployment can make this
  tab read-only regardless of role.
- Postgres (runPostgresStudio): the same worksheet page for PostgreSQL - explorer,
  worksheets, results grid, data editor, history and saved queries, Run all. Each
  worksheet picks its connection (login) and database in the header, and keeps
  its own session: SET work_mem, temp tables, search_path and an open BEGIN carry
  over between runs until the worksheet is closed, switched or idle 20 minutes.
  No Table Copier on this tab.
- Index Health (viewPGindexer): pick a PostgreSQL table from the DEV/QA/PROD
  tree on the left (or Ctrl/Cmd + K to search), mark how each column is queried
  (Lookup & sort, Text search, Vector search) and run a diagnosis. It shows a
  health score out of 100, the missing indexes ranked critical/high with the
  exact SQL, the current indexes (invalid, redundant, unused) and a coverage
  grid. Diagnosis is read-only. Building the fixes needs buildPGindexer: it runs
  them on the server with CREATE INDEX CONCURRENTLY so the table stays online,
  shows a live log, keeps going if the user leaves the page, and lists running
  builds in a tray bottom right. One build per table at a time; PROD asks the
  user to type the table name first. Cancelling leaves an invalid index that
  the next diagnosis offers to drop.
- S3 Explorer (runS3Explorer): browse buckets and folders, preview and download
  objects, copy the s3:// URI or a short-lived link, search keys under a folder.
  Uploads and deletes only work where the deployment enables them.

A user with the page but neither tab permission sees a message that they have
no Data Studio tools.

## Data API Export

Publish one or more source tables as a key-authenticated, read-only API. Each API
has a name, description, an api_key and a list of tables (source
system/env/database/schema/table, an alias, and a row limit). Consumers call the
generated URL with the key - no portal login needed. The page opens on a
dashboard: requests across every API, live calls, traffic by source and by
client, what needs attention (rejected calls, non-PROD aliases) and a
card per API. Ctrl K jumps to any API. Opening an API shows a list of APIs on
the left and Overview, Tables, Endpoints, Semantic search, Docs and Activity
tabs. Overview has requests by alias, response health, who is calling (by
machine or by tool, with calls, rows and time taken) and the key (hold to
reveal, copy, email it under a Highly Restricted label through the
Communication Center, or rotate). Tables shows each alias's lineage and lets
you add, edit (alias, rows per page, columns) or remove one.
"New API" opens a three-step wizard (details, tables, review) that ends with
"Publish API" and shows the new key once. Keys can be regenerated (the old key
stops working), an API can be deactivated (calls then return 403) or activated, and
deleted. A browsable table view lives at /data-api-view/<api>/<alias> with a
search box (all columns by default, or pick one column - faster on big tables).
The assistant never reveals API keys.

## RAGs

Chat over document sets, one tab each: EPOD RAG, RIMDOCS RAG and RIMDocs
Agentic, each with its own permission and each backed by a separate service. The
page opens on the first tab the user can use.

## AI assistant page

The "AI assistant" page in the sidebar embeds a separate Streamlit chatbot
(titled GRADH AI assistant) in an iframe, with a button to open it in a new tab.
It is not Regwiz; Regwiz is the floating avatar available on every page.

## User Management

Three tabs: Access Map (a flow chart of where access comes from - AD groups,
manual assignments and the default role - into roles and on to the features
they open, with the AD group to role mappings and their members underneath),
Roles & Permissions (built-in and custom roles and which permissions each
grants, as cards or a side-by-side matrix; create and edit custom roles) and
Users (search users and assign a role directly). A role assigned directly to a
user wins over their AD group. After a role change the user has to refresh the
page to pick it up. Built-in roles can be edited or reset to default but not
deleted.

Custom access: on top of their role, one person can be given extra
permissions or have some taken away, from the Users tab's Access matrix view
or the Custom access button in their profile. A sub-permission only works
while its parent is on. Custom access stays when the person's role changes and
is cleared with "Match role exactly".

## Roles

Four built-in roles plus any custom roles:
- Admin - everything, including User Management, Audit Log, Data Studio, Data
  API Export, Communication Center and the Regwiz avatar.
- DQE - RightTime Super User - Business Defined Checks only, including running
  operations, the Superuser and Review tabs, and the Executive View.
- DQE - IT User - read-only IT Checks, Data Validation and Business Defined
  Checks (including the Executive View). Cannot run anything.
- No access (role id `viewer`) - the default role. Dashboard only. Someone who signs in and sees an empty
  portal has almost always landed here because their AD group isn't mapped.

Only Admin has the Regwiz avatar among the built-in roles; custom roles can be
given it.

## Communication Center

Admin-only (sendCommunications). Tabs: Compose, What's new, Queue, Sent history.
- Compose: email portal users - an update, warning, maintenance notice or free
  text. Recipients in To/Cc/Bcc, typed or picked from the directory (an AD group,
  everyone with a role, or one person). Attachments allowed, simple formatting
  toolbar, live preview. "Send test to me" mails only the signed-in admin. Send
  now or "Schedule send" for a later date/time (Eastern). Messages can be saved
  as named templates.
- What's new: release notes; any entry can be turned into a draft.
- Queue: scheduled sends, cancellable until they go out; the server sends them
  whether or not anyone is signed in.
- Sent history: everything sent, searchable, CSV export, attachments kept, and
  any message can be resent or reopened as a new draft.
Every send is written to the audit log.

## Audit Log

Who is signed in right now plus a log of every sign-in, page view, role change
and data/admin action. The banner at the top shows today's activity by hour
(Eastern) on a 24-hour dial. Three views: Stream (default, the event timeline),
Analytics (charts: today by hour, category mix, 30-day volume, weekday x hour
heatmap, top actions, most active people, sensitive events) and People (one
card per person with their event count, sensitive count and recent activity).
Filters: search text, category (Roles, Access, Data, Navigation, Admin), action
type, time range (last 24 hours, 7 days - the default, 30 days, all time), and
under Filters: person and dates. Saved views: Security review, Secrets viewed,
Access issues, Admin trail, Rule decisions. "Sensitive" events (role deleted or
removed, secret viewed, log entry deleted, audit edit mode opened) are flagged
and listed under "Needs a look". Clicking an event opens its details with the
same person's events around it. Live mode (on by default) adds new events as
they happen. Results export to CSV. Every filter is in the URL, so a copied
link opens the same view.

## What Regwiz can and can't do

It can answer questions about the portal, fetch live numbers through tools the
user's role allows, take the user to any page, tab or sub-tab, highlight things
on screen, click/type for them, run a guided tour, and remember a stated
preference. It must not answer about data the user's role can't see, give
numbers it didn't just fetch, reveal keys or credentials, or answer off-topic
questions. When something isn't covered by its knowledge or tools it says so.