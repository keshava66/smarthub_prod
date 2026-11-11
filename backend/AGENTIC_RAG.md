# SmartHub Enterprise Agentic RAG

This feature is implemented as an additive SmartHub capability. It does not replace the
existing FastAPI application, Data API Export, connector framework, authentication,
RBAC, audit logging, or the legacy RIMDocs/EPOD RAG tabs.

## Runtime architecture

```
React /rag?tab=rimdocs-agentic
    |
    | existing MSAL apiFetch/apiEventSource
    v
FastAPI /rag
    |
    +-- profile/source configuration (smarthub_core)
    +-- bounded query rewrite/decomposition/source routing
    +-- Data API native retrieval (existing /retrieve internals)
    +-- Data API live retrieval fallback
    +-- existing JDBC/database connector search
    +-- allowlisted external REST APIs
    +-- existing permission-gated SmartHub read-only tools
    +-- reciprocal-rank fusion
    +-- bounded reranking
    +-- structured grounded generation
    +-- deterministic citation validation
    +-- claim-level verification
    +-- one repair + re-verification
    +-- abstention when verification is not successful
```

## Migration

Run the normal SmartHub database migration process. The feature adds:

- `rag_sources`
- `rag_profiles`
- `rag_feedback`
- `rag_eval_datasets`
- `rag_eval_runs`
- `rag_eval_case_results`

Migration: `migrations/smarthub_core/V004__agentic_rag.sql`.

No existing Data API, assistant, RBAC, audit, or RAG tables are modified.

## First configuration

Users with `manageDataAPIExports` can open **RAG > RIMDocs Agentic > Configure**.

### Data API source

Recommended when the data has already been published through SmartHub Data API.

```json
{
  "api_id": "<data-api-id-or-slug>",
  "alias": "documents",
  "mode": "auto"
}
```

`mode=auto` uses the existing Data API semantic/hybrid retrieval profile when it is
enabled for the alias. If semantic retrieval is not enabled, the adapter uses the
existing connector for safe live text search.

Optional fields include `text_columns`, `columns`, `text_fields`,
`title_field`, `id_columns`, `retrieval_mode`, and `required_permission`.

### Existing JDBC/database connector source

This reuses `data_validation_connectors.py`; it does not create a second connection
framework.

```json
{
  "source_system": "sqlserver",
  "env": "prod",
  "database": "Regulatory",
  "schema": "dbo",
  "table": "Documents",
  "text_columns": ["TITLE", "BODY"],
  "columns": ["DOC_ID", "TITLE", "BODY", "STATUS"],
  "id_columns": ["DOC_ID"],
  "title_field": "TITLE"
}
```

The same pattern works for connectors already registered in SmartHub, including
PostgreSQL, Oracle/RIMDocs, Snowflake, Denodo JDBC, SQL Server/Grail JDBC, and
Databricks JDBC. Retrieval capabilities remain source-specific; a JDBC source is not
pretended to have pgvector semantics when it does not.

### External REST source

Outbound REST sources are disabled by default. Add hostnames to
`RAG_ALLOWED_API_HOSTS`.

```json
{
  "url": "https://approved-api.example.com/search",
  "method": "GET",
  "query_param": "q",
  "items_path": "results",
  "text_field": "content",
  "title_field": "title",
  "id_field": "id",
  "secret_env": "RAG_SECRET_APPROVED_API",
  "auth_header": "Authorization",
  "auth_prefix": "Bearer "
}
```

Secrets are never stored in RAG metadata. Only a `RAG_SECRET_*` environment-variable
name may be referenced.

### Profile

A profile controls sources and orchestration policy.

```json
{
  "default": true,
  "source_ids": ["<source-id>"],
  "source_only": true,
  "decompose": true,
  "route_sources": true,
  "rerank": true,
  "verify": true,
  "require_verification": true,
  "candidate_k": 20,
  "final_k": 10,
  "rerank_threshold": 2.0,
  "portal_tools": [],
  "max_tool_calls": 3,
  "repair_attempts": 1
}
```

Optional model keys are `generation_model`, `routing_model`,
`decomposition_model`, `rerank_model`, `verification_model`, or
`small_model`. They use the existing SmartHub Azure OpenAI endpoint/AAD/API-key
configuration.

## Grounding contract

Answers are generated as structured data containing claims and evidence IDs. The
backend then:

1. rejects citation IDs that were not supplied as evidence;
2. requires claim citations;
3. verifies each claim only against its cited evidence;
4. treats verifier failure as `unavailable`, never as successful verification;
5. allows at most one repair;
6. verifies the repaired result again;
7. abstains when a profile requires verification and verification is not `verified`.

Retrieved database text, REST responses, and portal tool output are untrusted data and
are explicitly separated from model instructions.

## Plugin/tool model

Profiles can enable existing SmartHub assistant data tools through `portal_tools`.
Only tools already present in the SmartHub tool registry and permitted for the current
user are exposed. Browser actions and preference-writing tools are excluded.

The existing optional Data API MCP endpoint remains unchanged and can continue serving
external agents. Native RAG calls Data API internals directly to avoid a second
authentication/rate-limit/retrieval implementation.

## Evaluation

Route: `/rag/evaluation`.

Evaluation datasets are immutable/versioned. Metrics always available include:

- retrieval recall / precision / hit@K / MRR when evidence labels are supplied;
- source recall;
- citation validity;
- claim support rate;
- unsupported claim rate;
- reference-term recall;
- expected/forbidden tool metrics;
- abstention accuracy;
- verification rate;
- latency.

Ragas is an optional adapter and is deliberately not part of the production
`requirements.txt`. Install `requirements-rag-eval.txt` in an evaluation
environment to enable it. If Ragas is absent or incompatible, deterministic SmartHub
metrics continue to work.

## Rollout / rollback

Native React is the default. During rollout only, set:

```
VITE_RAG_NATIVE_ENABLED=false
```

to render the previous Streamlit URL for the RIMDocs Agentic tab. This flag is a
temporary rollback mechanism; new development should target the native React/FastAPI
implementation.

## Security controls

- Existing MSAL authentication and backend RBAC.
- `viewRimDocsAgentic` for chat/evaluation access.
- `manageDataAPIExports` for RAG source/profile/dataset administration.
- Existing connector identifier validation.
- No unrestricted LLM-generated SQL.
- REST HTTPS + explicit host allowlist + redirect disabled + size/time limits.
- Secret references only; no secret values in metadata or frontend responses.
- Retrieved/tool content is untrusted.
- Citation IDs are application validated.
- Verification fails closed when required.
- Audit events for RAG queries, configuration changes, and evaluation runs.

## Validation

The feature branch contains `.github/workflows/agentic-rag-ci.yml`, which runs:

- Python compilation for `rag.py` and `rag_evaluation.py`;
- new grounding/security tests;
- the existing Data API retrieval unit suite;
- ESLint on changed React/routing files;
- full Vite production build.

Optional live model/database evaluation still requires deployment-specific credentials,
sources, and test data and therefore is not claimed by unit CI.
