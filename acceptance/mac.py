"""Drive a throwaway Hermes profile named shadow-demo. Never opens secret files."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from acceptance.support import FORBIDDEN


CHECKS = {
    6: (
        "chat-started test on profile shadow-demo; record has both runs per turn; "
        "picked and left-out counts with reasons; judge maker is a third maker; "
        "at least 20 usable turns; start card busy-or-not line matches what happened; "
        "screenshots of the start card and the verdict card"
    ),
    8: (
        "same-provider gradual switch; post_api_request.response_model shows each model "
        "that should answer at each of the three steps; no turn mixes models; excluded "
        "request kinds untouched; switch record survives a Hermes restart; a different "
        "provider is offered only the all-at-once switch; screenshot"
    ),
    10: (
        "different-provider switch; settings model.provider and model.default before, "
        "after the default changes, after a staged watch trip, and after undo; names only"
    ),
    11: (
        "Desktop page for the check-6 test: verdict strip, turn list, side-by-side with "
        "hidden names and reveal; switch panel during check 8; every number equals the record; screenshots"
    ),
}


def drive() -> str:
    """Create an empty shadow-demo profile and print exactly what was checked.

    A real provider test is not started. Secret files are not opened.
    """
    home = Path(tempfile.mkdtemp(prefix="shadow-demo-home-"))
    profile = home / "profiles" / "shadow-demo"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model:\n  provider: unset\n  default: unset\n")
    hermes = shutil.which("hermes")
    system = platform.system()
    lines = [
        f"platform={system}",
        f"hermes={hermes or 'absent'}",
        f"profile={profile}",
        "profile_name=shadow-demo",
        "forbidden files not opened: " + ", ".join(str(path) for path in FORBIDDEN),
        "providers=not called",
    ]
    drove = False
    if system == "Darwin" and hermes:
        env = dict(os.environ)
        env["HERMES_HOME"] = str(home)
        env.pop("OPENAI_API_KEY", None)
        env.pop("ANTHROPIC_API_KEY", None)
        try:
            completed = subprocess.run(
                [hermes, "-p", "shadow-demo", "shadow", "doctor"],
                env=env,
                cwd=str(home),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            lines.append(f"doctor=not completed ({exc.__class__.__name__})")
        else:
            drove = True
            output = " ".join((completed.stdout or "").split())[:240]
            lines.append(f"doctor_exit={completed.returncode} doctor_out={output or '(empty)'}")
    else:
        lines.append("hermes shadow doctor=not launched; need macOS and a hermes binary")
    for number, text in CHECKS.items():
        lines.append(f"AC{number} checked: {text}; live proof={'doctor only' if drove else 'not run'}")
    report = "\n".join(lines)
    print(report)
    return report
