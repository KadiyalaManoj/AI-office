"""Pure pipeline logic for the AI Office: team rules, prompts, file extraction and the release gate.

Nothing here calls the API or touches the network, so all of it is unit-tested.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .roles import AGENTS, CODERS, DEFAULT_PIPELINES, echo_job

MAX_TEAM = 12
GATES = ["lens", "probe", "relay"]          # always in this order whenever code is written
POST_RELEASE = {"pulse", "quill"}
CODE_LIMIT = 20_000                          # chars of the latest code a reviewer sees
TEXT_LIMIT = 4_000                           # chars of any other teammate output
SUPERSEDED_LIMIT = 1_500                     # chars of an earlier, replaced code draft
CHECKS = "checks"                            # pseudo-agent id for automated check results


def _dedupe_consecutive(items: list[str]) -> list[str]:
    return [x for i, x in enumerate(items) if i == 0 or x != items[i - 1]]


def valid_team(team, kind: str) -> list[str]:
    """Turn Atlas's proposed team into a safe one.

    Rules, applied regardless of what Atlas proposed:
    - only known agents, never Atlas itself, no back-to-back duplicates;
    - if anyone writes code or Terraform, the order ends with Lens -> Probe -> Relay -> post-release work,
      so nothing reaches release unreviewed and nothing ships without the human gate;
    - the team is capped at MAX_TEAM by dropping optional agents, never the gates or the coders.
    """
    t = [str(x).lower().strip() for x in (team or [])]
    t = _dedupe_consecutive([x for x in t if x in AGENTS and x != "atlas"])
    if not t:
        t = list(DEFAULT_PIPELINES.get(kind, DEFAULT_PIPELINES["feature"]))

    if not any(x in CODERS for x in t):
        # Nothing to review or release: drop gates that would have no work to judge.
        return _dedupe_consecutive([x for x in t if x not in ("lens", "relay")])[:MAX_TEAM]

    first_coder = next(i for i, x in enumerate(t) if x in CODERS)
    core: list[str] = []
    post: list[str] = []
    for i, x in enumerate(t):
        if x in GATES:
            continue
        if x in POST_RELEASE or (x == "echo" and i > first_coder):
            post.append(x)
        else:
            core.append(x)
    core = _dedupe_consecutive(core)
    post = list(dict.fromkeys(post))

    while len(core) + len(GATES) + len(post) > MAX_TEAM and post:
        post.pop()
    while len(core) + len(GATES) + len(post) > MAX_TEAM:
        droppable = [j for j, x in enumerate(core) if x not in CODERS]
        if not droppable:
            break
        core.pop(droppable[-1])
    return core + GATES + post


def history_text(history: list[tuple[str, str]]) -> str:
    """Teammates' work as the next agent sees it.

    The latest version of each coder's output and the latest check results are passed in full
    (up to CODE_LIMIT) so reviewers judge the whole file. Earlier, superseded drafts are shortened.
    """
    if not history:
        return "\n\n(none yet)"
    latest = {aid: i for i, (aid, _) in enumerate(history)}
    parts = []
    for i, (aid, text) in enumerate(history):
        if aid in CODERS and latest[aid] != i:
            body, limit = text, SUPERSEDED_LIMIT
            note = "\n...(earlier draft, replaced by a later version below)"
        elif aid in CODERS or aid == CHECKS:
            body, limit, note = text, CODE_LIMIT, "\n...(trimmed)"
        else:
            body, limit, note = text, TEXT_LIMIT, "\n...(trimmed)"
        if len(body) > limit:
            body = body[:limit] + note
        label = "Automated checks" if aid == CHECKS else f"{AGENTS[aid].name} ({AGENTS[aid].role})"
        parts.append(f"\n\n### {label}\n{body}")
    return "".join(parts)


def planner_prompt(company: str, title: str, codebase_tree: str = "") -> str:
    roster = "\n".join(f"- {a.id}: {a.role}. {a.mission}" for a in AGENTS.values() if a.id != "atlas")
    repo = f"\nThe team works in an existing repository. Its files:\n{codebase_tree[:4000]}\n" if codebase_tree else ""
    return f"""You are Atlas, the CEO and orchestrator of an AI agent team at a software company.
Company context:
{company}
{repo}
New request from the owner: "{title}"

Agents you can assign (use these ids only):
{roster}

Choose who works on this request and in what order. Rules: code written by byte, pixel or terra must be
followed by lens, then probe, then relay. Relay only runs after the human owner approves. Leave out agents
who add nothing. Use at most 9 agents.

Reply with only a JSON object like:
{{"kind":"feature","priority":"P1","brief":"Two or three sentences for the team.","team":["nova","forge","byte","lens","probe","relay","pulse"]}}
kind is one of feature, bug, infra, growth. priority is one of P0, P1, P2, P3."""


CODEBASE_READERS = {"forge", "byte", "pixel", "terra", "lens", "probe"}


def step_prompt(company: str, title: str, meta: dict, team: list[str], position: int,
                history: list[tuple[str, str]], codebase: str = "") -> str:
    agent_id = team[position]
    a = AGENTS[agent_id]
    job = echo_job(meta["kind"], team, position) if agent_id == "echo" else a.job
    repo = ""
    if codebase and agent_id in CODEBASE_READERS:
        repo = (f"\n\nThe existing codebase (read-only excerpt). Fit its structure, names and conventions. "
                f"Paths in `File:` lines are relative to the repository root.\n{codebase}")
    return f"""You are {a.name}, the {a.role} agent in an AI agent team at a software company.
Your mission: {a.mission}

Company context:
{company}{repo}

Task: {title}
Priority: {meta['priority']} · Type: {meta['kind']}
Atlas's brief: {meta['brief']}

Work from teammates so far:{history_text(history)}

Your job now: {job}

Write in plain Markdown. Build on the teammates' work, do not repeat it, and be concrete and specific to this company."""


# A `File:` marker line (plain, bold, heading or list form, path optionally in backticks),
# then a fenced block, possibly after blank lines.
FILE_RE = re.compile(
    r"^[ \t>#*_-]*(?:\*\*|__)?File:(?:\*\*|__)?[ \t]*[`*]*(?P<path>[\w./-]+?)[`*]*[ \t]*\n"
    r"(?:[ \t]*\n)*[ \t]*```[^\n]*\n(?P<body>.*?)^[ \t]*```",
    re.M | re.S,
)


@dataclass
class Extracted:
    saved: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unlabeled_blocks: int = 0


def extract_files(text: str, dest: Path) -> Extracted:
    """Save each `File: path` + fenced block under dest. Unsafe paths are skipped, never written."""
    result = Extracted()
    dest = dest.resolve()
    for m in FILE_RE.finditer(text):
        rel = Path(m.group("path"))
        target = (dest / rel).resolve()
        if rel.is_absolute() or ".." in rel.parts or dest not in target.parents:
            result.skipped.append(m.group("path"))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(m.group("body"))
        result.saved.append(target)
    total_blocks = len(re.findall(r"^[ \t]*```[^\n]*\n.*?^[ \t]*```", text, re.M | re.S))
    result.unlabeled_blocks = max(0, total_blocks - len(result.saved) - len(result.skipped))
    return result


@dataclass
class GateSummary:
    lens_verdict: str          # "APPROVE", "CHANGES REQUESTED" or "NO VERDICT"
    findings: list[str]        # CRITICAL / MAJOR lines from the latest review
    qa: str                    # "READY", "BLOCKED - reason" or "NO STATUS"
    checks: str                # "PASSED", "FAILED", "SKIPPED" or "NOT RUN"
    blockers: list[str]

    def lines(self) -> list[str]:
        out = [f"Lens verdict: {self.lens_verdict}", f"QA status:    {self.qa}", f"Checks:       {self.checks}"]
        out += [f"  {f}" for f in self.findings[:8]]
        if self.blockers:
            out.append("Blockers:")
            out += [f"  - {b}" for b in self.blockers]
        return out


def _last(history, agent_id):
    return next((text for aid, text in reversed(history) if aid == agent_id), None)


def lens_verdict(text: str | None) -> str:
    found = re.findall(r"VERDICT:\s*(APPROVE|CHANGES\s+REQUESTED)", text or "", re.I)
    return re.sub(r"\s+", " ", found[-1].upper()) if found else "NO VERDICT"


def gate_summary(history: list[tuple[str, str]], checks_status: str = "NOT RUN") -> GateSummary:
    lens = _last(history, "lens")
    verdict = lens_verdict(lens)
    findings = [ln.strip(" -*") for ln in (lens or "").splitlines()
                if re.match(r"^[\s*-]*\[(CRITICAL|MAJOR)\]", ln, re.I)]
    probe = _last(history, "probe")
    qa_match = re.findall(r"QA:\s*(READY|BLOCKED[^\n]*)", probe or "", re.I)
    qa = qa_match[-1].strip() if qa_match else "NO STATUS"

    blockers = []
    if verdict == "CHANGES REQUESTED":
        blockers.append("Lens still requests changes after the revision.")
    elif verdict == "NO VERDICT":
        blockers.append("Lens did not give a verdict.")
    if qa.upper().startswith("BLOCKED"):
        blockers.append(f"Probe: QA {qa}.")
    if checks_status == "FAILED":
        blockers.append("Automated checks failed.")
    return GateSummary(verdict, findings, qa, checks_status, blockers)


def unique_dir(base: Path) -> Path:
    """base, or base-2, base-3 ... so a rerun never mixes files with an earlier run."""
    if not base.exists():
        return base
    n = 2
    while (candidate := base.with_name(f"{base.name}-{n}")).exists():
        n += 1
    return candidate


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:50] or "task"
