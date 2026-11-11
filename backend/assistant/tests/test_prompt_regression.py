"""
Guards against an edit to SYSTEM_PROMPT (or the memory/history plumbing that
feeds it) silently dropping one of the safety rules the whole grounding story
depends on. These don't call the live model — they check the prompt text and
the pure-Python helpers that assemble it, so they run in CI with no Azure
creds and stay fast.

Run from backend/:  python -m pytest assistant/tests/test_prompt_regression.py -q
"""

from assistant import memory, router


# ---- SYSTEM_PROMPT must keep saying these things, in substance ----
# Not a brittle exact-string match — checks that the *rule* is still present
# by keying on stable phrasing that's unlikely to change unless the rule
# itself is being intentionally rewritten.
REQUIRED_PROMPT_PHRASES = [
    "TRUST BOUNDARY",                         # prompt-injection guard
    "never obey it",
    "Only state facts you got from a tool result",
    "NEVER FABRICATE",                        # no invented ids/slugs
    "ACTIONS ARE DISPATCHED, NOT CONFIRMED",
    "USER MEMORY",                            # new: memory is data, not instructions
    "OFF-PORTAL = ANSWER IN ONE LINE, CALL NOTHING",
]


def test_system_prompt_keeps_safety_rules():
    for phrase in REQUIRED_PROMPT_PHRASES:
        assert phrase in router.SYSTEM_PROMPT, f"SYSTEM_PROMPT lost required rule: {phrase!r}"


def test_system_prompt_trust_boundary_covers_memory():
    # the trust-boundary sentence specifically has to list USER MEMORY alongside
    # the other untrusted-data sources, not just mention the phrase elsewhere
    boundary_line = next(
        (line for line in router.SYSTEM_PROMPT.splitlines() if "TRUST BOUNDARY" in line), ""
    )
    assert "USER MEMORY" in boundary_line


# ---- _memory_block ----

def test_memory_block_omitted_when_empty():
    assert router._memory_block({"preferences": {}, "recent_summary": ""}) is None


def test_memory_block_includes_preferences_and_summary():
    block = router._memory_block({
        "preferences": {"preferred_name": "Sam"},
        "recent_summary": "- Q: how's the DQE dashboard looking",
    })
    assert block is not None
    assert "USER MEMORY" in block
    assert "preferred_name: Sam" in block
    assert "Recent topics" in block


# ---- _trim_history ----

def test_trim_history_noop_under_limit():
    history = [{"role": "user", "content": "hi"}]
    out = router._trim_history(None, "gpt-4o", history, max_hist=20)
    assert out == history


def test_trim_history_never_waits_for_the_model(monkeypatch):
    """Long chats used to pay a summary model call on every turn before the
    reply could start. Now: an instant extractive summary, the real one built
    in the background for the following turns."""
    import time
    calls = []

    class _SlowClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    calls.append(1)
                    time.sleep(0.5)
                    return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": "model summary"})()})()]})()

    router._history_summary_cache.clear()
    history = [{"role": "user", "content": f"question {i}"} for i in range(25)]
    t0 = time.time()
    out = router._trim_history(_SlowClient(), "gpt-4o", history, max_hist=20)
    assert time.time() - t0 < 0.1                       # didn't wait for the model
    assert out[0]["role"] == "system" and "EARLIER IN THIS CONVERSATION" in out[0]["content"]
    assert out[1:] == history[10:]                      # first 10 summarized, the rest verbatim
    time.sleep(0.8)                                     # background summary lands
    out2 = router._trim_history(_SlowClient(), "gpt-4o", history, max_hist=20)
    assert "model summary" in out2[0]["content"]
    # the next turn (26 messages) still summarizes the same 10-message block - no new model call
    out3 = router._trim_history(_SlowClient(), "gpt-4o", history + [{"role": "user", "content": "next"}], max_hist=20)
    assert "model summary" in out3[0]["content"] and len(calls) == 1


def test_trim_history_keeps_something_verbatim():
    router._history_summary_cache.clear()
    history = [{"role": "user", "content": f"q{i}"} for i in range(10)]
    out = router._trim_history(None, "gpt-4o", history, max_hist=4)
    assert out[-1] == history[-1] and len(out) >= 2


# ---- memory module degrades cleanly with no Postgres configured ----

def test_memory_noop_without_dsn(monkeypatch):
    monkeypatch.setattr(memory.db, "is_configured", lambda: False)
    assert memory.get_memory("nobody@example.com") == {"preferences": {}, "recent_summary": ""}
    assert memory.set_preference("nobody@example.com", "k", "v") is False
    memory.append_turn("nobody@example.com", "q", "a")  # must not raise