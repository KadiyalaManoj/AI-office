from pathlib import Path

import pytest

from agents.pipeline import (CHECKS, CODE_LIMIT, GATES, MAX_TEAM, extract_files, gate_summary, history_text,
                             lens_verdict, step_prompt, unique_dir, valid_team)
from agents.roles import ECHO_JOBS


# --- team rules: the release gate can never be lost or reordered -------------------------------

def assert_gated(team):
    """Every coder is followed by Lens, Probe, Relay; no coder comes after Relay."""
    coders = [i for i, x in enumerate(team) if x in ("byte", "pixel", "terra")]
    assert coders, team
    lens, probe, relay = team.index("lens"), team.index("probe"), team.index("relay")
    assert max(coders) < lens < probe < relay, team


def test_relay_before_lens_is_reordered():
    assert_gated(valid_team(["byte", "relay", "lens"], "feature"))


def test_cap_never_drops_the_gate():
    proposed = ["nova", "iris", "forge", "pixel", "probe", "pulse", "quill", "echo", "nova", "byte",
                "pulse", "quill", "echo"]
    team = valid_team(proposed, "feature")
    assert len(team) <= MAX_TEAM
    assert_gated(team)
    assert {"pixel", "byte"} <= set(team)


def test_cap_with_many_coders_keeps_coders_and_gates():
    team = valid_team(["byte", "nova", "pixel", "iris", "terra", "forge"] * 3, "feature")
    assert len(team) <= MAX_TEAM
    assert_gated(team)


def test_post_release_work_moves_after_relay():
    team = valid_team(["pulse", "byte", "quill", "lens"], "feature")
    assert_gated(team)
    assert team.index("pulse") > team.index("relay") and team.index("quill") > team.index("relay")


def test_bug_triage_echo_stays_first_and_closing_echo_goes_last():
    team = valid_team(["echo", "byte", "lens", "relay", "echo"], "bug")
    assert team[0] == "echo" and team[-1] == "echo"
    assert_gated(team)


def test_no_code_means_no_review_or_release_steps():
    team = valid_team(["nova", "lens", "quill", "relay", "echo"], "growth")
    assert "lens" not in team and "relay" not in team


def test_unknown_agents_and_atlas_are_removed_and_empty_falls_back():
    assert valid_team(["atlas", "ghost"], "infra") == ["forge", "terra"] + GATES + ["pulse"]
    assert valid_team(None, "growth") == ["nova", "quill", "echo"]


# --- context: reviewers see the whole latest code ----------------------------------------------

def test_latest_code_is_passed_in_full():
    code = "File: app/a.py\n```python\n" + "\n".join(f"x{i} = {i}" for i in range(900)) + "\n```"
    assert 6000 < len(code) < CODE_LIMIT
    text = history_text([("nova", "spec"), ("byte", code)])
    assert "x899 = 899" in text


def test_superseded_draft_is_shortened_and_latest_kept():
    old = "OLD" + "o" * 5000
    new = "NEW" + "n" * 5000
    text = history_text([("byte", old), ("lens", "review"), ("byte", new)])
    assert "earlier draft, replaced" in text
    assert new in text


def test_check_results_are_shown_as_automated_checks():
    assert "### Automated checks" in history_text([(CHECKS, "Status: FAILED")])


# --- Echo gets a job that fits the task --------------------------------------------------------

META = {"priority": "P2", "brief": "b"}


@pytest.mark.parametrize("kind,team,pos,job", [
    ("growth", ["nova", "quill", "echo"], 2, "launch"),
    ("bug", ["echo", "byte", "lens", "probe", "relay", "echo"], 0, "triage"),
    ("bug", ["echo", "byte", "lens", "probe", "relay", "echo"], 5, "fixed"),
    ("feature", ["nova", "byte", "lens", "probe", "relay", "echo"], 5, "shipped"),
    ("feature", ["echo", "nova"], 0, "voice"),
])
def test_echo_job_fits_the_task(kind, team, pos, job):
    prompt = step_prompt("ctx", "t", {**META, "kind": kind}, team, pos, [])
    assert ECHO_JOBS[job] in prompt


def test_codebase_only_goes_to_agents_that_need_it():
    team = ["nova", "byte"]
    assert "existing codebase" not in step_prompt("c", "t", {**META, "kind": "feature"}, team, 0, [], "TREE")
    assert "existing codebase" in step_prompt("c", "t", {**META, "kind": "feature"}, team, 1, [], "TREE")


# --- file extraction ---------------------------------------------------------------------------

@pytest.mark.parametrize("marker", [
    "File: app/x.py", "**File:** `app/x.py`", "**File:** app/x.py", "### File: `app/x.py`", "- File: app/x.py",
])
def test_extract_accepts_common_markers(tmp_path, marker):
    got = extract_files(f"{marker}\n\n```python\nx = 1\n```\n", tmp_path)
    assert [p.relative_to(tmp_path.resolve()).as_posix() for p in got.saved] == ["app/x.py"]
    assert (tmp_path / "app/x.py").read_text() == "x = 1\n"


@pytest.mark.parametrize("path", ["../evil.py", "/etc/passwd", "a/../../evil.py"])
def test_extract_refuses_unsafe_paths(tmp_path, path):
    got = extract_files(f"File: {path}\n```python\nx\n```", tmp_path / "dest")
    assert not got.saved and got.skipped
    assert not (tmp_path / "evil.py").exists()


def test_extract_counts_unlabeled_blocks(tmp_path):
    got = extract_files("Here:\n```python\nx = 1\n```", tmp_path)
    assert not got.saved and got.unlabeled_blocks == 1


def test_code_containing_backticks_is_kept_whole(tmp_path):
    body = 'msg = "use `pip install`"\nprint(msg)\n'
    extract_files(f"File: a.py\n```python\n{body}```", tmp_path)
    assert (tmp_path / "a.py").read_text() == body


# --- release gate summary ----------------------------------------------------------------------

def test_gate_is_clean_when_everything_passes():
    s = gate_summary([("lens", "ok\nVERDICT: APPROVE"), ("probe", "plan\nQA: READY")], "PASSED")
    assert s.blockers == [] and s.lens_verdict == "APPROVE" and s.qa == "READY"


def test_gate_reports_every_blocker():
    history = [("lens", "[MAJOR] auth: token not checked. Fix: verify\nVERDICT: CHANGES REQUESTED"),
               ("probe", "QA: BLOCKED - no tests for expired tokens")]
    s = gate_summary(history, "FAILED")
    assert len(s.blockers) == 3
    assert any("MAJOR" in f for f in s.findings)
    assert s.qa.startswith("BLOCKED")


def test_gate_uses_the_latest_review():
    history = [("lens", "VERDICT: CHANGES REQUESTED"), ("byte", "fixed"), ("lens", "VERDICT: APPROVE"),
               ("probe", "QA: READY")]
    assert gate_summary(history).blockers == []


def test_missing_verdict_is_a_blocker():
    assert "did not give a verdict" in gate_summary([("lens", "looks fine")]).blockers[0]
    assert lens_verdict("verdict: changes   requested") == "CHANGES REQUESTED"


def test_unique_dir(tmp_path):
    base = tmp_path / "run"
    assert unique_dir(base) == base
    base.mkdir()
    (tmp_path / "run-2").mkdir()
    assert unique_dir(base) == tmp_path / "run-3"
