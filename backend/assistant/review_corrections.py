"""
Review the corrections users have submitted before any of them reach the assistant.

Raw feedback from /assistant/feedback lands in knowledge/pending_corrections.jsonl
and is NOT embedded into the knowledge base. This is the gate: an admin runs

    python -m assistant.review_corrections

and decides each one — promote it into learned.md (where it gets embedded on the
next build_kb), or drop it. Nothing a random user typed becomes "fact" for everyone
without a human saying yes first.

Flags:
    --list      just show what's pending, don't prompt
    --yes-all   promote everything without prompting (use sparingly)
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
PENDING = HERE / "knowledge" / "pending_corrections.jsonl"
LEARNED = HERE / "knowledge" / "learned.md"


def _read_pending():
    if not PENDING.exists():
        return []
    out = []
    for line in PENDING.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _promote(entry):
    block = (
        f"\n## Correction (promoted {datetime.now(timezone.utc):%Y-%m-%d})\n"
        f"Question: {entry.get('question') or '(not given)'}\n"
        f"Correct answer: {entry.get('correction', '').strip()}\n"
        f"(submitted by {entry.get('submitted_by', 'unknown')})\n"
    )
    with open(LEARNED, "a", encoding="utf-8") as f:
        f.write(block)


def main():
    args = sys.argv[1:]
    pending = _read_pending()
    if not pending:
        print("Nothing pending. The queue is empty.")
        return

    if "--list" in args:
        for i, e in enumerate(pending, 1):
            print(f"[{i}] {e.get('submitted_by')}  {e.get('submitted_at', '')}")
            print(f"     Q: {e.get('question') or '(none)'}")
            print(f"     A: {e.get('correction')}")
        print(f"\n{len(pending)} pending. Run without --list to act on them.")
        return

    yes_all = "--yes-all" in args
    kept = []   # entries we couldn't decide / chose to keep pending
    promoted = 0

    for e in pending:
        print("-" * 60)
        print(f"from : {e.get('submitted_by')}   at {e.get('submitted_at', '')}")
        print(f"Q    : {e.get('question') or '(none)'}")
        print(f"A    : {e.get('correction')}")
        if yes_all:
            choice = "p"
        else:
            choice = input("[p]romote  [d]iscard  [s]kip (keep pending)  [q]uit > ").strip().lower()
        if choice == "q":
            kept.append(e)
            kept.extend(pending[pending.index(e) + 1:])
            break
        if choice == "p":
            _promote(e)
            promoted += 1
        elif choice == "s":
            kept.append(e)
        # 'd' (or anything else) drops it

    # rewrite the queue with only the ones still pending
    if kept:
        PENDING.write_text("".join(json.dumps(k) + "\n" for k in kept), encoding="utf-8")
    else:
        PENDING.write_text("", encoding="utf-8")

    print("-" * 60)
    print(f"promoted {promoted} into learned.md, {len(kept)} left pending.")
    if promoted:
        print("Run `python -m assistant.build_kb` to embed the promoted ones.")


if __name__ == "__main__":
    main()