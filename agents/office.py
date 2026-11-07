"""Run a task through the AI Office team from the command line.

    python -m agents.office "Add SSO login with Okta"

Atlas plans the team, each agent writes its deliverable from the work before it,
Lens can send code back once for changes, and the run stops for your approval
before Relay writes the release plan. Everything lands in runs/<date>-<slug>/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import anthropic

from .roles import (AGENTS, CODERS, DEFAULT_COMPANY, DEFAULT_PIPELINES, ECHO_CLOSE,
                    ECHO_TRIAGE)

DEFAULT_MODEL = os.environ.get("AI_OFFICE_MODEL", "claude-sonnet-5-5")
PLANNER_MODEL = os.environ.get("AI_OFFICE_PLANNER_MODEL", "claude-haiku-5-5")


def ask(client: anthropic.Anthropic, model: str, prompt: str, max_tokens: int = 4000) -> str:
    msg = client.messages.create(model=model, max_tokens=max_tokens,
                                 messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def parse_json(text: str) -> dict:
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    body = fence.group(1) if fence else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {}


def valid_team(team, kind: str) -> list[str]:
    t = [str(x).lower().strip() for x in (team or []) if str(x).lower().strip() in AGENTS]
    t = [x for i, x in enumerate(t) if x != "atlas" and (i == 0 or x != t[i - 1])]
    if not t:
        t = list(DEFAULT_PIPELINES.get(kind, DEFAULT_PIPELINES["feature"]))
    coder_positions = [i for i, x in enumerate(t) if x in CODERS]
    if coder_positions:
        last = coder_positions[-1]
        if "lens" not in t[last:]:
            t.insert(last + 1, "lens")
        if "relay" not in t:
            after = max(i for i, x in enumerate(t) if x in ("lens", "probe"))
            t.insert(after + 1, "relay")
    return t[:10]


def plan(client, company: str, title: str) -> dict:
    roster = "\n".join(f"- {a.id}: {a.role}. {a.mission}" for a in AGENTS.values() if a.id != "atlas")
    prompt = f"""You are Atlas, the CEO and orchestrator of an AI agent team at a software company.
Company context:
{company}

New request from the owner: "{title}"

Agents you can assign (use these ids only):
{roster}

Choose who works on this request and in what order. Rules: code written by byte, pixel or terra must be
followed by lens. Anything that changes production must go through relay, which only runs after the
human owner approves. Put probe after lens when code changes. Leave out agents who add nothing. Use at
most 9 agents.

Reply with only a JSON object like:
{{"kind":"feature","priority":"P1","brief":"Two or three sentences for the team.","team":["nova","forge","byte","lens","probe","relay","pulse"]}}
kind is one of feature, bug, infra, growth. priority is one of P0, P1, P2, P3."""
    p = parse_json(ask(client, PLANNER_MODEL, prompt, 800))
    kind = p.get("kind") if p.get("kind") in DEFAULT_PIPELINES else "feature"
    priority = p.get("priority") if re.fullmatch(r"P[0-3]", str(p.get("priority"))) else "P2"
    return {"kind": kind, "priority": priority, "brief": str(p.get("brief") or title)[:600],
            "team": valid_team(p.get("team"), kind)}


def step_prompt(company: str, title: str, meta: dict, agent_id: str, history: list[tuple[str, str]],
                position: int) -> str:
    a = AGENTS[agent_id]
    if agent_id == "echo":
        job = ECHO_TRIAGE if position < 3 and not any(h[0] == "echo" for h in history) else ECHO_CLOSE
    else:
        job = a.job
    work = "".join(f"\n\n### {AGENTS[aid].name} ({AGENTS[aid].role})\n{text[:3500]}" for aid, text in history)
    return f"""You are {a.name}, the {a.role} agent in an AI agent team at a software company.
Your mission: {a.mission}

Company context:
{company}

Task: {title}
Priority: {meta['priority']} · Type: {meta['kind']}
Atlas's brief: {meta['brief']}

Work from teammates so far:{work or chr(10) + chr(10) + '(none yet)'}

Your job now: {job}

Write in plain Markdown. Build on the teammates' work, do not repeat it, and be concrete and specific to this company."""


def extract_files(text: str, dest: Path) -> list[Path]:
    """Save blocks written as `File: path` followed by a fenced code block."""
    saved = []
    for m in re.finditer(r"File:\s*`?([\w./-]+)`?\s*\n```[\w+-]*\n(.*?)```", text, re.S):
        rel = Path(m.group(1))
        if rel.is_absolute() or ".." in rel.parts:
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(m.group(2))
        saved.append(out)
    return saved


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:50] or "task"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run a task through the AI Office agent team.")
    ap.add_argument("task", help="What you want done, in plain English")
    ap.add_argument("--company", type=Path, help="Text file with your company context")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Model for the agents (default {DEFAULT_MODEL})")
    ap.add_argument("--out", type=Path, default=Path("runs"), help="Where to save the run")
    args = ap.parse_args(argv)

    company = args.company.read_text().strip() if args.company else DEFAULT_COMPANY
    client = anthropic.Anthropic()

    run_dir = args.out / f"{dt.date.today():%Y-%m-%d}-{slugify(args.task)}"
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"Atlas is planning: {args.task}")
    meta = plan(client, company, args.task)
    team = meta["team"]
    print(f"  {meta['kind']} · {meta['priority']} — {meta['brief']}")
    print(f"  Team: {' -> '.join(AGENTS[x].name for x in team)}\n")

    history: list[tuple[str, str]] = []
    revised = False
    i = 0
    while i < len(team):
        agent_id = team[i]
        a = AGENTS[agent_id]
        if agent_id == "relay":
            answer = input(f"Release '{args.task}'? Review {run_dir} first. Approve [y/N]: ").strip().lower()
            if answer != "y":
                print("Release rejected. Nothing goes out.")
                break
        print(f"{a.name} ({a.role}) is working...")
        text = ask(client, args.model, step_prompt(company, args.task, meta, agent_id, history, i))
        history.append((agent_id, text))
        (run_dir / f"{len(history):02d}-{agent_id}.md").write_text(f"# {a.name} — {a.role}\n\n{text}\n")
        if a.writes_code:
            for f in extract_files(text, run_dir / "code"):
                print(f"  wrote {f}")
        if agent_id == "lens" and re.search(r"VERDICT:\s*CHANGES", text, re.I) and not revised:
            coder = next((x for aid, _ in reversed(history) if (x := aid) in CODERS), None)
            if coder:
                team[i + 1:i + 1] = [coder, "lens"]
                revised = True
                print(f"  Lens requested changes; back to {AGENTS[coder].name}.")
        i += 1

    summary = [f"# {args.task}", "", f"- Type: {meta['kind']}", f"- Priority: {meta['priority']}",
               f"- Brief: {meta['brief']}", f"- Team: {' -> '.join(AGENTS[x].name for x in team)}", ""]
    summary += [f"- [{AGENTS[aid].name}]({n:02d}-{aid}.md)" for n, (aid, _) in enumerate(history, 1)]
    (run_dir / "README.md").write_text("\n".join(summary) + "\n")
    print(f"\nDone. Deliverables are in {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
