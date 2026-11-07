"""Opt-in automated checks on code the agents wrote: pytest for Python, fmt/validate for Terraform.

This runs model-written code on the machine you run it on, so it is off unless you pass --run-checks.
Use a container or throwaway VM for it. Secrets are stripped from the environment before anything runs.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|SESSION)", re.I)


def safe_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}


def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, env=safe_env(), capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)[-3000:]
    except subprocess.TimeoutExpired:
        return 124, f"Timed out after {timeout}s."
    except FileNotFoundError as e:
        return 127, str(e)


def run_checks(workspace: Path, timeout: int = 180) -> tuple[str, str]:
    """Return (status, report). status is PASSED, FAILED or SKIPPED."""
    py = [p for p in workspace.rglob("*.py")]
    tf = [p for p in workspace.rglob("*.tf")]
    outcomes: list[str] = []
    report: list[str] = []

    if py:
        if importlib.util.find_spec("pytest") is None:
            outcomes.append("SKIPPED")
            report.append("pytest: skipped, pytest is not installed (pip install pytest).")
        else:
            rc, out = _run([sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
                           workspace, timeout)
            if rc == 0:
                outcomes.append("PASSED")
                report.append("pytest: passed.")
            elif rc == 5:
                outcomes.append("FAILED")
                report.append("pytest: no tests were found. Tests are required with every change.")
            else:
                outcomes.append("FAILED")
                report.append(f"pytest: failed (exit {rc}).\n```\n{out}\n```")

    if tf:
        if shutil.which("terraform") is None:
            outcomes.append("SKIPPED")
            report.append("terraform: skipped, terraform is not installed.")
        else:
            tf_dir = tf[0].parent
            rc, out = _run(["terraform", "fmt", "-check", "-recursive", "-no-color"], workspace, timeout)
            fmt_ok = rc == 0
            report.append("terraform fmt: passed." if fmt_ok else f"terraform fmt: files need formatting.\n```\n{out}\n```")
            rc_i, out_i = _run(["terraform", "init", "-backend=false", "-input=false", "-no-color"], tf_dir, timeout)
            if rc_i != 0:
                outcomes.append("FAILED")
                report.append(f"terraform init: failed (exit {rc_i}).\n```\n{out_i}\n```")
            else:
                rc_v, out_v = _run(["terraform", "validate", "-no-color"], tf_dir, timeout)
                outcomes.append("PASSED" if rc_v == 0 and fmt_ok else "FAILED")
                report.append("terraform validate: passed." if rc_v == 0 else f"terraform validate: failed.\n```\n{out_v}\n```")

    if not outcomes:
        return "SKIPPED", "No Python or Terraform files to check."
    status = "FAILED" if "FAILED" in outcomes else "PASSED" if "PASSED" in outcomes else "SKIPPED"
    return status, "\n\n".join(report)
