from __future__ import annotations

import json
import logging
import statistics
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import db
from audit import write_audit_event
from auth import require_permission
from rag import RAGQueryRequest, _actor, _ip, _loads, get_profile, run_rag

logger = logging.getLogger(__name__)
rag_evaluation_router = APIRouter(prefix="/rag/evaluation", tags=["Agentic RAG Evaluation"])


class EvalCase(BaseModel):
    case_id: str
    question: str
    reference_answer: Optional[str] = None
    reference_evidence_ids: List[str] = Field(default_factory=list)
    reference_source_ids: List[str] = Field(default_factory=list)
    expected_tool_calls: List[str] = Field(default_factory=list)
    forbidden_tool_calls: List[str] = Field(default_factory=list)
    expected_abstention: Optional[bool] = None
    tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DatasetBody(BaseModel):
    name: str
    description: str = ""
    cases: List[EvalCase]


class RunBody(BaseModel):
    dataset_id: str
    profile_id: str
    baseline_run_id: Optional[str] = None


def _dataset(row):
    return {"id": row["id"], "name": row["name"], "version": int(row["version"]),
            "description": row["description"] or "", "cases": _loads(row["cases"], []),
            "created_by": row["created_by"], "created_at": row["created_at"]}


def _run(row):
    return {"id": row["id"], "dataset_id": row["dataset_id"], "profile_id": row["profile_id"],
            "status": row["status"], "config_snapshot": _loads(row["config_snapshot"], {}),
            "summary": _loads(row["summary"], {}), "created_by": row["created_by"],
            "started_at": row["started_at"], "completed_at": row["completed_at"]}


def _tokens(text):
    import re
    return {x for x in re.findall(r"[A-Za-z0-9]+", (text or "").lower()) if len(x) > 1}


def rank_metrics(retrieved_ids, references):
    refs = set(references or [])
    if not refs:
        return {}
    hits = [i for i, eid in enumerate(retrieved_ids, 1) if eid in refs]
    return {"retrieval_recall": len(hits) / len(refs),
            "retrieval_precision": len(hits) / len(retrieved_ids) if retrieved_ids else 0.0,
            "hit_at_k": 1.0 if hits else 0.0, "mrr": 1.0 / hits[0] if hits else 0.0}


def case_metrics(case, result):
    evidence = result.get("evidence") or []
    evidence_ids = [e.get("evidence_id") for e in evidence if e.get("evidence_id")]
    source_ids = [e.get("source_id") for e in evidence if e.get("source_id")]
    metrics = rank_metrics(evidence_ids, case.get("reference_evidence_ids") or [])
    refs = set(case.get("reference_source_ids") or [])
    if refs:
        metrics["source_recall"] = len(refs & set(source_ids)) / len(refs)

    reference = case.get("reference_answer")
    if reference:
        ref_tokens = _tokens(reference)
        metrics["reference_term_recall"] = len(_tokens(result.get("answer")) & ref_tokens) / len(ref_tokens) if ref_tokens else 0.0

    citations = result.get("citations") or []
    valid_ids = set(evidence_ids)
    cited = [c.get("evidence_id") for c in citations]
    metrics["citation_validity"] = (
        sum(x in valid_ids for x in cited) / len(cited) if cited else
        (1.0 if result.get("insufficient_evidence") else 0.0)
    )
    verification = result.get("verification") or {}
    claims = verification.get("claims") or []
    if claims:
        metrics["claim_support_rate"] = sum(c.get("status") == "supported" for c in claims) / len(claims)
        metrics["unsupported_claim_rate"] = sum(c.get("status") == "unsupported" for c in claims) / len(claims)
    else:
        metrics["claim_support_rate"] = 1.0 if result.get("insufficient_evidence") else 0.0
        metrics["unsupported_claim_rate"] = 0.0

    tools = [x.get("tool") for x in ((result.get("diagnostics") or {}).get("stages") or {}).get("tool_calls", [])
             if isinstance(x, dict)]
    expected, forbidden = set(case.get("expected_tool_calls") or []), set(case.get("forbidden_tool_calls") or [])
    if expected:
        metrics["expected_tool_recall"] = len(expected & set(tools)) / len(expected)
    if forbidden:
        metrics["forbidden_tool_avoidance"] = 1.0 if not (forbidden & set(tools)) else 0.0
    if case.get("expected_abstention") is not None:
        metrics["abstention_accuracy"] = (
            1.0 if bool(result.get("insufficient_evidence")) == bool(case["expected_abstention"]) else 0.0
        )
    metrics["verified"] = 1.0 if verification.get("state") == "verified" else 0.0
    metrics["latency_ms"] = float((result.get("diagnostics") or {}).get("latency_ms") or 0)
    return metrics


def summarize(results):
    numeric = {}
    for item in results:
        for key, value in (item.get("metrics") or {}).items():
            if isinstance(value, (int, float)):
                numeric.setdefault(key, []).append(float(value))
    output = {}
    for key, values in numeric.items():
        ordered = sorted(values)
        p95 = ordered[min(len(ordered) - 1, max(0, round(.95 * (len(ordered) - 1))))]
        output[key] = {"mean": sum(values) / len(values), "median": statistics.median(values),
                       "p95": p95, "min": min(values), "max": max(values)}
    return {"cases": len(results), "failures": sum(bool(x.get("error")) for x in results), "metrics": output}


def ragas_available():
    try:
        import ragas  # noqa: F401
        import datasets  # noqa: F401
        return True
    except Exception:
        return False


def ragas_metrics(case, result):
    if not ragas_available():
        return {}
    try:
        from datasets import Dataset
        from ragas import evaluate
        row = {"question": case["question"], "answer": result.get("answer") or "",
               "contexts": [e.get("text") or "" for e in result.get("evidence") or []]}
        if case.get("reference_answer"):
            row["ground_truth"] = case["reference_answer"]
        frame = evaluate(Dataset.from_list([row])).to_pandas()
        return {f"ragas_{k}": float(v) for k, v in frame.iloc[0].to_dict().items()
                if isinstance(v, (int, float))}
    except Exception as exc:
        logger.warning("Optional Ragas adapter failed (%s)", type(exc).__name__)
        return {}


@rag_evaluation_router.get("/capabilities")
def capabilities(auth: dict = Depends(require_permission("viewRimDocsAgentic"))):
    return {"deterministic": ["retrieval_recall", "retrieval_precision", "hit_at_k", "mrr",
                              "citation_validity", "claim_support_rate", "unsupported_claim_rate",
                              "source_recall", "reference_term_recall", "abstention_accuracy",
                              "expected_tool_recall", "forbidden_tool_avoidance", "verified", "latency_ms"],
            "ragas": {"available": ragas_available(), "optional": True}}


@rag_evaluation_router.get("/datasets")
def list_datasets(auth: dict = Depends(require_permission("viewRimDocsAgentic"))):
    with db.transaction(read_only=True) as conn:
        rows = conn.execute("SELECT * FROM rag_eval_datasets ORDER BY name,version DESC").fetchall()
    return {"datasets": [_dataset(r) for r in rows]}


@rag_evaluation_router.post("/datasets")
def create_dataset(body: DatasetBody, request: Request,
                   auth: dict = Depends(require_permission("manageDataAPIExports"))):
    name = body.name.strip()
    ids = [c.case_id.strip() for c in body.cases]
    if not name or not body.cases or any(not x for x in ids) or len(set(ids)) != len(ids):
        raise HTTPException(400, "Dataset needs a name and unique non-empty case IDs.")
    actor = _actor(auth)
    with db.transaction() as conn:
        latest = conn.execute("SELECT MAX(version) AS v FROM rag_eval_datasets WHERE name=?", (name,)).fetchone()
        version, dataset_id = int(latest["v"] or 0) + 1, str(uuid.uuid4())
        values = [c.model_dump() if hasattr(c, "model_dump") else c.dict() for c in body.cases]
        conn.execute("INSERT INTO rag_eval_datasets (id,name,version,description,cases,created_by,created_at) VALUES (?,?,?,?,?,?,now())",
                     (dataset_id, name[:120], version, body.description.strip()[:1000], json.dumps(values), actor))
    write_audit_event("rag_eval_dataset_created", actor, f"{name} v{version}", f"cases={len(body.cases)}", _ip(request))
    return {"id": dataset_id, "version": version}


@rag_evaluation_router.get("/runs")
def list_runs(auth: dict = Depends(require_permission("viewRimDocsAgentic"))):
    with db.transaction(read_only=True) as conn:
        rows = conn.execute("SELECT * FROM rag_eval_runs ORDER BY started_at DESC LIMIT 100").fetchall()
    return {"runs": [_run(r) for r in rows]}


@rag_evaluation_router.get("/runs/{run_id}")
def get_run(run_id: str, auth: dict = Depends(require_permission("viewRimDocsAgentic"))):
    with db.transaction(read_only=True) as conn:
        row = conn.execute("SELECT * FROM rag_eval_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, "No such evaluation run.")
        cases = conn.execute("SELECT * FROM rag_eval_case_results WHERE run_id=? ORDER BY case_id", (run_id,)).fetchall()
    return {"run": _run(row), "cases": [
        {"id": c["id"], "case_id": c["case_id"], "question": c["question"], "response": c["response"],
         "citations": _loads(c["citations"], []), "retrieved_evidence": _loads(c["retrieved_evidence"], []),
         "metrics": _loads(c["metrics"], {}), "latency_ms": c["latency_ms"], "error": c["error"] or ""}
        for c in cases]}


@rag_evaluation_router.post("/runs")
def run_evaluation(body: RunBody, request: Request,
                   auth: dict = Depends(require_permission("viewRimDocsAgentic"))):
    with db.transaction(read_only=True) as conn:
        row = conn.execute("SELECT * FROM rag_eval_datasets WHERE id=?", (body.dataset_id,)).fetchone()
    if not row:
        raise HTTPException(404, "No such evaluation dataset.")
    dataset, profile = _dataset(row), get_profile(body.profile_id)
    actor, run_id = _actor(auth), str(uuid.uuid4())
    snapshot = {"profile": profile, "dataset": {"id": dataset["id"], "name": dataset["name"], "version": dataset["version"]}}
    with db.transaction(autocommit=True) as conn:
        conn.execute("INSERT INTO rag_eval_runs (id,dataset_id,profile_id,status,config_snapshot,summary,created_by,started_at) VALUES (?,?,?,?,?,?,?,now())",
                     (run_id, dataset["id"], profile["id"], "running", json.dumps(snapshot), json.dumps({}), actor))

    results = []
    try:
        for case in dataset["cases"]:
            start, error, result, metrics = time.monotonic(), "", {}, {}
            try:
                result = run_rag(RAGQueryRequest(query=case["question"], profile_id=profile["id"]),
                                 auth, request_id=f"eval-{run_id[:8]}-{case['case_id']}")
                metrics = case_metrics(case, result)
                metrics.update(ragas_metrics(case, result))
            except Exception as exc:
                logger.warning("Evaluation case %s failed (%s)", case.get("case_id"), type(exc).__name__)
                error = type(exc).__name__
            latency = int((time.monotonic() - start) * 1000)
            metrics.setdefault("latency_ms", float(latency))
            item = {"case_id": case["case_id"], "question": case["question"],
                    "response": result.get("answer") or "", "citations": result.get("citations") or [],
                    "evidence": result.get("evidence") or [], "metrics": metrics, "latency_ms": latency, "error": error}
            results.append(item)
            with db.transaction(autocommit=True) as conn:
                conn.execute("INSERT INTO rag_eval_case_results (id,run_id,case_id,question,response,citations,retrieved_evidence,metrics,latency_ms,error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (str(uuid.uuid4()), run_id, item["case_id"], item["question"], item["response"],
                              json.dumps(item["citations"]), json.dumps(item["evidence"]), json.dumps(metrics), latency, error))
        summary = summarize(results)
        if body.baseline_run_id:
            try:
                baseline = get_run(body.baseline_run_id, auth)["run"]["summary"]
                summary["baseline"] = {"run_id": body.baseline_run_id, "mean_deltas": {
                    metric: stats["mean"] - float((baseline.get("metrics", {}).get(metric) or {}).get("mean"))
                    for metric, stats in summary["metrics"].items()
                    if isinstance((baseline.get("metrics", {}).get(metric) or {}).get("mean"), (int, float))
                }}
            except Exception:
                summary["baseline"] = {"run_id": body.baseline_run_id, "error": "baseline_unavailable"}
        with db.transaction(autocommit=True) as conn:
            conn.execute("UPDATE rag_eval_runs SET status='complete',summary=?,completed_at=now() WHERE id=?",
                         (json.dumps(summary), run_id))
    except Exception as exc:
        with db.transaction(autocommit=True) as conn:
            conn.execute("UPDATE rag_eval_runs SET status='failed',summary=?,completed_at=now() WHERE id=?",
                         (json.dumps({"error": type(exc).__name__}), run_id))
        raise

    write_audit_event("rag_eval_run_completed", actor, run_id,
                      f"dataset={dataset['name']} v{dataset['version']} cases={len(results)}", _ip(request))
    return {"run_id": run_id, "summary": summary}


@rag_evaluation_router.delete("/datasets/{dataset_id}")
def delete_dataset(dataset_id: str, request: Request,
                   auth: dict = Depends(require_permission("manageDataAPIExports"))):
    actor = _actor(auth)
    with db.transaction() as conn:
        row = conn.execute("SELECT name,version FROM rag_eval_datasets WHERE id=?", (dataset_id,)).fetchone()
        if not row:
            raise HTTPException(404, "No such evaluation dataset.")
        if conn.execute("SELECT 1 FROM rag_eval_runs WHERE dataset_id=? LIMIT 1", (dataset_id,)).fetchone():
            raise HTTPException(409, "Historical runs use this dataset version.")
        conn.execute("DELETE FROM rag_eval_datasets WHERE id=?", (dataset_id,))
    write_audit_event("rag_eval_dataset_deleted", actor, f"{row['name']} v{row['version']}", "", _ip(request))
    return {"deleted": True}
