"""
/assistant/tts: auth, config, input cleanup, and that a slow speech call
doesn't freeze the rest of the server.
"""

import asyncio
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant import router as r


def app_for(perms):
    app = FastAPI()
    app.include_router(r.router)

    async def fake_auth():
        return {"user": {"email": "t@x.com"}, "token": "t", "role": "x", "permissions": perms}
    app.dependency_overrides[r._auth] = fake_auth
    return app


OK = {"viewAssistantAvatar": True}


class FakeResp:
    status_code = 200
    content = b"ID3fake-mp3"
    text = ""


@pytest.fixture(autouse=True)
def tts_env(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_TTS_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_TTS_ENDPOINT", "https://tts.example")
    r._hits.clear()
    r._tts_cache.clear()


def test_tts_needs_the_avatar_permission():
    res = TestClient(app_for({})).post("/assistant/tts", json={"text": "hi"})
    assert res.status_code == 403


def test_tts_returns_mp3(monkeypatch):
    seen = {}

    def fake_post(url, **kw):
        seen.update(kw["json"], url=url)
        return FakeResp()
    monkeypatch.setattr(r, "requests_post", fake_post)
    res = TestClient(app_for(OK)).post("/assistant/tts", json={"text": "Hello there", "voice": "shimmer",
                                                                "emotion": "happy"})
    assert res.status_code == 200 and res.headers["content-type"] == "audio/mpeg"
    assert res.content == FakeResp.content
    assert seen["voice"] == "shimmer" and seen["input"] == "Hello there"
    assert seen["url"].endswith("/audio/speech")


def test_unknown_voice_falls_back_to_default(monkeypatch):
    seen = {}
    monkeypatch.setattr(r, "requests_post", lambda url, **kw: seen.update(kw["json"]) or FakeResp())
    TestClient(app_for(OK)).post("/assistant/tts", json={"text": "hi", "voice": "robot9000"})
    assert seen["voice"] == r._settings()["tts_voice"]


def test_long_text_is_capped(monkeypatch):
    seen = {}
    monkeypatch.setattr(r, "requests_post", lambda url, **kw: seen.update(kw["json"]) or FakeResp())
    TestClient(app_for(OK)).post("/assistant/tts", json={"text": "a" * 10000})
    assert len(seen["input"]) == r._TTS_MAX_CHARS


def test_empty_text_is_a_no_op():
    assert TestClient(app_for(OK)).post("/assistant/tts", json={"text": "  "}).status_code == 204


def test_not_configured_is_503_so_the_browser_uses_its_own_voice(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_TTS_API_KEY")
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    assert TestClient(app_for(OK)).post("/assistant/tts", json={"text": "hi"}).status_code == 503


def test_upstream_failure_is_502(monkeypatch):
    class Bad(FakeResp):
        status_code = 500
    monkeypatch.setattr(r, "requests_post", lambda url, **kw: Bad())
    assert TestClient(app_for(OK)).post("/assistant/tts", json={"text": "hi"}).status_code == 502


def test_upstream_timeout_is_502(monkeypatch):
    def boom(url, **kw):
        raise r.requests.Timeout("slow")
    monkeypatch.setattr(r, "requests_post", boom)
    assert TestClient(app_for(OK)).post("/assistant/tts", json={"text": "hi"}).status_code == 502


def test_slow_tts_does_not_block_other_requests(monkeypatch):
    """A 1.5s speech call used to hold the event loop, so a tour's action acks
    (and every other request) waited behind it."""
    def slow_post(url, **kw):
        time.sleep(1.5)
        return FakeResp()
    monkeypatch.setattr(r, "requests_post", slow_post)
    app = app_for(OK)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            t0 = time.time()
            tts = asyncio.create_task(c.post("/assistant/tts", json={"text": "hi"}))
            await asyncio.sleep(0.1)   # let the TTS call get going
            ack = await c.post("/assistant/action-ack", json={"ack_id": "nope", "success": True})
            waited = time.time() - t0   # a blocked loop shows up here, whenever it happened
            await tts
            return ack.status_code, waited

    status, waited = asyncio.run(scenario())
    assert status == 200
    assert waited < 0.8, f"action-ack waited {waited:.2f}s behind a TTS call"


def test_repeated_lines_come_from_the_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(r, "requests_post", lambda url, **kw: calls.append(1) or FakeResp())
    c = TestClient(app_for(OK))
    first = c.post("/assistant/tts", json={"text": "I've opened Review for you.", "voice": "shimmer"})
    second = c.post("/assistant/tts", json={"text": "I've opened Review for you.", "voice": "shimmer"})
    other_voice = c.post("/assistant/tts", json={"text": "I've opened Review for you.", "voice": "verse"})
    assert first.content == second.content and second.headers.get("X-TTS-Cache") == "hit"
    assert len(calls) == 2   # first line + the different voice; the repeat was free
    assert other_voice.status_code == 200


def test_failed_clips_are_not_cached(monkeypatch):
    class Bad(FakeResp):
        status_code = 500
    monkeypatch.setattr(r, "requests_post", lambda url, **kw: Bad())
    TestClient(app_for(OK)).post("/assistant/tts", json={"text": "x"})
    assert len(r._tts_cache) == 0