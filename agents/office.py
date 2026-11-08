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
import subprocess
import sys
from pathlib import Path

try:
    import anthropic
except ImportError:      # only needed for --provider anthropic
    anthropic = None  # type: ignore[assignment]

from .checks import SANDBOX_IMAGE, DockerRunner, make_runner, run_checks
from .codebase import SKIP_DIRS, build_context
from .pipeline import (CHECKS, CODERS, correction_prompt, extract_files, gate_summary, lens_verdict,
                       plan_problem, planner_prompt, reply_problem, slugify, step_prompt, unique_dir,
                       valid_team)
from .providers import PROVIDERS, ProviderError, build_provider, default_models
from .roles import AGENTS, DEFAULT_COMPANY, DEFAULT_PIPELINES

DEFAULT_MODEL, PLANNER_MODEL = default_models("anthropic")
CUT_OFF_NOTE = "\n\n_(This reply hit the length limit and was cut off.)_"
MAX_FORMAT_RETRIES = 1       # extra tries when a reply breaks the format the pipeline depends on
MAX_REPAIRS = 3              # automatic fix attempts after failed checks, per coding step


def ask(provider, model: str, prompt: str, max_tokens: int = 3000) -> tuple[str, bool]:
    """Return (text, truncated)."""
    return provider.complete(model, prompt, max_tokens)


def ask_valid(provider, model: str, prompt: str, max_tokens: int, agent_id: str,
              retries: int = MAX_FORMAT_RETRIES) -> tuple[str, bool]:
    """Ask, and if the reply breaks its required format, ask again naming the problem."""
    text, cut = ask(provider, model, prompt, max_tokens)
    for _ in range(retries):
        problem = None if cut else reply_problem(agent_id, text)
        if not problem:
            break
        print(f"  {AGENTS[agent_id].name}'s reply is unusable: {problem} Asking again.")
        text, cut = ask(provider, model, correction_prompt(prompt, problem), max_tokens)
    return text, cut


def parse_json(text: str) -> dict:
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    body = fence.group(1) if fence else text[text.find("{"): text.rfind("}") + 1]
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def plan(client, company: str, title: str, tree: str = "", planner_model: str = PLANNER_MODEL) -> dict:
    prompt = planner_prompt(company, title, tree)
    p = parse_json(ask(client, planner_model, prompt, 800)[0])
    problem = plan_problem(p)
    if problem:
        print(f"  Atlas's plan is unusable: {problem} Asking again.")
        p = parse_json(ask(client, planner_model, correction_prompt(prompt, problem), 800)[0])
    kind = p.get("kind") if p.get("kind") in DEFAULT_PIPELINES else "feature"
    priority = p.get("priority") if re.fullmatch(r"P[0-3]", str(p.get("priority"))) else "P2"
    return {"kind": kind, "priority": priority, "brief": str(p.get("brief") or title)[:600],
            "team": valid_team(p.get("team"), str(kind))}


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
                    help="Run pytest, ruff, tsc, npm test and terraform validate on the agents' code. "
                         "Runs model-written code: use --sandbox docker or a throwaway VM.")
    ap.add_argument("--sandbox", choices=["none", "docker"], default="none",
                    help="Where --run-checks runs the code. 'docker' uses a no-network throwaway container.")
    ap.add_argument("--sandbox-image", default=SANDBOX_IMAGE, help=f"Docker image for the sandbox (default {SANDBOX_IMAGE})")
    ap.add_argument("--max-repairs", type=int, default=MAX_REPAIRS,
                    help=f"Automatic fix attempts when checks fail (default {MAX_REPAIRS}, 0 turns it off)")
    ap.add_argument("--approve", action="store_true",
                    help="Pre-approve the release, only if Lens approved, QA is ready and checks did not fail")
    ap.add_argument("--provider", choices=PROVIDERS, default=os.environ.get("AI_OFFICE_PROVIDER", "anthropic"),
                    help="Which model API to use: anthropic (default), ollama (local) or openai (any "
                         "OpenAI-compatible API)")
    ap.add_argument("--base-url", help="API address for ollama or openai (default: the provider's usual one)")
    ap.add_argument("--model", help="Model for the agents (default depends on --provider)")
    ap.add_argument("--planner-model", help="Model for Atlas's planning step (default depends on --provider)")
    ap.add_argument("--out", type=Path, default=Path("runs"), help="Where to save runs")
    args = ap.parse_args(argv)

    if args.repo and not args.repo.is_dir():
        ap.error(f"--repo {args.repo} is not a directory")
    if args.provider not in PROVIDERS:      # an AI_OFFICE_PROVIDER default is not checked by argparse
        ap.error(f"unknown provider '{args.provider}' (from AI_OFFICE_PROVIDER?). Choose one of: "
                 f"{', '.join(PROVIDERS)}")
    if args.sandbox == "docker" and not args.run_checks:
        ap.error("--sandbox only applies together with --run-checks")
    if args.sandbox == "docker" and not DockerRunner.available():
        ap.error("--sandbox docker needs Docker running. Start Docker Desktop, or leave --sandbox off.")
    if args.sandbox == "docker":
        try:
            DockerRunner.ensure_image(args.sandbox_image)
        except (subprocess.CalledProcessError, OSError) as e:
            ap.error(f"Could not build the sandbox image '{args.sandbox_image}': {e}")
    company = args.company.read_text().strip() if args.company else DEFAULT_COMPANY
    main_model, planner_model = default_models(args.provider)
    model, planner_model = args.model or main_model, args.planner_model or planner_model
    try:
        client = build_provider(args.provider, args.base_url)
    except (ValueError, ImportError) as e:
        ap.error(str(e))
    except Exception as e:      # e.g. the Anthropic SDK complaining about a missing API key
        ap.error(f"Could not set up the {args.provider} provider: {e}")

    run_dir = unique_dir(args.out.resolve() / f"{dt.date.today():%Y-%m-%d}-{slugify(args.task)}")
    run_dir.mkdir(parents=True)
    if args.run_checks:
        if args.sandbox == "docker":
            print("Checks run in a throwaway Docker container with no network access.\n")
        else:
            print("Checks will run code the agents wrote on this machine. Secrets are removed from its environment,\n"
                  "but use --sandbox docker, a container or a throwaway VM.\n")

    tree = build_context(args.repo, args.task)[0] if args.repo else ""
    print(f"Atlas is planning: {args.task}")
    try:
        meta = plan(client, company, args.task, tree, planner_model)
    except ProviderError as e:
        run_dir.rmdir()
        print(f"\nCould not start: {e}")
        return 1
    team = meta["team"]
    codebase = build_context(args.repo, f"{args.task} {meta['brief']}")[1] if args.repo else ""
    print(f"  {meta['kind']} · {meta['priority']} — {meta['brief']}")
    print(f"  Team: {' -> '.join(AGENTS[x].name for x in team)}\n")

    workspace = prepare_workspace(args.repo, run_dir) if args.run_checks else None
    runner = make_runner(args.sandbox, workspace, args.sandbox_image) if workspace is not None else None
    agent_files: set[str] = set()       # every file the agents have written so far, for linting
    history: list[tuple[str, str]] = []
    checks_status = "NOT RUN"
    revised = False
    outcome = "complete"
    i = 0

    def record(agent_id: str, text: str) -> None:
        history.append((agent_id, text))
        title = "Automated checks" if agent_id == CHECKS else f"{AGENTS[agent_id].name} — {AGENTS[agent_id].role}"
        (run_dir / f"{len(history):02d}-{agent_id}.md").write_text(f"# {title}\n\n{text}\n")

    def work(agent_id: str, position: int) -> list[str]:
        """One agent writes its deliverable; code is saved and checked. Returns the files it wrote."""
        a = AGENTS[agent_id]
        text, cut = ask_valid(client, model, step_prompt(company, args.task, meta, team, position, history,
                                                         codebase),
                              8000 if agent_id in CODERS else 3000, agent_id)
        if cut:
            text += CUT_OFF_NOTE
            print(f"  Warning: {a.name}'s reply hit the length limit and was cut off.")
        written: list[str] = []
        if a.writes_code:
            got = extract_files(text, run_dir / "code")
            for f in got.saved:
                print(f"  wrote {f.relative_to(run_dir)}")
            for bad in got.skipped:
                print(f"  Skipped unsafe path: {bad}")
            if not got.saved:
                text += "\n\n_(No files were saved: code blocks need a `File: path` line above them.)_"
                print(f"  Warning: no files saved from {a.name}'s reply ({got.unlabeled_blocks} unlabeled code blocks).")
            written = [f.relative_to(run_dir / "code").as_posix() for f in got.saved]
        record(agent_id, text)
        return written

    def check(text_of_coder: str, changed: list[str]) -> None:
        nonlocal checks_status
        assert workspace is not None
        extract_files(text_of_coder, workspace)
        agent_files.update(changed)
        print("  Running checks...")
        checks_status, report = run_checks(workspace, runner=runner, changed=sorted(agent_files))
        print(f"  Checks: {checks_status}")
        record(CHECKS, f"Status: {checks_status}\n\n{report}")

    try:
        while i < len(team):
            agent_id = team[i]
            a = AGENTS[agent_id]

            if agent_id == "relay":
                summary = gate_summary(history, checks_status)
                (run_dir / "GATE.md").write_text("# Release gate\n\n" + "\n".join(f"- {ln}" for ln in summary.lines()) + "\n")
                if not approve_release(args, summary, run_dir):
                    outcome = "stopped at the release gate"
                    break

            print(f"{a.name} ({a.role}) is working...")
            changed = work(agent_id, i)
            text = history[-1][1]

            if a.writes_code and workspace is not None:
                check(text, changed)
                repairs = 0
                while checks_status == "FAILED" and repairs < args.max_repairs:
                    repairs += 1
                    print(f"  Checks failed; {a.name} fixes the code (attempt {repairs} of {args.max_repairs}).")
                    changed = work(agent_id, i)
                    text = history[-1][1]
                    check(text, changed)
                if checks_status == "FAILED" and args.max_repairs:
                    print(f"  Checks still fail after {args.max_repairs} fix attempts. The release gate will block.")

            if agent_id == "lens" and lens_verdict(text) == "CHANGES REQUESTED" and not revised:
                coder = next((aid for aid, _ in reversed(history) if aid in CODERS), None)
                if coder:
                    team[i + 1:i + 1] = [coder, "lens"]
                    revised = True
                    print(f"  Lens requested changes; back to {AGENTS[coder].name}.")
            i += 1
    except ProviderError as e:
        outcome = f"stopped, the model failed: {e}"
        print(f"\nStopped: {e}")

    summary_lines = [f"# {args.task}", "", f"- Outcome: {outcome}", f"- Type: {meta['kind']}",
                     f"- Priority: {meta['priority']}", f"- Brief: {meta['brief']}",
                     f"- Team: {' -> '.join(AGENTS[x].name for x in team)}", f"- Checks: {checks_status}", ""]
    summary_lines += [f"- [{'Automated checks' if aid == CHECKS else AGENTS[aid].name}]({n:02d}-{aid}.md)"
                      for n, (aid, _) in enumerate(history, 1)]
    (run_dir / "README.md").write_text("\n".join(summary_lines) + "\n")
    print(f"\nRun {outcome}. Deliverables are in {run_dir}")
    return 0 if outcome == "complete" else 2 if outcome.startswith("stopped at") else 1


if __name__ == "__main__":
    sys.exit(main())
