export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || window.location.origin;

export async function readJson(res) {
  return res.json().catch(() => ({}));
}

// Full, ready-to-paste endpoint URL for one table on one API.
export function tableUrl(apiId, alias, apiKey) {
  return `${API_BASE_URL}/data-api/${apiId}/${alias}/rows?api_key=${apiKey}&limit=100&offset=0`;
}

// Same table, but the browsable HTML view instead of raw JSON.
// Frontend-hosted React table view — separate origin from the API in dev,
// so this uses window.location.origin (the app), not API_BASE_URL (the API).
export function tableUiUrl(apiId, alias, apiKey) {
  return `${window.location.origin}/data-api-view/${apiId}/${alias}?api_key=${apiKey}`;
}

// Triggers a browser download of a text file — no backend round trip needed.
export function downloadTextFile(filename, content) {
  const blob = new Blob([content], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// Consumer-facing doc, filled in with this API's real id/key/aliases so it's
// ready to hand off as-is — no placeholders for the admin to edit.
export function buildConsumerDoc(apiName, apiId, apiKey, tables) {
  const base = `${API_BASE_URL}/data-api/${apiId}`;
  const list = tables && tables.length ? tables : [];
  const first = list[0];
  const firstAlias = first ? first.alias : '{alias}';
  const tablesJsonExample = list.length
    ? list.map(t => `    { "id": "...", "alias": "${t.alias}", "source_system": "${t.source_system || '...'}", "env": "${t.env || '...'}", "database": ${t.database ? `"${t.database}"` : 'null'}, "schema": ${t.schema ? `"${t.schema}"` : 'null'}, "table": "${t.table || '...'}", "row_limit": ${t.row_limit ?? 1000} }`).join(',\n')
    : '    { "id": "...", "alias": "customers", "source_system": "...", "env": "...", "database": "...", "schema": "...", "table": "...", "row_limit": 1000 }';
  const tableListMd = list.length
    ? list.map(t => `| \`${t.alias}\` | ${t.source_system || ''} | ${t.row_limit ?? 1000} |`).join('\n')
    : '| *(none configured)* | | |';
  const rowsUrl = `${base}/${firstAlias}/rows?api_key=YOUR_API_KEY&limit=500&offset=0`;
  const countUrl = `${base}/${firstAlias}/count?api_key=YOUR_API_KEY`;
  const columnsUrl = `${base}/${firstAlias}/columns?api_key=YOUR_API_KEY`;
  const tablesUrl = `${base}/tables?api_key=YOUR_API_KEY`;
  const nextPageUrl = `${base}/${firstAlias}/rows?api_key=YOUR_API_KEY&limit=500&offset=500`;

  return `# GRA-SmartHub - API Access Guide

You've been given access to this "${apiName}" API. No login needed — these are plain URLs, authenticated by the key below.

**API key:**
The API key will be shared separately in an encrypted format.

**Base URL:**
\`${base}\`

Keep the key private — treat it like a password. Send it as \`X-API-Key\` (preferred) or the existing \`api_key\` query parameter. Do not send conflicting keys. Wrong/missing key → \`401\`. If this API gets deactivated → \`403\`.

## Tables exposed on this API

| Alias | Source | Row limit/page |
|---|---|---|
${tableListMd}

Use the **alias** — not the real table/schema name — in every call below.

---

## 1. List available tables

\`\`\`
GET ${tablesUrl}
\`\`\`

Response:
\`\`\`json
{
  "api_id": "${apiId}",
  "tables": [
${tablesJsonExample}
  ]
}
\`\`\`

Use \`alias\` — that's the name you'll use in every other call, not the real table name.

---

## 2. Get columns for a table

\`\`\`
GET ${base}/${firstAlias}/columns?api_key=YOUR_API_KEY
\`\`\`

Response:
\`\`\`json
{ "alias": "${firstAlias}", "columns": [ { "name": "id", "type": "INTEGER" }, ... ] }
\`\`\`

\`404\` if that alias doesn't exist on this API.

---

## 3. Get row count

\`\`\`
GET ${countUrl}&search=optional_term&column=optional_column_name
\`\`\`

Response:
\`\`\`json
{ "alias": "${firstAlias}", "total_rows": 5000, "max_offset": 4999, "row_limit": ${first?.row_limit ?? 1000} }
\`\`\`

\`search\` (optional) filters rows before counting. By default it checks every column — slow on large or wide tables. Pass \`column\` alongside it to restrict the search to one column instead, which is much faster since only that column gets scanned.

---

## 4. Get rows (the actual data)

\`\`\`
GET ${rowsUrl}&search=optional_term&column=optional_column_name
\`\`\`

Params:
- \`limit\` — rows per page. Capped automatically to this table's configured row limit (\`${first?.row_limit ?? 1000}\`) and a hard max of 10,000, whichever is smaller.
- \`offset\` — where to start. Default 0.
- \`search\` — optional, filters rows. Checks every column by default.
- \`column\` — optional, used together with \`search\`. Restricts the search to just this one column instead of every column — much faster on large tables, since the rest of the columns don't get scanned. Must be a real column name on this table (see section 2), or you'll get a \`400\`.

Response:
\`\`\`json
{
  "alias": "${firstAlias}",
  "row_count": 500,
  "total_rows": 5000,
  "max_offset": 4999,
  "offset": 0,
  "limit": 500,
  "has_more": true,
  "next_offset": 500,
  "search": null,
  "column": null,
  "rows": [ { "id": 1, "...": "..." }, ... ]
}
\`\`\`

### Pagination

- A suitable unique sort key makes ordering deterministic on an unchanged source. Separate HTTP pages do not share a database snapshot; concurrent inserts, updates, or deletes can still affect an export.
- If \`has_more\` is \`true\`, call again with \`offset = next_offset\`.
- Keep going until \`has_more\` is \`false\`.
- \`total_rows\` / \`max_offset\` are only filled in on the very first page (\`offset=0\`); on later pages they may be \`null\` — that's normal, just keep using \`has_more\`/\`next_offset\` to know when to stop.
- There's no limit on how big the table can be overall — only on how many rows come back per page.

### Search

Add \`search=term\` to \`count\` or \`rows\` to filter rows. Same param, same behavior in both endpoints — count it first if you want to know how many pages to expect, or just page through \`rows\` until \`has_more\` is \`false\`.

By default \`search\` matches across every column, which means every column gets cast and scanned — on large tables (millions/billions of rows, or tables with big text columns) this can be slow. If you know which column has what you're looking for, add \`column=column_name\` alongside \`search\` to check only that column. Much faster, and works the same way in both \`count\` and \`rows\`.

Narrowing the searched columns can reduce work. Index use depends on the predicate, indexes, data distribution, and query plan; some databases can combine indexes for OR conditions.

---

## Going faster (all optional)

Everything above this line is the contract and isn't changing. Nothing in this section is required, and none of it alters a field you're already reading — if you ignore all of it, your integration behaves exactly as documented above.

### \`columns=col_a,col_b\` — ask for fewer columns

\`\`\`
GET ${rowsUrl}&columns=col_a,col_b
\`\`\`

Usually the single biggest win. A table holding embeddings, documents or blobs carries far more bytes in those columns than in everything else combined, and every one of them has to be read from disk, serialized and pushed over the wire before you see a row. Name only what you use and the rest never leaves the database.

Asking for a column this API doesn't expose is a \`400\`, not a silent drop — so you find out immediately rather than building on a response that quietly lost a field.

### \`cursor\` — seek to the next page instead of skipping to it

Every response already carries \`next_cursor\` alongside \`next_offset\`. Pass it back in place of an offset:

\`\`\`
GET ${base}/${firstAlias}/rows?api_key=YOUR_API_KEY&limit=500&cursor=CURSOR_FROM_LAST_RESPONSE
\`\`\`

Same response shape. Offset pagination can scan and discard preceding rows. Cursor pagination can instead seek through a suitable unique index, reducing deep-page work. Actual latency still depends on the plan, filters, cache, data, and concurrent load.

Treat the cursor as opaque — it's a position in the table's sort key, not a row number, and not something to build or edit by hand.

\`next_cursor\` is \`null\` on the last page, and on any request where seeking isn't available: the source has no unique key to seek on, or you're searching, or you set \`sort_by\`. Those page with \`next_offset\` exactly as before. Sending a \`cursor\` anyway on one of those requests is a \`400\` telling you which of them applied — it is not silently ignored, so a loop can't quietly restart at page one without you noticing.

### \`where=\` — filter on any column

\`\`\`
GET ${base}/${firstAlias}/rows?api_key=YOUR_API_KEY&where=status:eq:approved&where=created_at:gte:2026-01-01
\`\`\`

Each filter is \`column:op:value\`. Repeat the parameter for more than one; they are AND'd together. The value is everything after the second colon, so a timestamp like \`2026-08-15T10:00:00\` works as-is.

| operator | meaning | value |
| --- | --- | --- |
| \`eq\` \`ne\` | equals, does not equal | one |
| \`gt\` \`gte\` \`lt\` \`lte\` | greater / less than | one |
| \`in\` \`not_in\` | one of, not one of | comma-separated |
| \`is_null\` \`not_null\` | empty, not empty | none |
| \`contains\` | text appears anywhere in the value | one |
| \`starts_with\` | value begins with the text | one |

Up to 10 filters per request, and up to 100 values in an \`in\` list. The column must be one this API exposes, the operator must be from the list above, and anything else comes back as a \`400\` explaining what was wrong.

Filters work on their own or alongside \`search\`. They also work with \`columns=\`, \`cursor=\` and \`count=\`.

**On speed.** A filter is as fast as the column it touches. An indexed, selective column — an id, a timestamp, a title — is answered from the index. A column with only a handful of distinct values across millions of rows can't be, no matter what: an index cannot help eliminate a third of a table, so that filter reads rows. The useful pattern is to lead with something selective and let the rest refine it: \`where=doc_id:eq:X&where=chunk_index:gt:300\` narrows to one document from the index and filters what remains, which is why it returns in well under a millisecond.

### \`mode=exact\` — look up a known value

\`\`\`
GET ${base}/${firstAlias}/rows?api_key=YOUR_API_KEY&search=THE_ID&column=id_column&mode=exact
\`\`\`

The value must **equal** the column rather than appear somewhere inside it. Use this whenever you know exactly what you are looking for — an id, a file name, a status.

Equality is the right semantic operation for a known ID and can use a suitable B-tree index. A leading-wildcard substring query generally needs a different indexing strategy or a scan. No latency figure is guaranteed by this guide; verify the actual execution plan and concurrent-load measurements.

It is also more correct. A substring match on an id will also return rows whose id merely *contains* the one you asked for; an exact match will not.

Needs \`column\`. Cheap enough that you can leave ordering alone — no need for \`sort=none\`.

### \`mode=words\` — match whole words

\`\`\`
GET ${base}/${firstAlias}/rows?api_key=YOUR_API_KEY&search=SOME+WORDS&column=text_column&mode=words&sort=none
\`\`\`

Full-text search rather than a literal substring. Three differences worth knowing:

- Whole words only. Searching \`Bern\` stops matching \`Berlin\`.
- Several words means **all of them appear somewhere** in the value, in any order — not that exact sequence of characters. A substring search for \`Amsterdam Berlin Bern\` looks for that precise 26-character string, spacing included, and usually finds nothing.
- It can use a text-search index where a substring match cannot.

Needs \`column\`, and is only fast where that column has a text-search index. Ask whoever runs the API which columns do.

### \`sort=none\` — skip the ordering on a search

\`\`\`
GET ${base}/${firstAlias}/rows?api_key=YOUR_API_KEY&search=TERM&column=text_column&sort=none
\`\`\`

The single biggest lever on how long a broad search takes. Ordered, the source has to find **every** matching row before it can say which ones sort first; unordered, it stops as soon as it has a page. Measured on the same table: **12.5 seconds** ordered, **171 ms** unordered, for the same rows.

The trade is real: row order across pages is no longer guaranteed, so paging deep into a search could in principle show a row twice or miss one. Fine for "show me matches" or a bounded extract; not what you want for an exhaustive page-by-page export. Only applies to a search — the default \`sort=key\` is unchanged.

### \`sort_by=\` — order by a column

\`\`\`
GET ${base}/${firstAlias}/rows?api_key=YOUR_API_KEY&sort_by=created_at&sort_dir=desc
\`\`\`

Sorts the whole result on the source, not just the page. \`sort_dir\` is \`asc\` (default) or \`desc\`. The table's key is used as a tie-breaker so pages don't overlap. \`/columns\` lists which columns can be sorted in \`sortable_columns\`. No \`next_cursor\` while this is set — page with \`offset\`.

### \`count=false\` — skip the first page's count

\`total_rows\` on page one comes from a real \`COUNT(*)\`, which reads the whole table. If you don't use that number, \`count=false\` leaves it \`null\` and skips the scan entirely. \`has_more\` still tells you when to stop.

### \`estimate=true\` on \`count\`

Returns a database catalog estimate where available. Estimates can be stale or substantially inaccurate, even when no write is happening at the instant of the request. The response adds \`estimated: true\`; do not treat it as an exact count.

### Putting it together

For a large table, the fast version of a full extract is: name the columns you need, follow \`next_cursor\`, and count once at the start (or not at all).

\`\`\`
GET ${base}/${firstAlias}/rows?api_key=YOUR_API_KEY&limit=1000&columns=col_a,col_b&count=false
→ follow next_cursor until has_more is false
\`\`\`

To fetch everything belonging to one record:

\`\`\`
GET ${base}/${firstAlias}/rows?api_key=YOUR_API_KEY&search=THE_ID&column=id_column&mode=exact&columns=col_a,col_b
\`\`\`

To narrow by column values:

\`\`\`
GET ${base}/${firstAlias}/rows?api_key=YOUR_API_KEY&where=id_column:eq:THE_ID&where=created_at:gte:2026-01-01
\`\`\`

To find records mentioning something, quickly:

\`\`\`
GET ${base}/${firstAlias}/rows?api_key=YOUR_API_KEY&search=TERM&column=text_column&mode=words&sort=none&count=false
\`\`\`

### What each option needs

| option | needs | changes results? |
| --- | --- | --- |
| \`columns=\` | — | fewer fields per row |
| \`cursor=\` | — | no |
| \`count=false\` | — | \`total_rows\` is \`null\` |
| \`estimate=true\` | — | count is approximate |
| \`mode=exact\` | \`search\` + \`column\` | yes — equality, not substring |
| \`mode=words\` | \`search\` + \`column\` | yes — whole words, not substring |
| \`sort=none\` | \`search\` | order no longer guaranteed |
| \`where=\` | — | fewer rows, per your conditions |

Anything invalid — an option without what it needs, an unknown column, a bad value — comes back as a \`400\` saying what was wrong. Nothing is silently ignored.

### Two extra fields in every response

\`elapsed_ms\` is how long the source took, server-side. Compare it against your own round-trip time: if the two are close the query is the cost, and if they are far apart you are waiting on the size of the response, which \`columns=\` fixes.

\`total_is_estimate\` is \`true\` when \`total_rows\` came from the database's own estimate rather than a real count. Both are additions — nothing that was in the response before has moved or changed.

---

## Vector similarity search (PostgreSQL only)

This is an additional POST operation. Existing GET endpoints, response fields,
column exposure rules and pagination are unchanged. It searches stored embeddings;
it does not generate embeddings, write rows or create indexes.

Check \`vector_search\` and \`vector_columns\` on the existing \`/columns\` response.
Only exposed pgvector \`vector\`, \`halfvec\` and \`sparsevec\` columns are eligible.
\`dimensions: null\` means the column permits variable dimensions, not zero dimensions.

\`\`\`http
POST ${base}/${firstAlias}/vector-search
Content-Type: application/json
X-API-Key: YOUR_API_KEY
\`\`\`

Example body (names and three dimensions are illustrative; use your real metadata):

\`\`\`json
{
  "vector_column": "embedding",
  "query_vector": [0.12, -0.34, 0.56],
  "top_k": 10,
  "metric": "cosine",
  "search_mode": "auto",
  "columns": ["id", "title"],
  "where": ["status:eq:approved"]
}
\`\`\`

The existing \`?api_key=\` form also works. Prefer the header on this new endpoint
so the key is not placed in the request URL. If both are supplied, they must match.

The query must come from the compatible embedding model/setup used for the stored
column. Matching dimensions alone does not establish model compatibility. Submit
only finite JSON numbers: no strings, booleans, nulls or nested arrays. This endpoint
accepts 1 to 16,000 dimensions, including dense arrays used to query sparse columns.
Values must fit the target float precision. Cosine rejects queries that round to zero.

\`top_k\` defaults to 10; the server default maximum is 100 (configurable up to 1,000). Read \`vector_max_top_k\` from /columns. It is capped further by the alias's
row limit. It is a nearest-match limit, not an export page size: this endpoint has
no offset, cursor or table-wide count. Equal-distance ties have no fixed order.
\`where\` uses the same exposed-column filter syntax and operators as the GET APIs.
Unknown JSON fields are rejected rather than ignored.

\`columns\` is optional. Omitted or empty returns exposed non-bulk fields only.
Explicitly name an exposed embedding column to include its raw value. This default
applies ONLY to vector search; GET \`/rows\` still returns its usual exposed columns.
Results nest source fields under \`row\` so distance/rank cannot overwrite source columns.

\`\`\`json
{
  "alias": "${firstAlias}",
  "vector_column": "embedding",
  "dimensions": 3,
  "metric": "cosine",
  "search_mode": "auto",
  "requested_top_k": 10,
  "top_k": 10,
  "row_count": 1,
  "columns": ["id", "title"],
  "matches": [
    {"rank": 1, "distance": 0.08, "similarity": 0.92, "row": {"id": 42, "title": "Example"}}
  ],
  "elapsed_ms": 12,
  "warnings": ["The source may use an approximate index. Recall is not guaranteed; filters can yield fewer than top_k results. Use search_mode=exact to compare."]
}
\`\`\`

Distances sort ascending. For \`cosine\`, similarity = 1 - distance (not a percentage
or confidence). For \`inner_product\`, distance is the negative dot product and
similarity = -distance. For \`l2\`, only distance is returned; no invented similarity.
Null vectors are excluded. Cosine also excludes zero stored vectors. Variable-dimension
columns search only rows matching the query length, with a guarded distance expression.

\`search_mode=auto\` (default) lets PostgreSQL choose its plan, including existing
HNSW/IVFFlat indexes. Recall is not guaranteed and filters can yield fewer than Top K
results. \`search_mode=exact\` disables the ANN distance-ordering path for this query
without altering the pooled connection's session settings. It may scan and sort many
rows. This endpoint does not create indexes. It applies configured query-local HNSW/IVFFlat settings, and enables iterative scans on pgvector 0.8.0+. Those settings reset at transaction end. It never silently retries with an expensive exact scan.

New endpoint errors: 400 for invalid vectors/columns/filters or unsupported sources;
401 for absent/invalid keys; 403 for deactivated APIs; 404 for missing API/alias;
422 for invalid request JSON/schema; 504 for source timeout; 502 for source failures.
Handler-level calls/errors use the existing activity log. Framework-level JSON/schema
rejections happen before the handler and follow the existing FastAPI behavior.

Example: save a complete real query as \`request.json\`, then call:

\`\`\`bash
curl -X POST "${base}/${firstAlias}/vector-search" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  --data-binary @request.json
\`\`\`

---

### Production response semantics

The new response includes \`recall_guaranteed\`: true only in Exact mode, for the eligible rows at that query's database snapshot and the stored numeric precision. Auto is not an exact-recall guarantee even if results are sorted by distance. An eligible index is not proof the planner used it; inspect a representative EXPLAIN plan in staging.

\`top_k_filled=false\` in Auto does not prove the database contains fewer matches. Approximate scan limits and filters can reduce the returned set. Exact mode scans the eligible set without ANN ordering; a known unique key breaks equal-distance ties.

Auto requires a valid full-column HNSW or IVFFlat index for the selected metric by default. A missing index returns 409, not an unannounced full scan. Partial/expression indexes need a separate reviewed query path; variable-dimension columns use Exact mode.

The new endpoint encodes SQL Decimal values as strings to avoid numeric precision loss. Existing /rows serialization is unchanged. Request bodies and final serialized responses have byte budgets; excessive payloads return 413 without truncating rows. Use narrow projections. Very large individual source fields still require source-side size controls.

Null vectors are excluded; cosine also excludes zero vectors. These are distance/similarity results, not calibrated confidence probabilities. Matching dimensions does not verify matching embedding models; callers must use the same model/version and preprocessing as the stored embeddings.

429/503 responses include Retry-After. Retry read requests with bounded exponential backoff and jitter; do not retry 400/401/403/409/413/422 unchanged. A 504 is a database timeout, not an empty answer.

### Text queries

If \`/columns\` lists the embedding column under \`text_query_columns\`, send
\`query_text\` instead of \`query_vector\` and the server embeds it with the model
that built that column. Send one or the other, not both. The response adds
\`query_source\` (\`text\` or \`vector\`) and \`embedding_model\`. Query text is not
stored or logged. If no model is registered for the column, the call returns 409;
\`query_vector\` keeps working exactly as before.

\`\`\`json
{ "vector_column": "embedding", "query_text": "sterility failures in aseptic filling", "top_k": 10 }
\`\`\`

Response (abridged, illustrative values; same shape as a \`query_vector\` search, plus two fields):

\`\`\`json
{
  "vector_column": "embedding",
  "dimensions": 1536,
  "metric": "cosine",
  "search_mode": "auto",
  "matches": [
    { "rank": 1, "distance": 0.1832, "similarity": 0.8168,
      "row": { "chunk_id": "4db8fd65-e7ae-597f-a2b4-66003a4c07a5", "chunk_text": "Sterility failure observed in the aseptic filling line..." } }
  ],
  "query_source": "text",
  "embedding_model": "rimdocs-embed"
}
\`\`\`

---

## Semantic + keyword retrieval (\`/retrieve\`)

Available when \`/columns\` returns a non-null \`retrieval\` object. Send plain-language
questions; the server embeds them, runs a semantic search and a keyword (full-text)
search, and merges both with Reciprocal Rank Fusion. Batch up to
\`retrieval.max_queries\` questions in one call.

\`\`\`http
POST ${base}/${firstAlias}/retrieve
Content-Type: application/json
X-API-Key: YOUR_API_KEY
\`\`\`

\`\`\`json
{
  "retrieval_queries": [
    { "query": "sterility failures in aseptic filling", "k": 10, "where": ["region:eq:US"] },
    { "query": "container closure integrity", "k": 5, "mode": "dense" }
  ],
  "search_mode": "auto"
}
\`\`\`

\`mode\` per query: \`hybrid\` (both searches, the default where available), \`dense\`
(semantic only) or \`sparse\` (keywords only); \`retrieval.modes\` lists what this alias
supports. \`where\` uses the same filter syntax as the GET APIs. \`search_mode\` applies to
the semantic search exactly as on \`/vector-search\`.

The response has one \`results\` entry per query, in order: \`{ "k": 10, "matches": [...] }\`.
Some tables publish a fixed response contract (nested metadata objects, and possibly a
different name than \`matches\`); \`retrieval.response_contract\` says whether one applies.
Every match carries:

| Field | Meaning |
|---|---|
| \`rank\` | 1-based position after fusion |
| \`score\` | fused RRF score, higher is better; this is what orders the results |
| \`dense_score\` | semantic similarity (cosine: 1 - distance), or \`null\` if the semantic search did not return the item |
| \`sparse_score\` | keyword relevance (\`ts_rank_cd\`, 0..1), or \`null\` if the keyword search did not return the item |

Scores are for ordering and debugging, not calibrated confidence. Contract fields are
always present; missing values are \`null\`. If keyword search exceeds its time budget,
the call still succeeds with semantic results only and says so in \`warnings\`.
Decimal values are encoded as strings.

Response (one query, abridged to its first match; values are from a demo run):

\`\`\`json
{
  "alias": "${firstAlias}",
  "results": [
    {
      "k": 5,
      "matches": [
        {
          "rank": 1,
          "score": 0.0325,
          "dense_score": 0.5345,
          "sparse_score": 0.0244,
          "chunk_id": "4db8fd65-e7ae-597f-a2b4-66003a4c07a5",
          "chunk_source_id": "src-001",
          "chunk_number": 1,
          "page_number": 1,
          "chunk_text": "Application BLA 761201. Sterility failure observed in the aseptic filling line...",
          "document_id": "DOC-0001",
          "rimdocs_metadata": { "object_name": "Sterility assurance report 001", "jj_region_country": "US",
                                "a_status": "Draft", "r_version_label": "1.0", "approved_date": "2025-01-01" },
          "document_metadata": { "indication": null, "application_number": "BLA 761201",
                                 "title": "Sterility assurance report (US)" }
        }
      ]
    }
  ],
  "embedding_model": "rimdocs-embed",
  "fusion": { "method": "reciprocal_rank_fusion", "rrf_k": 60, "dense_weight": 1.0, "sparse_weight": 1.0 },
  "elapsed_ms": 6,
  "warnings": ["Semantic results may come from an approximate index; recall is not guaranteed. Use search_mode=exact to compare."],
  "decimal_encoding": "string"
}
\`\`\`

The fields inside each match depend on the table's response contract; tables without one return \`rank\`, \`score\`,
\`dense_score\`, \`sparse_score\` and a \`row\` object instead.

Python client example:

\`\`\`python
import requests

resp = requests.post(
    "${base}/${firstAlias}/retrieve",
    headers={"X-API-Key": "YOUR_API_KEY"},
    json={"retrieval_queries": [
        {"query": "sterility failures in aseptic filling", "k": 5},
        {"query": "BLA 761207", "k": 3, "mode": "sparse"},
    ]},
    timeout=30,
)
if resp.status_code in (429, 503):
    retry_after = int(resp.headers.get("Retry-After", "2"))  # back off, then retry
resp.raise_for_status()
for i, result in enumerate(resp.json()["results"]):
    for m in result["matches"]:
        print(i, m["rank"], round(m["score"], 4), m.get("chunk_text", "")[:80])
\`\`\`

Errors specific to \`/retrieve\`: 409 when retrieval is not configured for the table or a
required index is missing (a DBA or administrator must act; retrying will not help);
503 with Retry-After when the embedding service is temporarily unavailable.

\`\`\`bash
curl -X POST "${base}/${firstAlias}/retrieve" \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: YOUR_API_KEY" \\
  -d '{"retrieval_queries":[{"query":"sterility failures","k":5}]}'
\`\`\`

---

## AI agents (MCP)

If your administrator has enabled it, this API is also available as a read-only
Model Context Protocol server for AI assistants and agents:

\`\`\`
URL:    ${API_BASE_URL}/data-api/mcp/${apiId}
Header: Authorization: Bearer YOUR_API_KEY   (or X-API-Key: YOUR_API_KEY)
\`\`\`

Tools: \`list_tables\`, \`describe_table\`, \`search\` (the same retrieval as \`/retrieve\`).
Calls use this API's key, rate limits and activity log. Long text fields are truncated
for the model's context and embeddings are never returned. Example client entry:

\`\`\`json
{ "mcpServers": { "smarthub-data-api": {
    "type": "http",
    "url": "${API_BASE_URL}/data-api/mcp/${apiId}",
    "headers": { "Authorization": "Bearer YOUR_API_KEY" } } } }
\`\`\`


## Errors

| Status | Meaning |
|---|---|
| 400 | something in the request was invalid — an unknown column, a bad filter, an option without what it needs. The response body says which |
| 401 | api_key missing or wrong |
| 403 | this API has been deactivated — contact whoever gave you the key |
| 404 | alias doesn't exist on this API |
| 409 | the server isn't set up for this request (missing index, or search not configured for the table) — needs an administrator, not a retry |
| 429 | too many calls too quickly — wait for the \`Retry-After\` header's seconds, then continue |
| 502 | source system couldn't be reached — retry later |
| 503 | busy, or the embedding service is temporarily unavailable — wait for \`Retry-After\`, then retry |
| 504 | the query ran past the time limit and was stopped — see below |

### On \`504\`

This is the one worth writing code for. It means the query was still running after 60 seconds and the source cancelled it — most often an unfiltered \`search\`, which has to read every row of every searchable column before it can answer.

Retrying the identical request will time out again. Make the query cheaper instead:

- add \`column=\` so the search hits one column and an index can serve it
- use \`mode=exact\` if you know the exact value
- add \`sort=none\` on a broad search
- ask for fewer \`columns=\`
- narrow with \`where=\` first

### On \`429\`

Limits are deployment-configurable. The rate budget is per API, a fixed window shared by every server process. A separate per-key concurrent-request limit also returns 429. The response carries \`Retry-After\` in seconds — wait that long rather than retrying immediately, or you will simply be refused again.

If you are hitting this during a bulk extract, the fix is bigger pages, not more requests: one call at \`limit=1000\` costs you one call, ten calls at \`limit=100\` cost you ten for the same rows.

---

## Quick example (curl)

\`\`\`bash
curl "${tablesUrl}"
curl "${columnsUrl}"
curl "${rowsUrl}"
curl "${nextPageUrl}"
\`\`\`

Four existing GET endpoints, plus optional vector-search and retrieve POSTs for PostgreSQL aliases.
`;
}