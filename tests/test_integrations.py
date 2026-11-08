"""Local GitHub pull requests and email, with fake `gh`/git and SMTP so nothing real is touched."""

import smtplib
import subprocess
from types import SimpleNamespace

import pytest

from agents.integrations import IntegrationError, Integrations, load_env_file

CODE = "File: app/calc.py\n```python\ndef add(a, b):\n    return a + b\n```\n"
WORKFLOW = "File: .github/workflows/evil.yml\n```yaml\non: pull_request\n```\n"


class FakeSMTP:
    sent = []
    fail = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        self.user, self.password = user, password

    def send_message(self, msg):
        FakeSMTP.sent.append((self.host, self.port, self.user, self.password, msg))


@pytest.fixture(autouse=True)
def reset_smtp():
    FakeSMTP.sent, FakeSMTP.fail = [], None


def mail_config(**kw):
    return Integrations(smtp_user="me@gmail.com", smtp_password="app-pass", **kw)


def test_email_is_sent_through_smtp_with_the_app_password():
    to = mail_config().send_email("", "Proposal: X", "Body text", smtp_factory=FakeSMTP)
    host, port, user, password, msg = FakeSMTP.sent[0]
    assert to == "me@gmail.com" and (host, port) == ("smtp.gmail.com", 465)
    assert (user, password) == ("me@gmail.com", "app-pass")
    assert msg["To"] == "me@gmail.com" and msg["Subject"] == "Proposal: X" and "Body text" in msg.get_content()


def test_mail_can_only_go_to_allowed_addresses():
    cfg = mail_config(email_to=["boss@work.com"])
    assert cfg.send_email("BOSS@work.com", "s", "b", smtp_factory=FakeSMTP) == "BOSS@work.com"
    with pytest.raises(IntegrationError, match="can only go to"):
        cfg.send_email("stranger@evil.com", "s", "b", smtp_factory=FakeSMTP)
    assert len(FakeSMTP.sent) == 1


def test_subject_cannot_inject_headers():
    mail_config().send_email("me@gmail.com", "Hi\r\nBcc: x@evil.com", "b", smtp_factory=FakeSMTP)
    msg = FakeSMTP.sent[0][4]
    assert msg["Bcc"] is None and "\n" not in msg["Subject"] and "\r" not in msg["Subject"]


def test_email_without_setup_explains_how_to_set_it_up():
    with pytest.raises(IntegrationError, match="AI_OFFICE_SMTP_PASSWORD"):
        Integrations().send_email("", "s", "b", smtp_factory=FakeSMTP)


def test_smtp_failures_become_readable_errors():
    FakeSMTP.fail = smtplib.SMTPAuthenticationError(535, b"bad")
    with pytest.raises(IntegrationError, match="App Password"):
        mail_config().send_email("", "s", "b", smtp_factory=FakeSMTP)
    FakeSMTP.fail = OSError("network down")
    with pytest.raises(IntegrationError, match="network down"):
        mail_config().send_email("", "s", "b", smtp_factory=FakeSMTP)


def test_settings_come_from_the_environment_and_env_file(monkeypatch, tmp_path):
    for k in ("AI_OFFICE_SMTP_USER", "AI_OFFICE_SMTP_PASSWORD", "AI_OFFICE_EMAIL_TO", "AI_OFFICE_GITHUB_REPOS"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("AI_OFFICE_SMTP_USER=me@gmail.com\nAI_OFFICE_SMTP_PASSWORD='abcd efgh ijkl mnop'\n"
                   "AI_OFFICE_GITHUB_REPOS=a/b, c/d\nOTHER_SECRET=nope\nAI_OFFICE_EMAIL_TO=x@y.com\n")
    monkeypatch.setenv("AI_OFFICE_EMAIL_TO", "real@env.com")      # the real environment wins
    load_env_file(env)
    cfg = Integrations.from_env()
    assert cfg.smtp_user == "me@gmail.com" and cfg.smtp_password == "abcdefghijklmnop"
    assert cfg.repos == ["a/b", "c/d"] and cfg.email_to == ["real@env.com"]
    import os
    assert "OTHER_SECRET" not in os.environ
    for k in ("AI_OFFICE_SMTP_USER", "AI_OFFICE_SMTP_PASSWORD", "AI_OFFICE_GITHUB_REPOS"):
        monkeypatch.delenv(k, raising=False)


class FakeGit:
    """Records commands and answers like gh/git would."""

    def __init__(self, fail=None, status=" A app/calc.py", pr="https://github.com/me/repo/pull/7\n", signed_in=True):
        self.calls, self.fail, self.status, self.pr, self.signed_in = [], fail or {}, status, pr, signed_in

    def __call__(self, cmd, cwd=None, capture_output=True, text=True, timeout=None):
        self.calls.append((cmd, cwd))
        line = " ".join(cmd)
        if cmd[1:3] == ["auth", "status"]:
            return SimpleNamespace(returncode=0 if self.signed_in else 1, stdout="", stderr="")
        for key, (rc, err) in self.fail.items():
            if key in line:
                return SimpleNamespace(returncode=rc, stdout="", stderr=err)
        out = {"rev-parse": "main\n", "status --porcelain": self.status, "pr create": self.pr}
        return SimpleNamespace(returncode=0, stdout=next((v for k, v in out.items() if k in line), ""), stderr="")

    def commands(self):
        return [" ".join(c[0][:3]) if c[0][0] == "git" else " ".join(c[0][1:4]) for c in self.calls]


def ship(run, texts=(CODE,), repo="me/repo", **kw):
    cfg = Integrations(repos=["me/repo"])
    return cfg.open_pull_request(repo, "Add calculator", list(texts), run=run, which=lambda n: "/usr/bin/gh", **kw)


def test_pull_request_flow():
    run = FakeGit()
    result = ship(run)
    assert result["prUrl"] == "https://github.com/me/repo/pull/7"
    assert result["files"] == ["app/calc.py"] and result["branch"].startswith("office/add-calculator-")
    assert run.commands() == ["auth status", "repo clone me/repo", "git rev-parse --abbrev-ref", "git checkout -b",
                              "git add -A", "git status --porcelain", "git commit -m", "git push -u",
                              "pr create --repo"]
    push = next(c for c, _ in run.calls if c[:2] == ["git", "push"])
    assert push[-1] == result["branch"]
    create = next(c for c, _ in run.calls if c[1:3] == ["pr", "create"])
    assert create[create.index("--base") + 1] == "main"


def test_the_agents_files_are_what_lands_on_the_branch(tmp_path):
    seen = {}

    class Capturing(FakeGit):
        def __call__(self, cmd, cwd=None, **kw):
            if cmd[:2] == ["git", "add"]:
                seen["files"] = sorted(p.relative_to(cwd).as_posix() for p in cwd.rglob("*") if p.is_file())
            if cmd[1:3] == ["repo", "clone"]:
                (tmp_path / "x").mkdir(exist_ok=True)
                from pathlib import Path
                Path(cmd[4]).mkdir(parents=True)
            return super().__call__(cmd, cwd, **kw)

    ship(Capturing())
    assert seen["files"] == ["app/calc.py"]


def test_later_drafts_replace_earlier_ones():
    seen = {}

    class Capturing(FakeGit):
        def __call__(self, cmd, cwd=None, **kw):
            if cmd[1:3] == ["repo", "clone"]:
                from pathlib import Path
                Path(cmd[4]).mkdir(parents=True)
            if cmd[:2] == ["git", "add"]:
                seen["code"] = (cwd / "app/calc.py").read_text()
            return super().__call__(cmd, cwd, **kw)

    ship(Capturing(), texts=[CODE, CODE.replace("a + b", "b + a")])
    assert "b + a" in seen["code"]


def test_workflow_and_git_files_are_never_sent():
    with pytest.raises(IntegrationError, match="not written any files"):
        ship(FakeGit(), texts=[WORKFLOW])
    run = FakeGit()
    assert ship(run, texts=[CODE + WORKFLOW])["files"] == ["app/calc.py"]


def test_repositories_outside_the_allow_list_are_refused():
    run = FakeGit()
    with pytest.raises(IntegrationError, match="isn't an allowed repository"):
        ship(run, repo="someone/else")
    with pytest.raises(IntegrationError, match="owner/name"):
        ship(run, repo="../../etc")
    assert run.calls == []


def test_problems_are_reported_in_plain_words():
    with pytest.raises(IntegrationError, match="gh auth login"):
        ship(FakeGit(signed_in=False))
    with pytest.raises(IntegrationError, match="push.*failed.*rejected"):
        ship(FakeGit(fail={"git push": (1, "rejected")}))
    with pytest.raises(IntegrationError, match="already in the repository"):
        ship(FakeGit(status=""))
    with pytest.raises(IntegrationError, match="no pull request link"):
        ship(FakeGit(pr="done\n"))
    with pytest.raises(IntegrationError, match="isn't installed"):
        Integrations(repos=["me/repo"]).open_pull_request("me/repo", "t", [CODE], run=FakeGit(),
                                                          which=lambda n: None)


def test_a_hung_command_times_out_cleanly():
    class Hang(FakeGit):
        def __call__(self, cmd, cwd=None, **kw):
            if cmd[1:3] == ["repo", "clone"]:
                raise subprocess.TimeoutExpired(cmd, 1)
            return super().__call__(cmd, cwd, **kw)

    with pytest.raises(IntegrationError, match="took longer"):
        ship(Hang())


def test_status_explains_what_is_missing():
    cfg = Integrations(repos=["me/repo"])
    none = SimpleNamespace(returncode=1)
    s = cfg.status(which=lambda n: None, run=lambda *a, **k: none)
    assert not s["github"]["ready"] and "brew install gh" in s["github"]["reason"]
    s = cfg.status(which=lambda n: "/x/gh", run=lambda *a, **k: none)
    assert "gh auth login" in s["github"]["reason"]
    ok = SimpleNamespace(returncode=0)
    assert cfg.status(which=lambda n: "/x/gh", run=lambda *a, **k: ok)["github"]["ready"]
    assert "AI_OFFICE_SMTP_USER" in cfg.status()["email"]["reason"]
    assert mail_config().status()["email"] == {"ready": True, "to": ["me@gmail.com"], "reason": ""}
