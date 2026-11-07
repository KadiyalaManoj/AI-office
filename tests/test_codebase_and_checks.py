import os

from agents.checks import run_checks, safe_env
from agents.codebase import build_context, list_files


def make_repo(root):
    (root / "billing").mkdir()
    (root / "billing/invoice.py").write_text("def total_invoice(lines):\n    return sum(lines)\n")
    (root / "auth").mkdir()
    (root / "auth/login.py").write_text("def login(user):\n    return True  # sso goes here\n")
    (root / "README.md").write_text("# Service\n")
    (root / ".env").write_text("SECRET=1")
    (root / "node_modules").mkdir()
    (root / "node_modules/x.js").write_text("x")
    (root / "terraform.tfstate").write_text("{}")


def test_list_files_skips_secrets_and_vendor_dirs(tmp_path):
    make_repo(tmp_path)
    files = list_files(tmp_path)
    assert "billing/invoice.py" in files and "README.md" in files
    assert not any(f.startswith(("node_modules", ".env")) or "tfstate" in f for f in files)


def test_context_prefers_related_files(tmp_path):
    make_repo(tmp_path)
    tree, ctx = build_context(tmp_path, "Add SSO to login")
    assert "auth/login.py" in tree
    assert "--- auth/login.py ---" in ctx
    assert ctx.index("--- auth/login.py ---") < ctx.index("--- README.md ---")
    assert "SECRET=1" not in ctx


def test_checks_pass_fail_and_missing_tests(tmp_path):
    ok = tmp_path / "ok"
    (ok / "tests").mkdir(parents=True)
    (ok / "m.py").write_text("def f():\n    return 1\n")
    (ok / "tests/test_m.py").write_text("from m import f\n\ndef test_f():\n    assert f() == 1\n")
    assert run_checks(ok)[0] == "PASSED"

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "test_x.py").write_text("def test_x():\n    assert 1 == 2\n")
    status, report = run_checks(bad)
    assert status == "FAILED" and "failed" in report

    none = tmp_path / "none"
    none.mkdir()
    (none / "m.py").write_text("x = 1\n")
    status, report = run_checks(none)
    assert status == "FAILED" and "no tests" in report


def test_secrets_are_removed_from_check_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    env = safe_env()
    assert "ANTHROPIC_API_KEY" not in env and "GITHUB_TOKEN" not in env and "PATH" in env


def test_check_code_cannot_read_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leak-me")
    (tmp_path / "test_env.py").write_text(
        "import os\n\ndef test_no_key():\n    assert 'ANTHROPIC_API_KEY' not in os.environ\n")
    assert run_checks(tmp_path)[0] == "PASSED"
