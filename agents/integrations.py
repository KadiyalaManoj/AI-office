"""Local replacements for the claude.ai connectors: open a GitHub pull request with `gh`, send mail over SMTP.

Credentials stay on this machine. Mail settings come from AI_OFFICE_SMTP_* environment variables (or a `.env`
file next to the project), GitHub access from your own `gh auth login`. The browser page never sees either.
"""

from __future__ import annotations

import os
import re
import shutil
import smtplib
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

from .pipeline import extract_files, slugify

REPO_RE = re.compile(r"[\w.-]+/[\w.-]+")
PR_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
BLOCKED_DIRS = {".git", ".github"}      # a pull request must never carry its own CI workflow or git internals
STEP_TIMEOUT = 180


class IntegrationError(RuntimeError):
    """A problem the page should show to the user as written."""


def load_env_file(path: Path) -> None:
    """Read AI_OFFICE_* settings from a .env file without overriding the real environment."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = raw.strip().partition("=")
        key = key.strip()
        if sep and key.startswith("AI_OFFICE_") and key not in os.environ:
            os.environ[key] = value.strip().strip("'\"")


def origin_repo(project: Path) -> str | None:
    """owner/name of this project's own `origin` remote, if it has a GitHub one."""
    try:
        url = subprocess.run(["git", "-C", str(project), "remote", "get-url", "origin"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


@dataclass
class Integrations:
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    email_to: list[str] = field(default_factory=list)      # extra addresses mail may go to
    repos: list[str] = field(default_factory=list)         # the only repositories code may be pushed to

    @classmethod
    def from_env(cls, project: Path | None = None, repos: list[str] | None = None) -> Integrations:
        env = os.environ
        allowed = list(repos or []) + [r for r in env.get("AI_OFFICE_GITHUB_REPOS", "").split(",") if r.strip()]
        own = origin_repo(project) if project else None
        if own:
            allowed.append(own)
        return cls(
            smtp_user=env.get("AI_OFFICE_SMTP_USER", "").strip(),
            smtp_password=env.get("AI_OFFICE_SMTP_PASSWORD", "").replace(" ", ""),   # Google shows it in groups
            smtp_host=env.get("AI_OFFICE_SMTP_HOST", "smtp.gmail.com"),
            smtp_port=int(env.get("AI_OFFICE_SMTP_PORT", "465")),
            email_to=[a.strip() for a in env.get("AI_OFFICE_EMAIL_TO", "").split(",") if a.strip()],
            repos=sorted({r.strip() for r in allowed if REPO_RE.fullmatch(r.strip())}),
        )

    # ---------- status, so the page can explain what is missing ----------
    def status(self, which=shutil.which, run: Callable = subprocess.run) -> dict:
        gh = which("gh")
        signed_in = bool(gh) and run([gh, "auth", "status"], capture_output=True, timeout=30).returncode == 0
        return {
            "github": {"ready": bool(gh) and signed_in and bool(self.repos),
                       "repos": self.repos,
                       "reason": "" if gh and signed_in and self.repos else
                       "Install the GitHub CLI (brew install gh)." if not gh else
                       "Sign in with: gh auth login" if not signed_in else
                       "No repository is allowed. Pass --github-repo owner/name."},
            "email": {"ready": bool(self.smtp_user and self.smtp_password),
                      "to": self.recipients(),
                      "reason": "" if self.smtp_user and self.smtp_password else
                      "Set AI_OFFICE_SMTP_USER and AI_OFFICE_SMTP_PASSWORD (a Gmail App Password) in .env."},
        }

    # ---------- email ----------
    def recipients(self) -> list[str]:
        return [a for a in dict.fromkeys([self.smtp_user, *self.email_to]) if a]

    def send_email(self, to: str, subject: str, body: str,
                   smtp_factory: Callable = smtplib.SMTP_SSL) -> str:
        if not (self.smtp_user and self.smtp_password):
            raise IntegrationError("Email isn't set up. Put AI_OFFICE_SMTP_USER and AI_OFFICE_SMTP_PASSWORD "
                                   "(a Gmail App Password from myaccount.google.com/apppasswords) in .env, "
                                   "then restart the server.")
        to = (to or "").strip() or self.smtp_user
        if to.lower() not in {a.lower() for a in self.recipients()}:
            raise IntegrationError(f"Mail can only go to {', '.join(self.recipients())}. "
                                   "Add other addresses with AI_OFFICE_EMAIL_TO.")
        msg = EmailMessage()
        msg["From"], msg["To"] = self.smtp_user, to
        msg["Subject"] = re.sub(r"[\r\n]+", " ", subject)[:200]        # no header injection
        msg.set_content(body[:20_000])
        try:
            with smtp_factory(self.smtp_host, self.smtp_port, timeout=30) as smtp:
                smtp.login(self.smtp_user, self.smtp_password)
                smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            raise IntegrationError("Gmail refused the login. Use an App Password, not your normal password "
                                   "(it needs 2-Step Verification).") from e
        except (smtplib.SMTPException, OSError) as e:
            raise IntegrationError(f"Could not send the email: {e}") from e
        return to

    # ---------- GitHub ----------
    def open_pull_request(self, repo: str, title: str, texts: list[str], run: Callable = subprocess.run,
                          which=shutil.which) -> dict:
        """Clone `repo`, put the agents' files on a new branch, push it and open a pull request."""
        if not REPO_RE.fullmatch(repo or ""):
            raise IntegrationError("The repository must look like owner/name.")
        if repo not in self.repos:
            raise IntegrationError(f"{repo} isn't an allowed repository. Allowed: "
                                   f"{', '.join(self.repos) or 'none'}. Add one with --github-repo owner/name.")
        gh = which("gh")
        if not gh:
            raise IntegrationError("The GitHub CLI isn't installed. Run: brew install gh")

        def step(cmd: list[str], cwd: Path | None = None) -> str:
            try:
                p = run(cmd, cwd=cwd, capture_output=True, text=True, timeout=STEP_TIMEOUT)
            except subprocess.TimeoutExpired as e:
                raise IntegrationError(f"`{' '.join(cmd[:3])}` took longer than {STEP_TIMEOUT}s.") from e
            if p.returncode != 0:
                raise IntegrationError(f"`{' '.join(cmd[:3])}` failed: {(p.stderr or p.stdout).strip()[-400:]}")
            return (p.stdout or "").strip()

        with tempfile.TemporaryDirectory(prefix="ai-office-ship-") as tmp:
            work = Path(tmp)
            staged = work / "files"
            staged.mkdir()
            saved: set[str] = set()
            for text in texts:
                got = extract_files(text, staged)
                saved.update(f.relative_to(staged.resolve()).as_posix() for f in got.saved)
            kept = sorted(p for p in saved if not BLOCKED_DIRS & set(Path(p).parts))
            if not kept:
                raise IntegrationError("The agents have not written any files to send.")

            if run([gh, "auth", "status"], capture_output=True, timeout=30).returncode != 0:
                raise IntegrationError("GitHub isn't signed in. Run: gh auth login")
            clone = work / "clone"
            step([gh, "repo", "clone", repo, str(clone), "--", "--depth", "1"])
            base = step(["git", "rev-parse", "--abbrev-ref", "HEAD"], clone)
            branch = f"office/{slugify(title)[:40]}-{uuid.uuid4().hex[:6]}"
            step(["git", "checkout", "-b", branch], clone)
            for rel in kept:
                dest = clone / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(staged / rel, dest)
            step(["git", "add", "-A"], clone)
            if not step(["git", "status", "--porcelain"], clone):
                raise IntegrationError("Those files are already in the repository. Nothing to send.")
            step(["git", "commit", "-m", title[:100], "-m",
                  "Written by the AI Office agents. Review it before merging."], clone)
            step(["git", "push", "-u", "origin", branch], clone)
            body = ("Written by the AI Office agents and reviewed by Lens. A human must review it before merging.\n\n"
                    "Files:\n" + "\n".join(f"- `{p}`" for p in kept))
            out = step([gh, "pr", "create", "--repo", repo, "--head", branch, "--base", base,
                        "--title", title[:100], "--body", body], clone)
        url = PR_RE.search(out)
        if not url:
            raise IntegrationError(f"The branch {branch} was pushed, but no pull request link came back.")
        return {"prUrl": url.group(0), "branch": branch, "files": kept}
