"""The wider QA checks (ruff, TypeScript, every Terraform directory) and the Docker sandbox, with fake runners."""

import os
from pathlib import Path

import pytest

from agents.checks import DockerRunner, LocalRunner, make_runner, run_checks


class FakeRunner:
    """Records every command and answers from a table of (substring -> (rc, output))."""
    python = ["python"]

    def __init__(self, answers=None, tools=("terraform", "npm"), modules=("pytest", "ruff", "mypy")):
        self.answers, self.tools, self.modules, self.calls = answers or {}, set(tools), set(modules), []

    def run(self, cmd, cwd, timeout, network=False, env=None):
        self.calls.append((cmd, Path(cwd), network, env))
        line = " ".join(cmd)
        return next((v for k, v in self.answers.items() if k in line), (0, ""))

    def has_tool(self, tool):
        return tool in self.tools

    def has_module(self, module):
        return module in self.modules


def line(runner, text):
    return [c for c in runner.calls if text in " ".join(c[0])]


def test_terraform_is_validated_in_every_directory(tmp_path):
    for d in ("network", "database"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "main.tf").write_text("# tf\n")
    runner = FakeRunner({"validate": (0, "")})
    status, report = run_checks(tmp_path, runner=runner)
    assert status == "PASSED"
    assert {c[1].name for c in line(runner, "terraform validate")} == {"network", "database"}
    assert "terraform validate (database): passed." in report
    assert all(c[2] for c in line(runner, "terraform init"))             # init may use the network...
    assert not any(c[2] for c in line(runner, "terraform validate"))     # ...validate never


def test_one_bad_terraform_directory_fails_the_run(tmp_path):
    for d in ("a", "b"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "main.tf").write_text("# tf\n")
    runner = FakeRunner({"validate": (1, "bad")})
    status, report = run_checks(tmp_path, runner=runner)
    assert status == "FAILED" and "terraform validate (a): failed" in report and "(b)" in report


def test_ruff_lints_only_the_files_the_agents_wrote(tmp_path):
    (tmp_path / "old.py").write_text("x = 1\n")
    (tmp_path / "new.py").write_text("x = 2\n")
    runner = FakeRunner()
    run_checks(tmp_path, runner=runner, changed=["new.py", "notes.md"])
    ruff = line(runner, "-m ruff")[0][0]
    assert ruff[-1] == "new.py" and "old.py" not in ruff and "notes.md" not in ruff
    assert "E9,F63,F7,F82" in ruff


def test_ruff_errors_fail_the_run_but_mypy_is_only_advice(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    runner = FakeRunner({"-m ruff": (1, "a.py:1:1: F821 undefined name"), "-m mypy": (1, "a.py: error")})
    status, report = run_checks(tmp_path, runner=runner, changed=["a.py"])
    assert status == "FAILED" and "F821" in report and "mypy (advisory" in report

    runner = FakeRunner({"-m mypy": (1, "a.py: error")})
    status, report = run_checks(tmp_path, runner=runner, changed=["a.py"])
    assert status == "PASSED" and "mypy (advisory" in report


def test_missing_linters_are_skipped_quietly(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    runner = FakeRunner(modules=("pytest",))
    status, report = run_checks(tmp_path, runner=runner, changed=["a.py"])
    assert status == "PASSED" and not line(runner, "ruff")


def test_typescript_needs_a_package_json(tmp_path):
    (tmp_path / "App.tsx").write_text("export const A = 1;\n")
    status, report = run_checks(tmp_path, runner=FakeRunner())
    assert status == "SKIPPED" and "no package.json" in report


def test_typescript_runs_install_without_scripts_then_tsc_and_tests(tmp_path):
    (tmp_path / "App.tsx").write_text("export const A = 1;\n")
    (tmp_path / "tsconfig.json").write_text("{}")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    runner = FakeRunner()
    status, report = run_checks(tmp_path, runner=runner)
    assert status == "PASSED" and "tsc: passed." in report and "npm test: passed." in report
    install = line(runner, "npm install")[0]
    assert "--ignore-scripts" in install[0] and install[2] is True
    assert line(runner, "npm test")[0][3] == {"CI": "1"}


def test_typescript_failures_are_reported(tmp_path):
    (tmp_path / "App.tsx").write_text("export const A = 1;\n")
    (tmp_path / "tsconfig.json").write_text("{}")
    (tmp_path / "package.json").write_text("{}")
    status, report = run_checks(tmp_path, runner=FakeRunner({"tsc": (2, "TS2322")}))
    assert status == "FAILED" and "TS2322" in report

    status, report = run_checks(tmp_path, runner=FakeRunner({"npm install": (1, "ERESOLVE")}))
    assert status == "FAILED" and "npm install: failed" in report


def test_placeholder_npm_test_script_does_not_count_as_tests(tmp_path):
    (tmp_path / "App.tsx").write_text("export const A = 1;\n")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}')
    runner = FakeRunner()
    status, _ = run_checks(tmp_path, runner=runner)
    assert status == "SKIPPED" and not line(runner, "npm test")


def test_vendored_folders_are_not_checked(tmp_path):
    (tmp_path / "node_modules/pkg").mkdir(parents=True)
    (tmp_path / "node_modules/pkg/x.py").write_text("x = 1\n")
    assert run_checks(tmp_path, runner=FakeRunner())[0] == "SKIPPED"


def test_docker_sandbox_command_is_locked_down(tmp_path):
    runner = DockerRunner(tmp_path, image="img")
    sub = tmp_path / "infra"
    sub.mkdir()
    cmd = runner.command(["terraform", "validate"], sub)
    assert cmd[:3] == ["docker", "run", "--rm"] and cmd[-3:] == ["img", "terraform", "validate"]
    assert cmd[cmd.index("--network") + 1] == "none"
    assert "--cap-drop" in cmd and cmd[cmd.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in cmd and "--pids-limit" in cmd and "--memory" in cmd
    assert cmd[cmd.index("-w") + 1] == "/work/infra"
    assert f"{tmp_path.resolve()}:/work" in cmd
    assert cmd[cmd.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"


def test_docker_sandbox_only_gets_a_network_when_asked_and_passes_no_host_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leak-me")
    runner = DockerRunner(tmp_path)
    cmd = runner.command(["npm", "install"], tmp_path, network=True, env={"CI": "1"})
    assert cmd[cmd.index("--network") + 1] == "bridge" and "CI=1" in cmd
    assert cmd[cmd.index("-w") + 1] == "/work"
    assert not any("leak-me" in part or "ANTHROPIC" in part for part in cmd)


def test_docker_runner_probes_tools_inside_the_container(tmp_path, monkeypatch):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return type("P", (), {"returncode": 0 if cmd[-1] == "terraform" else 1, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("agents.checks.subprocess.run", fake_run)
    runner = DockerRunner(tmp_path)
    assert runner.has_tool("terraform") and not runner.has_tool("npm")
    assert runner.has_tool("terraform") and len(seen) == 2          # answers are cached


def test_runner_choice(tmp_path):
    assert isinstance(make_runner("none", tmp_path), LocalRunner)
    assert isinstance(make_runner("docker", tmp_path), DockerRunner)


def test_sandbox_flag_requires_run_checks_and_a_running_docker(monkeypatch, tmp_path):
    import agents.office as office
    with pytest.raises(SystemExit):
        office.main(["task", "--sandbox", "docker", "--out", str(tmp_path)])
    monkeypatch.setattr(office.DockerRunner, "available", staticmethod(lambda: False))
    with pytest.raises(SystemExit):
        office.main(["task", "--run-checks", "--sandbox", "docker", "--out", str(tmp_path)])
    assert not any(tmp_path.iterdir())


def test_a_step_that_wrote_no_files_never_lints_the_whole_repository(tmp_path):
    (tmp_path / "old.py").write_text("x = 1\n")
    runner = FakeRunner()
    run_checks(tmp_path, runner=runner, changed=[])
    assert not line(runner, "-m ruff")


def test_typescript_already_in_the_repo_is_not_checked_unless_the_agents_wrote_some(tmp_path):
    (tmp_path / "old.tsx").write_text("export const A = 1;\n")
    (tmp_path / "new.py").write_text("x = 1\n")
    runner = FakeRunner()
    run_checks(tmp_path, runner=runner, changed=["new.py"])
    assert not line(runner, "npm")
    (tmp_path / "package.json").write_text("{}")
    run_checks(tmp_path, runner=runner, changed=["old.tsx"])
    assert line(runner, "npm install")


def test_a_failed_sandbox_image_build_is_a_clean_error(monkeypatch, tmp_path):
    import subprocess

    import agents.office as office

    def boom(image):
        raise subprocess.CalledProcessError(1, "docker build")

    monkeypatch.setattr(office.DockerRunner, "available", staticmethod(lambda: True))
    monkeypatch.setattr(office.DockerRunner, "ensure_image", staticmethod(boom))
    with pytest.raises(SystemExit):
        office.main(["task", "--run-checks", "--sandbox", "docker", "--out", str(tmp_path)])
    assert not any(tmp_path.iterdir())


def test_a_timed_out_container_is_killed_not_left_running(tmp_path, monkeypatch):
    import subprocess

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == "run":
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("agents.checks.subprocess.run", fake_run)
    rc, out = DockerRunner(tmp_path).run(["pytest"], tmp_path, 5)
    assert rc == 124 and "Timed out" in out
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[1] == ["docker", "rm", "-f", name]


def test_every_container_gets_its_own_name(tmp_path, monkeypatch):
    names = []

    def fake_run(cmd, **kw):
        names.append(cmd[cmd.index("--name") + 1])
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("agents.checks.subprocess.run", fake_run)
    runner = DockerRunner(tmp_path)
    runner.run(["a"], tmp_path, 5)
    runner.run(["b"], tmp_path, 5)
    assert len(set(names)) == 2 and all(n.startswith("ai-office-") for n in names)
