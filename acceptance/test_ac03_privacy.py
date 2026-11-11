"""AC3. Private data stays scoped and scrubbed."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from acceptance.support import Check

PLANTED_KEY = "sk-ant-PLANTEDKEYVALUE99"
PLANTED_EMAIL = "planted.user@example.com"
PLANTED_PHONE = "+1 415 555 0199"
PLANTED_FILE = "PLANTED-FILE-SECRET-9f3a"
PLANTED_BEARER = "Bearer planted-token-value-9f3a"
OUTSIDE = "PLANTED-OUTSIDE-SECRET"


def _blob() -> str:
    return " ".join((PLANTED_KEY, PLANTED_EMAIL, PLANTED_PHONE, PLANTED_FILE, PLANTED_BEARER, OUTSIDE))


def check(mac: bool = False):
    del mac
    probe = Check(3)
    from shadow.common.scrub import outbound
    from shadow.trial.judge import Judge, choose_judge
    from shadow.trial.picking import pick_turns
    from shadow.trial.reads import read_scoped, scoped_path
    from shadow.common.models import Model

    root = Path(tempfile.mkdtemp(prefix="shadow-privacy-"))
    work = root / "work"
    work.mkdir()
    (work / "notes.txt").write_text(f"notes api_key={PLANTED_FILE} {PLANTED_BEARER} {PLANTED_KEY}\n")
    (work / ".env").write_text(f"SECRET={PLANTED_FILE}\n")
    outside = root / "outside.txt"
    outside.write_text(OUTSIDE + "\n")
    sessions = [{
        "id": "session-1",
        "conversation_id": "conversation-1",
        "source": "cli",
        "cwd": str(work),
        "messages": [
            {"role": "assistant", "content": f"Earlier context {PLANTED_KEY} {PLANTED_EMAIL}"},
            {"role": "user", "id": "turn-1", "content": f"Please review the notes and explain {PLANTED_PHONE} carefully."},
            {"role": "assistant", "content": f"ORIGINAL_ANSWER {OUTSIDE} {PLANTED_KEY}"},
        ],
    }]
    selection = pick_turns(sessions, seed=11, count=30)
    probe.expect(len(selection.turns) == 1, "planted history did not yield the one qualifying turn")
    captured = []
    for turn in selection.turns:
        captured.append(outbound({"goal": turn.goal, "context": turn.context}))
    try:
        scoped_path(work, outside)
        probe.expect(False, "a read outside the working folder was allowed")
    except (ValueError, OSError):
        probe.note("outside read refused")
    try:
        scoped_path(work, work / ".env")
        probe.expect(False, "a read of a secret-looking file was allowed")
    except (ValueError, OSError):
        probe.note("secret-file read refused")
    allowed = read_scoped(work, work / "notes.txt")
    captured.append({"read": allowed})
    current = Model("openai-codex", "gpt-6.1-sol")
    trial = Model("openai-codex", "gpt-6-luna")
    judge = Model("anthropic", "claude-sonnet-4-5")
    choice = choose_judge(current, trial, [current, trial, judge])

    class LLM:
        def complete_structured(self, **kwargs):
            captured.append(kwargs["input"][0]["text"])
            return SimpleNamespace(
                provider=kwargs["provider"], model=kwargs["model"], text="same",
                parsed={"pick": "same", "fixes_1": "none", "fixes_2": "none",
                        "reason_kind": "missed_request", "reason": "Both covered the request."},
                usage={"input_tokens": 20, "output_tokens": 10},
            )

    turn = selection.turns[0]
    answers = [
        {"text": "new answer", "tool_calls": [], "stopped": None, "did_not_finish": False},
        {"text": "your answer", "tool_calls": [], "stopped": None, "did_not_finish": False},
    ]
    from shadow.trial.usage import resolve_prices
    Judge(LLM(), choice, (trial, current), prices=resolve_prices(choice.models)).call(turn, answers)
    flat = json.dumps(captured)
    for label, value in (
        ("key", PLANTED_KEY), ("email", PLANTED_EMAIL), ("phone", PLANTED_PHONE),
        ("file secret", PLANTED_FILE), ("bearer", "planted-token-value-9f3a"),
        ("outside", OUTSIDE), ("original answer", "ORIGINAL_ANSWER"),
    ):
        probe.expect(value not in flat, f"planted {label} appeared in an outbound payload")
    probe.expect(selection.scrub_counts.get("key", 0) > 0, "scrubber did not count a removed key")
    return probe.finish(
        "outbound payloads omit planted key, email, phone, file secret, and original answer; both reads refused"
    )


def test_ac03_secrets_stay_on_the_machine():
    result = check(False)
    assert result.status == "PASS", result.evidence
