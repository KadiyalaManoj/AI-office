"""Run a task through the AI Office team from the command line.

    python -m agents.office "Add SSO login with Okta" --repo ../my-service --run-checks

Atlas plans the team, each agent writes its deliverable from the work before it, Lens can send code
back once for changes, and the run stops for your approval before Relay writes the release plan.
Everything lands in runs/<date>-<slug>/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
from pathlib import Path

import anthropic

from .checks import run_checks
from .codebase import SKIP_DIRS, build_context
from .pipeline import (CHECKS, CODERS, extract_files, gate_summary, lens_verdict, planner_prompt,
                       slugify, step_prompt, unique_dir, valid_team)
from .roles import AGENTS, DEFAULT_COMPANY, DEFAULT_PIPELINES

DEFAULT_MODEL = os.environ.get("AI_OFFICE_MODEL", "claude-sonnet-5-5")
PLANNER_MODEL = os.environ.get("AI_OFFICE_PLANNER_MODEL", "claude-haiku-5-5")
CUT_OFF_NOTE = "\n\n_(This reply hit the length limit and was cut off.)_"


def ask(client, model: str, prompt: str, max_tokens: int = 3000) -> tuple[str, bool]:
    """Return (text, truncated)."""
    msg = client.messages.create(model=model, max_tokens=max_tokens,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    return text, getattr(msg, "stop_reason", None) == "max_tokens"


def parse_json(text: str) -> dict:
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    body = fence.group(1) if fence else text[text.find("{"): text.rfind("}") + 1]
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def plan(client, company: str, title: str, tree: str = "") -> dict:
    text, _ = ask(client, PLANNER_MODEL, planner_prompt(company, title, tree), 800)
    p = parse_json(text)
    kind = p.get("kind") if p.get("kind") in DEFAULT_PIPELINES else "feature"
    priority = p.get("priority") if re.fullmatch(r"P[0-3]", str(p.get("priority"))) else "P2"
    return {"kind": kind, "priority": priority, "brief": str(p.get("brief") or title)[:600],
            "team": valid_team(p.get("team"), kind)}


def approve_release(args, summary, run_dir: Path) -> bool:
    print("\nRelease gate")
    for line in summary.lines():
        print("  " + line)
    if args.approve:
        if summary.blockers:
            print("--approve only releases when there are no blockers. Stopping here.")
            return False
        print("Approved by --approve.")
        return True
    if not sys.stdin.isatty():
        print("Waiting for your approval. Review the run, then rerun interactively or with --approve.")
        return False
    try:
        if summary.blockers:
            answer = input(f"Blockers above. Review {run_dir}. Type RELEASE to approve anyway: ").strip()
            return answer == "RELEASE"
        return input(f"Review {run_dir}. Approve the release? [y/N]: ").strip().lower() == "y"
    except EOFError:
        return False


def prepare_workspace(repo: Path | None, run_dir: Path) -> Path:
    """Where checks run: a copy of the repository with the agents' files on top, or just their files."""
    ws = run_dir / "workspace"
    if repo:
        shutil.copytree(repo, ws, ignore=shutil.ignore_patterns(*SKIP_DIRS, ".env*"))
    else:
        ws.mkdir(parents=True)
    return ws


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run a task through the AI Office agent team.")
    ap.add_argument("task", help="What you want done, in plain English")
    ap.add_argument("--company", type=Path, help="Text file with your company context")
    ap.add_argument("--repo", type=Path, help="Existing repository the agents should read and fit into")
    ap.add_argument("--run-checks", action="store_true",
                    help="Run pytest / terraform validate on the agents' code. Runs model-written code: use a container.")
    ap.add_argument("--approve", action="store_true",
                    help="Pre-approve the release, only if Lens approved, QA is ready and checks did not fail")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Model for the agents (default {DEFAULT_MODEL})")
    ap.add_argument("--out", type=Path, default=Path("runs"), help="Where to save runs")
    args = ap.parse_args(argv)

    if args.repo and not args.repo.is_dir():
        ap.error(f"--repo {args.repo} is not a directory")
    company = args.company.read_text().strip() if args.company else DEFAULT_COMPANY
    client = anthropic.Anthropic()

    run_dir = unique_dir(args.out / f"{dt.date.today():%Y-%m-%d}-{slugify(args.task)}")
    run_dir.mkdir(parents=True)
    if args.run_checks:
        print("Checks will run code the agents wrote on this machine. Secrets are removed from its environment,\n"
              "but use a container or throwaway VM.\n")

    tree = build_context(args.repo, args.task)[0] if args.repo else ""
    print(f"Atlas is planning: {args.task}")
    meta = plan(client, company, args.task, tree)
    team = meta["team"]
    codebase = build_context(args.repo, f"{args.task} {meta['brief']}")[1] if args.repo else ""
    print(f"  {meta['kind']} · {meta['priority']} — {meta['brief']}")
    print(f"  Team: {' -> '.join(AGENTS[x].name for x in team)}\n")

    workspace = prepare_workspace(args.repo, run_dir) if args.run_checks else None
    history: list[tuple[str, str]] = []
    checks_status = "NOT RUN"
    revised = False
    outcome = "complete"
    i = 0

    def record(agent_id: str, text: str) -> None:
        history.append((agent_id, text))
        title = "Automated checks" if agent_id == CHECKS else f"{AGENTS[agent_id].name} — {AGENTS[agent_id].role}"
        (run_dir / f"{len(history):02d}-{agent_id}.md").write_text(f"# {title}\n\n{text}\n")

    while i < len(team):
        agent_id = team[i]
        a = AGENTS[agent_id]

        if agent_id == "relay":
            summary = gate_summary(history, checks_status)
            (run_dir / "GATE.md").write_text("# Release gate\n\n" + "\n".join(f"- {l}" for l in summary.lines()) + "\n")
            if not approve_release(args, summary, run_dir):
                outcome = "stopped at the release gate"
                break

        print(f"{a.name} ({a.role}) is working...")
        text, cut = ask(client, args.model, step_prompt(company, args.task, meta, team, i, history, codebase),
                        8000 if agent_id in CODERS else 3000)
        if cut:
            text += CUT_OFF_NOTE
            print(f"  Warning: {a.name}'s reply hit the length limit and was cut off.")

        if a.writes_code:
            got = extract_files(text, run_dir / "code")
            for f in got.saved:
                print(f"  wrote {f.relative_to(run_dir)}")
            for bad in got.skipped:
                print(f"  Skipped unsafe path: {bad}")
            if not got.saved:
                text += "\n\n_(No files were saved: code blocks need a `File: path` line above them.)_"
                print(f"  Warning: no files saved from {a.name}'s reply ({got.unlabeled_blocks} unlabeled code blocks).")
        record(agent_id, text)

        if a.writes_code and workspace is not None:
            extract_files(text, workspace)
            print("  Running checks...")
            checks_status, report = run_checks(workspace)
            print(f"  Checks: {checks_status}")
            record(CHECKS, f"Status: {checks_status}\n\n{report}")

        if agent_id == "lens" and lens_verdict(text) == "CHANGES REQUESTED" and not revised:
            coder = next((aid for aid, _ in reversed(history) if aid in CODERS), None)
            if coder:
                team[i + 1:i + 1] = [coder, "lens"]
                revised = True
                print(f"  Lens requested changes; back to {AGENTS[coder].name}.")
        i += 1

    summary_lines = [f"# {args.task}", "", f"- Outcome: {outcome}", f"- Type: {meta['kind']}",
                     f"- Priority: {meta['priority']}", f"- Brief: {meta['brief']}",
                     f"- Team: {' -> '.join(AGENTS[x].name for x in team)}", f"- Checks: {checks_status}", ""]
    summary_lines += [f"- [{'Automated checks' if aid == CHECKS else AGENTS[aid].name}]({n:02d}-{aid}.md)"
                      for n, (aid, _) in enumerate(history, 1)]
    (run_dir / "README.md").write_text("\n".join(summary_lines) + "\n")
    print(f"\nRun {outcome}. Deliverables are in {run_dir}")
    return 0 if outcome == "complete" else 2


if __name__ == "__main__":
    sys.exit(main())
