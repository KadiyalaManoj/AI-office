"""Format retries and the automatic repair loop, driven end to end with a fake model."""

import json

import pytest
from test_office_run import PLAN, FakeClient, order, run

import agents.office as office
from agents.pipeline import plan_problem, reply_problem
from agents.providers import ProviderError

GOOD = ("File: calc.py\n```python\ndef add(a, b):\n    return a + b\n```\n"
        "File: tests/test_calc.py\n```python\nfrom calc import add\n\n"
        "def test_add():\n    assert add(2, 2) == 4\n```\n")
BAD = GOOD.replace("return a + b", "return a - b")
UNDEFINED_NAME = ("File: calc.py\n```python\ndef add(a, b):\n    return a + b\n\n\n"
                  "def broken():\n    return missing_name\n```\n"
                  "File: tests/test_calc.py\n```python\nfrom calc import add\n\n"
                  "def test_add():\n    assert add(2, 2) == 4\n```\n")
OK_REPLIES = {"Atlas": [PLAN], "Lens": ["VERDICT: APPROVE"], "Probe": ["QA: READY"]}


def test_reply_problems_name_the_missing_format():
    assert reply_problem("byte", "just prose") and not reply_problem("byte", GOOD)
    assert reply_problem("lens", "looks fine") and not reply_problem("lens", "VERDICT: APPROVE")
    assert reply_problem("probe", "plan") and not reply_problem("probe", "QA: BLOCKED - x")
    assert reply_problem("nova", "anything") is None
    assert plan_problem({}) and plan_problem({"team": []}) and not plan_problem({"team": ["nova"]})


def test_lens_without_a_verdict_is_asked_again(monkeypatch, tmp_path):
    replies = {**OK_REPLIES, "Byte": [GOOD], "Lens": ["No verdict here", "VERDICT: APPROVE"]}
    rc, client, _ = run(monkeypatch, tmp_path, replies)
    assert rc == 0 and order(client).count("Lens") == 2
    retry_prompt = [p for who, p, _ in client.calls if who == "Lens"][1]
    assert "previous reply was rejected" in retry_prompt and "VERDICT" in retry_prompt


def test_coder_without_file_blocks_is_asked_again(monkeypatch, tmp_path):
    replies = {**OK_REPLIES, "Byte": ["Here is some prose.", GOOD]}
    rc, client, run_dir = run(monkeypatch, tmp_path, replies)
    assert rc == 0 and order(client).count("Byte") == 2
    assert (run_dir / "code/calc.py").exists()


def test_a_reply_that_stays_wrong_is_not_retried_forever(monkeypatch, tmp_path):
    replies = {**OK_REPLIES, "Byte": ["prose only"]}
    rc, client, _ = run(monkeypatch, tmp_path, replies)
    assert order(client).count("Byte") == 2        # one retry, then the existing warning path


def test_unusable_plan_is_asked_again(monkeypatch, tmp_path):
    replies = {**OK_REPLIES, "Atlas": ["I would pick the team like so", PLAN], "Byte": [GOOD]}
    rc, client, _ = run(monkeypatch, tmp_path, replies)
    assert rc == 0 and order(client)[:2] == ["Atlas", "Atlas"]


def test_failed_tests_send_code_back_to_its_author_until_they_pass(monkeypatch, tmp_path):
    replies = {**OK_REPLIES, "Byte": [BAD, GOOD]}
    rc, client, run_dir = run(monkeypatch, tmp_path, replies, "--run-checks")
    assert rc == 0
    assert order(client) == ["Atlas", "Nova", "Byte", "Byte", "Lens", "Probe", "Relay", "Pulse"]
    # The fixing pass saw the failing test output.
    assert "pytest: failed" in [p for who, p, _ in client.calls if who == "Byte"][1]
    assert "Checks:       PASSED" in (run_dir / "GATE.md").read_text()


def test_lint_errors_count_as_failed_checks(monkeypatch, tmp_path):
    pytest.importorskip("ruff")
    replies = {**OK_REPLIES, "Byte": [UNDEFINED_NAME, GOOD]}
    rc, client, _ = run(monkeypatch, tmp_path, replies, "--run-checks")
    assert rc == 0 and order(client).count("Byte") == 2
    assert "ruff" in [p for who, p, _ in client.calls if who == "Byte"][1]


def test_repairs_are_bounded_and_the_gate_still_blocks(monkeypatch, tmp_path):
    replies = {**OK_REPLIES, "Byte": [BAD]}
    rc, client, run_dir = run(monkeypatch, tmp_path, replies, "--run-checks", answer="y")
    assert order(client).count("Byte") == 1 + office.MAX_REPAIRS
    assert rc == 2 and "Automated checks failed" in (run_dir / "GATE.md").read_text()


def test_repair_can_be_turned_off(monkeypatch, tmp_path):
    replies = {**OK_REPLIES, "Byte": [BAD]}
    rc, client, _ = run(monkeypatch, tmp_path, replies, "--run-checks", "--max-repairs", "0", answer="y")
    assert order(client).count("Byte") == 1


class Failing(FakeClient):
    def create(self, model, max_tokens, messages):
        if "You are Byte," in messages[0]["content"]:
            raise ProviderError("Ollama isn't running. Start it with: ollama serve")
        return super().create(model, max_tokens, messages)


def test_a_failing_model_stops_the_run_cleanly(monkeypatch, tmp_path, capsys):
    client = Failing({"Atlas": [PLAN]})
    monkeypatch.setattr(office.anthropic, "Anthropic", lambda: client)
    monkeypatch.setattr(office.sys.stdin, "isatty", lambda: True)
    rc = office.main(["Add SSO login with Okta", "--out", str(tmp_path)])
    assert rc == 1 and "ollama serve" in capsys.readouterr().out
    run_dir = next(p for p in tmp_path.iterdir() if p.is_dir())
    assert "stopped, the model failed" in (run_dir / "README.md").read_text()


def test_a_dead_provider_at_start_fails_before_any_run_folder_is_left(monkeypatch, tmp_path):
    class Dead:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise ProviderError("no key")

    monkeypatch.setattr(office.anthropic, "Anthropic", lambda: Dead())
    rc = office.main(["Some task", "--out", str(tmp_path)])
    assert rc == 1 and not any(tmp_path.iterdir())


def test_other_providers_can_be_chosen(monkeypatch, tmp_path):
    seen = {}

    class Local:
        name = "ollama"

        def complete(self, model, prompt, max_tokens=3000):
            seen.setdefault("models", set()).add(model)
            if "You are Atlas," in prompt:
                return json.dumps({"kind": "growth", "team": ["nova", "quill"]}), False
            return "text", False

    monkeypatch.setattr(office, "build_provider", lambda name, base_url=None: Local())
    rc = office.main(["Launch post", "--provider", "ollama", "--model", "big", "--planner-model", "small",
                      "--out", str(tmp_path)])
    assert rc == 0 and seen["models"] == {"big", "small"}


def test_a_relative_out_folder_works_when_code_is_written(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    client = FakeClient({**OK_REPLIES, "Byte": [GOOD]})
    monkeypatch.setattr(office.anthropic, "Anthropic", lambda: client)
    monkeypatch.setattr(office.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert office.main(["Add SSO login with Okta", "--out", "runs"]) == 0
    assert list((tmp_path / "runs").glob("*/code/calc.py"))


def test_a_bad_provider_in_the_environment_is_a_usage_error_not_a_traceback(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_OFFICE_PROVIDER", "ollma")
    with pytest.raises(SystemExit) as e:
        office.main(["task", "--out", str(tmp_path)])
    assert e.value.code == 2 and not any(tmp_path.iterdir())
