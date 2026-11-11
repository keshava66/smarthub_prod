import os

import pytest
from fastapi import HTTPException

import rag


def evidence(eid, source="s1", score=None):
    return rag.Evidence(
        evidence_id=eid,
        source_id=source,
        source_name=source,
        source_type="data_api",
        title=eid,
        text=f"supporting text {eid}",
        retrieval_score=score,
    )


def test_rrf_deduplicates_and_rewards_cross_leg_hits():
    a, b, c = evidence("a"), evidence("b"), evidence("c")
    ranked = rag.reciprocal_rank_fusion([[a, b], [c, a]])
    assert [x.evidence_id for x in ranked][0] == "a"
    assert len({x.evidence_id for x in ranked}) == 3
    assert ranked[0].retrieval_score > ranked[-1].retrieval_score


def test_profile_defaults_to_source_only_verification():
    cfg = rag._validate_profile({"source_ids": ["source-1"]})
    assert cfg["source_only"] is True
    assert cfg["verify"] is True
    assert cfg["require_verification"] is True
    assert cfg["repair_attempts"] == 1
    assert cfg["final_k"] <= cfg["candidate_k"]


def test_profile_rejects_unknown_or_browser_tools():
    with pytest.raises(HTTPException):
        rag._validate_profile({"source_ids": ["source-1"], "portal_tools": ["navigate_to"]})
    with pytest.raises(HTTPException):
        rag._validate_profile({"source_ids": ["source-1"], "portal_tools": ["does_not_exist"]})


def test_connector_source_rejects_unsafe_identifier():
    with pytest.raises(HTTPException):
        rag._validate_source("connector", {
            "source_system": "postgresql",
            "table": "documents; DROP TABLE x",
            "text_columns": ["text"],
        })


def test_rest_source_requires_https_allowlisted_host(monkeypatch):
    monkeypatch.setenv("RAG_ALLOWED_API_HOSTS", "api.example.test")
    good = rag._validate_source("rest", {
        "url": "https://api.example.test/search",
        "method": "GET",
        "text_field": "text",
        "secret_env": "RAG_SECRET_TEST",
    })
    assert good["method"] == "GET"

    with pytest.raises(HTTPException):
        rag._validate_source("rest", {
            "url": "http://api.example.test/search",
            "method": "GET",
            "text_field": "text",
        })
    with pytest.raises(HTTPException):
        rag._validate_source("rest", {
            "url": "https://169.254.169.254/latest/meta-data",
            "method": "GET",
            "text_field": "text",
        })


def test_generation_rejects_fabricated_citation():
    ev = evidence("ev_good")
    generated, errors = rag._validate_generated({
        "answer": "Claim [cite:ev_fake]",
        "claims": [{"text": "Claim", "evidence_ids": ["ev_fake"]}],
        "insufficient_evidence": False,
    }, [ev])
    assert "invalid_inline_citation" in errors
    assert "invalid_claim_citation" in errors
    assert generated["used_evidence_ids"] == []


def test_generation_accepts_resolvable_claim_and_citation():
    ev = evidence("ev_good")
    generated, errors = rag._validate_generated({
        "answer": "Claim [cite:ev_good]",
        "claims": [{"text": "Claim", "evidence_ids": ["ev_good"]}],
        "insufficient_evidence": False,
    }, [ev])
    assert errors == []
    assert generated["used_evidence_ids"] == ["ev_good"]


def test_verifier_failure_is_unavailable_not_verified():
    class Broken:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("provider down")

    generated = {"claims": [{"text": "Claim", "evidence_ids": ["ev_good"]}]}
    result = rag._verify(Broken(), "gpt-4o", "question", generated, [evidence("ev_good")])
    assert result["state"] == "unavailable"


def test_citations_resolve_only_known_evidence():
    ev = evidence("ev_good")
    generated = {"used_evidence_ids": ["ev_good"]}
    citations = rag._citations(generated, [ev])
    assert citations[0]["evidence_id"] == "ev_good"
    assert citations[0]["source_id"] == "s1"
