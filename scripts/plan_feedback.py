#!/usr/bin/env python3
"""Record feedback from the plan browser on a library plan (run by the Plan feedback workflow).

    python scripts/plan_feedback.py confirm plans/AU/NSW/X.json --by octocat --issue 42
    python scripts/plan_feedback.py problem plans/AU/NSW/X.json --issue 43

confirm: someone on the plan says it matches their bill. Adds them to `confirmations` (once per
GitHub account) and clears `needs_review`, so the plan shows as Confirmed.
problem: a "Report a problem" correction. Sets `needs_review` again (confirmations are kept, so the
history stays) and notes the issue, until the plan is fixed.

The path must be a library plan file (plans/<country>/<region>/<name>.json). Prints what happened.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import REPO_ROOT, dump_json  # noqa: E402

PLAN_PATH = re.compile(r"^plans/[A-Z]{2}/[A-Z]{2,3}/[A-Za-z0-9-]+\.json$")
LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class FeedbackError(ValueError):
    pass


def plan_file(relative: str, root: Path = REPO_ROOT) -> Path:
    if not PLAN_PATH.match(relative):
        raise FeedbackError(f"not a library plan file: {relative!r}")
    path = (root / relative).resolve()
    if not path.is_relative_to((root / "plans").resolve()) or not path.is_file():
        raise FeedbackError(f"no such library plan: {relative}")
    return path


def confirm(path: Path, by: str, issue: int, today: date) -> str:
    if not LOGIN.match(by):
        raise FeedbackError(f"not a GitHub user name: {by!r}")
    plan = json.loads(path.read_text(encoding="utf-8"))
    confirmations = plan.setdefault("confirmations", [])
    if any(c.get("by", "").lower() == by.lower() for c in confirmations):
        return f"{by} has already confirmed {path.name}"
    confirmations.append({"date": today.isoformat(), "by": by, "issue": issue})
    plan["needs_review"] = False
    _write(path, plan)
    return f"{path.name}: confirmed by {by} ({len(confirmations)} confirmation(s))"


def problem(path: Path, issue: int, today: date) -> str:
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan["needs_review"] = True
    note = f"Problem reported in #{issue} ({today.isoformat()})."
    if note not in (plan.get("notes") or ""):
        plan["notes"] = f"{plan.get('notes') or ''} {note}".strip()
    _write(path, plan)
    return f"{path.name}: back to Needs review (#{issue})"


def _write(path: Path, plan: dict) -> None:
    # Keep the confirmations next to needs_review, where people look for them
    ordered = {}
    for key, value in plan.items():
        if key == "confirmations":
            continue
        ordered[key] = value
        if key == "needs_review" and "confirmations" in plan:
            ordered["confirmations"] = plan["confirmations"]
    if "confirmations" in plan and "confirmations" not in ordered:
        ordered["confirmations"] = plan["confirmations"]
    path.write_text(dump_json(ordered), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("confirm", "problem"))
    parser.add_argument("file")
    parser.add_argument("--by", default="")
    parser.add_argument("--issue", type=int, required=True)
    args = parser.parse_args()
    try:
        path = plan_file(args.file)
        message = confirm(path, args.by, args.issue, date.today()) if args.action == "confirm" else problem(
            path, args.issue, date.today())
    except FeedbackError as err:
        print(f"::error::{err}")
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
