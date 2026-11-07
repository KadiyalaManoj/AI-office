"""End-to-end runs of the office with a fake Claude client."""

import json
from types import SimpleNamespace

import pytest

import agents.office as office


class FakeClient:
    def __init__(self, replies):
        self.replies = replies          # agent name -> list of replies (last one repeats)
        self.calls = []
        self.messages = self

    def create(self, model, max_tokens, messages):
        prompt = messages[0]["content"]
        who = prompt.split("You are ", 1)[1].split(",", 1)[0]
        self.calls.append((who, prompt, max_tokens))
        queue = self.replies.get(who, [f"{who} deliverable"])
        text = queue.pop(0) if len(queue) > 1 else queue[0]
        cut = text.startswith("CUT:")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text.removeprefix("CUT:"))],
                               stop_reason="max_tokens" if cut else "end_turn")


PLAN = json.dumps({"kind": "feature", "priority": "P1", "brief": "Add Okta SSO.",
                   "team": ["nova", "byte", "relay", "lens", "pulse"]})
CODE = "File: app/sso.py\n```python\n" + "\n".join(f"v{i} = {i}" for i in range(400)) + "\n```\n"


def run(monkeypatch, tmp_path, replies, *extra, tty=True, answer="y"):
    client = FakeClient(replies)
    monkeypatch.setattr(office.anthropic, "Anthropic", lambda: client)
    monkeypatch.setattr(office.sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    rc = office.main(["Add SSO login with Okta", "--out", str(tmp_path), *extra])
    run_dir = next(p for p in tmp_path.iterdir() if p.is_dir())
    return rc, client, run_dir


def order(client):
    return [who for who, _, _ in client.calls]


def test_full_run_with_revision_and_approval(monkeypatch, tmp_path):
    replies = {"Atlas": [PLAN], "Byte": [CODE],
               "Lens": ["[MAJOR] x: y. Fix: z\nVERDICT: CHANGES REQUESTED", "VERDICT: APPROVE"],
               "Probe": ["plan\nQA: READY"]}
    rc, client, run_dir = run(monkeypatch, tmp_path, replies)
    assert rc == 0
    # Atlas's unsafe order (relay before lens) was fixed, and Lens sent the code back once.
    assert order(client) == ["Atlas", "Nova", "Byte", "Lens", "Byte", "Lens", "Probe", "Relay", "Pulse"]
    # Lens saw the whole file, not a truncated one.
    lens_prompt = next(p for who, p, _ in client.calls if who == "Lens")
    assert "v399 = 399" in lens_prompt
    assert (run_dir / "code/app/sso.py").exists()
    assert "Lens verdict: APPROVE" in (run_dir / "GATE.md").read_text()


def test_gate_requires_typed_release_when_lens_still_objects(monkeypatch, tmp_path):
    replies = {"Atlas": [PLAN], "Byte": [CODE], "Lens": ["VERDICT: CHANGES REQUESTED"],
               "Probe": ["QA: READY"]}
    rc, client, run_dir = run(monkeypatch, tmp_path, replies, answer="y")
    assert rc == 2 and "Relay" not in order(client)
    assert "Lens still requests changes" in (run_dir / "GATE.md").read_text()

    rc, client, _ = run(monkeypatch, tmp_path / "again", replies, answer="RELEASE")
    assert rc == 0 and "Relay" in order(client)


def test_non_interactive_run_stops_at_gate(monkeypatch, tmp_path):
    replies = {"Atlas": [PLAN], "Byte": [CODE], "Lens": ["VERDICT: APPROVE"], "Probe": ["QA: READY"]}
    rc, client, _ = run(monkeypatch, tmp_path, replies, tty=False)
    assert rc == 2 and "Relay" not in order(client)


def test_approve_flag_releases_only_without_blockers(monkeypatch, tmp_path):
    good = {"Atlas": [PLAN], "Byte": [CODE], "Lens": ["VERDICT: APPROVE"], "Probe": ["QA: READY"]}
    rc, client, _ = run(monkeypatch, tmp_path / "a", good, "--approve", tty=False)
    assert rc == 0 and "Relay" in order(client)

    blocked = {"Atlas": [PLAN], "Byte": [CODE], "Lens": ["VERDICT: APPROVE"], "Probe": ["QA: BLOCKED - flaky"]}
    rc, client, _ = run(monkeypatch, tmp_path / "b", blocked, "--approve", tty=False)
    assert rc == 2 and "Relay" not in order(client)


def test_cut_off_reply_is_flagged(monkeypatch, tmp_path, capsys):
    replies = {"Atlas": [PLAN], "Byte": ["CUT:" + CODE], "Lens": ["VERDICT: APPROVE"], "Probe": ["QA: READY"]}
    run(monkeypatch, tmp_path, replies)
    assert "cut off" in capsys.readouterr().out
    lens_prompt = next(p for who, p, _ in office.anthropic.Anthropic().calls if who == "Lens")
    assert "hit the length limit" in lens_prompt


def test_rerun_uses_a_new_folder(monkeypatch, tmp_path):
    replies = {"Atlas": [json.dumps({"kind": "growth", "team": ["nova", "quill"]})]}
    run(monkeypatch, tmp_path, replies)
    run(monkeypatch, tmp_path, {"Atlas": [json.dumps({"kind": "growth", "team": ["nova", "quill"]})]})
    assert len([p for p in tmp_path.iterdir() if p.is_dir()]) == 2


def test_checks_results_reach_lens(monkeypatch, tmp_path):
    passing = ("File: calc.py\n```python\ndef add(a, b):\n    return a + b\n```\n"
               "File: tests/test_calc.py\n```python\nfrom calc import add\n\n"
               "def test_add():\n    assert add(2, 2) == 4\n```\n")
    replies = {"Atlas": [PLAN], "Byte": [passing], "Lens": ["VERDICT: APPROVE"], "Probe": ["QA: READY"]}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    rc, client, run_dir = run(monkeypatch, tmp_path, replies, "--run-checks")
    assert rc == 0
    lens_prompt = next(p for who, p, _ in client.calls if who == "Lens")
    assert "### Automated checks" in lens_prompt and "Status: PASSED" in lens_prompt
    assert "Checks:       PASSED" in (run_dir / "GATE.md").read_text()
