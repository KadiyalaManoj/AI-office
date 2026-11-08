"""Opt-in automated checks on code the agents wrote: Python (pytest, ruff, mypy), TypeScript (tsc, npm test)
and Terraform (fmt, init, validate in every directory).

This runs model-written code, so it is off unless you pass --run-checks. By default it runs on this machine with
secrets stripped from the environment. With --sandbox docker every command runs in a throwaway container with no
network (only `npm install` and `terraform init` get one), no capabilities and a memory limit.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .codebase import SKIP_DIRS

SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|SESSION)", re.I)
SANDBOX_IMAGE = "ai-office-checks"
SANDBOX_DIR = Path(__file__).parent / "sandbox"
RUFF_RULES = "E9,F63,F7,F82"   # syntax errors and undefined names only, never style
NO_TEST_SCRIPT = "no test specified"
# A repair that rewrites a file with the same length in the same second would otherwise reuse the old .pyc.
NO_BYTECODE = {"PYTHONDONTWRITEBYTECODE": "1"}


def safe_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}


class LocalRunner:
    """Runs checks on this machine."""
    name = "local"
    python = [sys.executable]

    def run(self, cmd: list[str], cwd: Path, timeout: int, network: bool = False,
            env: dict[str, str] | None = None) -> tuple[int, str]:
        try:
            p = subprocess.run(cmd, cwd=cwd, env={**safe_env(), **NO_BYTECODE, **(env or {})}, capture_output=True,
                               text=True, timeout=timeout)
            return p.returncode, (p.stdout + p.stderr)[-3000:]
        except subprocess.TimeoutExpired:
            return 124, f"Timed out after {timeout}s."
        except FileNotFoundError as e:
            return 127, str(e)

    def has_tool(self, tool: str) -> bool:
        return shutil.which(tool) is not None

    def has_module(self, module: str) -> bool:
        return importlib.util.find_spec(module) is not None


class DockerRunner:
    """Runs every command inside a disposable container with the workspace mounted at /work."""
    name = "docker"
    python = ["python"]

    def __init__(self, workspace: Path, image: str = SANDBOX_IMAGE, memory: str = "2g", cpus: str = "2"):
        self.root = workspace.resolve()
        self.image, self.memory, self.cpus = image, memory, cpus
        self._probe: dict[tuple[str, ...], bool] = {}

    def command(self, cmd: list[str], cwd: Path, network: bool = False,
                env: dict[str, str] | None = None, name: str | None = None) -> list[str]:
        rel = cwd.resolve().relative_to(self.root).as_posix()
        out = ["docker", "run", "--rm", *(["--name", name] if name else []),
               "--network", "bridge" if network else "none",
               "--memory", self.memory, "--cpus", self.cpus, "--pids-limit", "512",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--user", f"{os.getuid()}:{os.getgid()}", "--tmpfs", "/tmp", "-e", "HOME=/tmp",
               "-v", f"{self.root}:/work", "-w", "/work" if rel == "." else f"/work/{rel}"]
        for k, v in {**NO_BYTECODE, **(env or {})}.items():
            out += ["-e", f"{k}={v}"]
        return out + [self.image, *cmd]

    def run(self, cmd: list[str], cwd: Path, timeout: int, network: bool = False,
            env: dict[str, str] | None = None) -> tuple[int, str]:
        # A name lets us kill the container on timeout: killing only the docker client would leave it running.
        name = f"ai-office-{uuid.uuid4().hex[:12]}"
        try:
            p = subprocess.run(self.command(cmd, cwd, network, env, name), env=safe_env(), capture_output=True,
                               text=True, timeout=timeout)
            return p.returncode, (p.stdout + p.stderr)[-3000:]
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False)
            return 124, f"Timed out after {timeout}s."
        except FileNotFoundError as e:
            return 127, str(e)

    def _ok(self, cmd: list[str]) -> bool:
        key = tuple(cmd)
        if key not in self._probe:
            self._probe[key] = self.run(cmd, self.root, 60)[0] == 0
        return self._probe[key]

    def has_tool(self, tool: str) -> bool:
        return self._ok(["which", tool])

    def has_module(self, module: str) -> bool:
        return self._ok(["python", "-c", f"import {module}"])

    @staticmethod
    def available() -> bool:
        try:
            return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def ensure_image(image: str = SANDBOX_IMAGE) -> None:
        """Build the sandbox image from agents/sandbox/Dockerfile if it is not there yet."""
        have = subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
        if not have:
            print(f"Building the sandbox image '{image}' (first time only)...")
            subprocess.run(["docker", "build", "-t", image, str(SANDBOX_DIR)], check=True)


def make_runner(sandbox: str, workspace: Path, image: str = SANDBOX_IMAGE):
    if sandbox == "docker":
        return DockerRunner(workspace, image)
    return LocalRunner()


def _files(root: Path, pattern: str) -> list[Path]:
    return [p for p in sorted(root.rglob(pattern))
            if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(root).parts)]


def _fence(out: str) -> str:
    return f"```\n{out}\n```"


def _python(runner, workspace: Path, changed: list[str] | None, timeout: int,
            outcomes: list[str], report: list[str]) -> None:
    if not runner.has_module("pytest"):
        outcomes.append("SKIPPED")
        report.append("pytest: skipped, pytest is not installed (pip install pytest).")
    else:
        rc, out = runner.run([*runner.python, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
                             workspace, timeout)
        if rc == 0:
            outcomes.append("PASSED")
            report.append("pytest: passed.")
        elif rc == 5:
            outcomes.append("FAILED")
            report.append("pytest: no tests were found. Tests are required with every change.")
        else:
            outcomes.append("FAILED")
            report.append(f"pytest: failed (exit {rc}).\n{_fence(out)}")

    # Lint only what the agents wrote, so problems already in your repository never block a run.
    listed = changed if changed is not None else [p.relative_to(workspace).as_posix()
                                                   for p in _files(workspace, "*.py")]
    targets = [c for c in listed if c.endswith(".py")]
    if targets and runner.has_module("ruff"):
        rc, out = runner.run([*runner.python, "-m", "ruff", "check", "--select", RUFF_RULES, "--no-cache",
                              "--output-format", "concise", *targets], workspace, timeout)
        if rc == 0:
            outcomes.append("PASSED")
            report.append("ruff: no syntax errors or undefined names.")
        else:
            outcomes.append("FAILED")
            report.append(f"ruff: found errors (exit {rc}).\n{_fence(out)}")
    if targets and runner.has_module("mypy"):
        rc, out = runner.run([*runner.python, "-m", "mypy", "--ignore-missing-imports", "--no-error-summary",
                              "--cache-dir", os.devnull, *targets], workspace, timeout)
        if rc != 0 and out.strip():
            report.append(f"mypy (advisory, does not block):\n{_fence(out)}")


def _typescript(runner, workspace: Path, timeout: int, outcomes: list[str], report: list[str]) -> None:
    pkg_file = workspace / "package.json"
    if not pkg_file.is_file():
        outcomes.append("SKIPPED")
        report.append("typescript: skipped, there is no package.json in the repository root.")
        return
    if not runner.has_tool("npm"):
        outcomes.append("SKIPPED")
        report.append("typescript: skipped, npm is not installed.")
        return
    try:
        scripts = json.loads(pkg_file.read_text()).get("scripts") or {}
    except (ValueError, OSError):
        scripts = {}
    rc, out = runner.run(["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"], workspace,
                         timeout, network=True)
    if rc != 0:
        outcomes.append("FAILED")
        report.append(f"npm install: failed (exit {rc}).\n{_fence(out)}")
        return
    ran = False
    if (workspace / "tsconfig.json").is_file():
        ran = True
        rc, out = runner.run(["npx", "--no-install", "tsc", "--noEmit"], workspace, timeout)
        outcomes.append("PASSED" if rc == 0 else "FAILED")
        report.append("tsc: passed." if rc == 0 else f"tsc: type errors (exit {rc}).\n{_fence(out)}")
    if scripts.get("test") and NO_TEST_SCRIPT not in scripts["test"]:
        ran = True
        rc, out = runner.run(["npm", "test"], workspace, timeout, env={"CI": "1"})
        outcomes.append("PASSED" if rc == 0 else "FAILED")
        report.append("npm test: passed." if rc == 0 else f"npm test: failed (exit {rc}).\n{_fence(out)}")
    if not ran:
        outcomes.append("SKIPPED")
        report.append("typescript: skipped, no tsconfig.json and no test script in package.json.")


def _terraform(runner, workspace: Path, tf: list[Path], timeout: int,
               outcomes: list[str], report: list[str]) -> None:
    if not runner.has_tool("terraform"):
        outcomes.append("SKIPPED")
        report.append("terraform: skipped, terraform is not installed.")
        return
    rc, out = runner.run(["terraform", "fmt", "-check", "-recursive", "-no-color"], workspace, timeout)
    fmt_ok = rc == 0
    report.append("terraform fmt: passed." if fmt_ok else f"terraform fmt: files need formatting.\n{_fence(out)}")
    failed = not fmt_ok
    for d in sorted({p.parent for p in tf}):
        label = d.relative_to(workspace).as_posix()
        label = "." if label == "" else label
        rc_i, out_i = runner.run(["terraform", "init", "-backend=false", "-input=false", "-no-color"], d,
                                 timeout, network=True)
        if rc_i != 0:
            failed = True
            report.append(f"terraform init ({label}): failed (exit {rc_i}).\n{_fence(out_i)}")
            continue
        rc_v, out_v = runner.run(["terraform", "validate", "-no-color"], d, timeout)
        if rc_v != 0:
            failed = True
        report.append(f"terraform validate ({label}): passed." if rc_v == 0
                      else f"terraform validate ({label}): failed.\n{_fence(out_v)}")
    outcomes.append("FAILED" if failed else "PASSED")


def run_checks(workspace: Path, timeout: int = 180, runner=None,
               changed: list[str] | None = None) -> tuple[str, str]:
    """Return (status, report). status is PASSED, FAILED or SKIPPED.

    `changed` lists the files the agents wrote (relative to the workspace); linting is limited to them.
    """
    runner = runner or LocalRunner()
    py, tf = _files(workspace, "*.py"), _files(workspace, "*.tf")
    ts = _files(workspace, "*.ts") + _files(workspace, "*.tsx")
    if changed is not None:     # only check TypeScript the agents wrote, not what was already in the repo
        ts = [p for p in ts if p.relative_to(workspace).as_posix() in changed]
    outcomes: list[str] = []
    report: list[str] = []

    if py:
        _python(runner, workspace, changed, timeout, outcomes, report)
    if ts:
        _typescript(runner, workspace, timeout, outcomes, report)
    if tf:
        _terraform(runner, workspace, tf, timeout, outcomes, report)

    if not outcomes:
        return "SKIPPED", "No Python, TypeScript or Terraform files to check."
    status = "FAILED" if "FAILED" in outcomes else "PASSED" if "PASSED" in outcomes else "SKIPPED"
    return status, "\n\n".join(report)
