"""scripts/plan_feedback.py: confirmations and reported problems on library plans."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import plan_feedback as fb  # noqa: E402
from _common import plan_files  # noqa: E402

from engine import validate_plan  # noqa: E402

TODAY = date(2026, 11, 21)
REL = "plans/AU/NSW/CovaU-SolarMaxTOU-NSW-EssentialEnergy-20260701.json"


@pytest.fixture
def root(tmp_path):
    """A copy of one library plan in a repo-shaped folder."""
    source = next(p for p in plan_files() if p.name == Path(REL).name)
    target = tmp_path / REL
    target.parent.mkdir(parents=True)
    shutil.copyfile(source, target)
    return tmp_path


def load(root):
    return json.loads((root / REL).read_text(encoding="utf-8"))


def test_confirm_records_the_person_and_clears_needs_review(root):
    path = fb.plan_file(REL, root)
    assert fb.confirm(path, "octocat", 42, TODAY) == f"{path.name}: confirmed by octocat (1 of 2, 1 more needed)"
    assert load(root)["needs_review"] is True  # one isn't enough
    assert fb.confirm(path, "hubot", 43, TODAY) == f"{path.name}: confirmed by hubot (2 of 2, Confirmed)"
    plan = load(root)
    assert plan["needs_review"] is False
    assert plan["confirmations"] == [{"date": "2026-11-21", "by": "octocat", "issue": 42},
                                     {"date": "2026-11-21", "by": "hubot", "issue": 43}]
    assert validate_plan(plan, today=TODAY).ok  # the plan format accepts confirmations
    keys = list(plan)
    assert keys.index("confirmations") == keys.index("needs_review") + 1


def test_one_confirmation_per_account(root):
    path = fb.plan_file(REL, root)
    fb.confirm(path, "octocat", 42, TODAY)
    assert "already confirmed" in fb.confirm(path, "OctoCat", 43, TODAY)
    fb.confirm(path, "hubot", 44, TODAY)
    assert [c["by"] for c in load(root)["confirmations"]] == ["octocat", "hubot"]


def test_a_reported_problem_puts_it_back_to_needs_review(root):
    path = fb.plan_file(REL, root)
    fb.confirm(path, "octocat", 42, TODAY)
    fb.confirm(path, "hubot", 43, TODAY)
    fb.problem(path, 50, date(2026, 12, 1))
    plan = load(root)
    assert plan["needs_review"] is True
    assert len(plan["confirmations"]) == 2  # history kept
    assert plan["problems"] == [{"date": "2026-12-01", "issue": 50}]
    assert "Problem reported in #50 (2026-12-01)." in plan["notes"]
    assert validate_plan(plan, today=TODAY).ok
    fb.problem(path, 50, date(2026, 12, 1))
    assert load(root)["notes"].count("#50") == 1 and len(load(root)["problems"]) == 1
    # Only confirmations after the problem count; earlier confirmers may confirm the fixed plan again
    assert fb.current_confirmations(load(root)) == []
    fb.confirm(path, "octocat", 51, TODAY)
    assert load(root)["needs_review"] is True
    fb.confirm(path, "mona", 52, TODAY)
    assert load(root)["needs_review"] is False


@pytest.mark.parametrize("relative", [
    "plans/AU/NSW/../../../etc/passwd.json", "plans/index.json", "plans/AU/NSW/Missing.json",
    "/plans/AU/NSW/X.json", "plans/AU/NSW/X.json; rm -rf /",
])
def test_only_existing_library_plan_files(root, relative):
    with pytest.raises(fb.FeedbackError):
        fb.plan_file(relative, root)


@pytest.mark.parametrize("login", ["", "-x", "a b", "x" * 40, "$(whoami)"])
def test_only_github_user_names(root, login):
    with pytest.raises(fb.FeedbackError):
        fb.confirm(fb.plan_file(REL, root), login, 1, TODAY)
