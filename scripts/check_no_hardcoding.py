"""Fail CI if site knowledge / canned recipes leak into the agent source.

Windows-friendly: run with ``python scripts/check_no_hardcoding.py``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "src" / "hydra"

fail = 0


def check(name: str, pattern: str, *paths: Path) -> None:
    global fail
    rx = re.compile(pattern)
    hits: list[str] = []
    for base in paths:
        if not base.exists():
            continue
        files = [base] if base.is_file() else list(base.rglob("*.py"))
        for path in files:
            if path.suffix != ".py":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()}")
    if hits:
        print(f"FAIL: {name}")
        for h in hits:
            print(f"    {h}")
        fail = 1
    else:
        print(f"ok:   {name}")


def main() -> int:
    global fail

    check(
        "no task recipes",
        r"(def|function)\s+(delete_spam|order_food|apply_to_job|checkout|add_to_cart|login_to)",
        AGENT,
    )
    check(
        "no site test-id selectors anywhere",
        r"data-qa|data-test|data-testid|data-automation|data-marker|data-widget",
        AGENT,
    )
    check(
        "no selectors in the agent's own logic",
        r"querySelector|css=|xpath=|\.getElementById",
        AGENT / "agent",
        AGENT / "browser" / "actions.py",
    )
    check(
        "no site urls in the agent",
        r"https?://(www\.)?[a-z0-9-]+\.(ru|com|org|net)",
        AGENT,
    )
    check(
        "no site names in the prompt",
        r"(yandex|hh\.ru|ozon|gmail|habr|avito|papajohns|chitai|wildberries|delivery)",
        AGENT / "agent" / "prompts.py",
    )
    check(
        "no url paths hinted in the prompt",
        r"/(vacancies|cart|checkout|inbox|orders|search)\b",
        AGENT / "agent" / "prompts.py",
    )

    # Risk vocabulary must only gate, never choose actions outside safety.py.
    assess_hits: list[str] = []
    for path in AGENT.rglob("*.py"):
        if path.name == "safety.py":
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if ".assess(" in line:
                assess_hits.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()}")
    if assess_hits:
        print("FAIL: the risk classifier is used outside the safety gate")
        for h in assess_hits:
            print(f"    {h}")
        fail = 1
    else:
        print("ok:   risk vocabulary can only gate, never choose")

    print()
    if fail == 0:
        print("no site knowledge is baked in")
    else:
        print("site knowledge leaked into the agent")
    return fail


if __name__ == "__main__":
    sys.exit(main())
