"""The AI Office team: who each agent is and what it delivers."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    role: str
    mission: str
    job: str = ""
    writes_code: bool = False
    tags: list[str] = field(default_factory=list)


AGENTS: dict[str, Agent] = {a.id: a for a in [
    Agent("atlas", "Atlas", "CEO / Orchestrator",
          "Reads each request, sets priority, writes the brief and picks which agents work on it, in order."),
    Agent("nova", "Nova", "Product manager",
          "Turns requests into specs with acceptance criteria and a success metric.",
          "Write a concise product spec with these sections: Problem, Users, Scope (in / out), "
          "Acceptance criteria (3-6, Given/When/Then), Success metric. Under 300 words."),
    Agent("iris", "Iris", "UX designer",
          "Designs the user flow and screens, including empty, loading and error states.",
          "Describe the UX: the user flow as numbered steps, each key screen with its components and "
          "its empty, loading and error states, and accessibility notes. Under 300 words."),
    Agent("forge", "Forge", "Solutions architect",
          "Chooses the technical approach and splits the work into tickets.",
          "Write the technical design: approach, components and data model, APIs or interfaces, risks "
          "with mitigations, and a numbered ticket list where each ticket names its owner (Byte for "
          "backend, Pixel for frontend, Terra for infrastructure). Under 350 words."),
    Agent("byte", "Byte", "Backend developer",
          "Writes Python services and APIs with tests.",
          "Implement the backend tickets in Python. Give the essential code in fenced code blocks, each "
          "block preceded by a line `File: <relative/path.py>`, plus pytest tests in files named "
          "tests/test_*.py, under 150 lines of code in total. Give each file's complete contents. If Lens "
          "requested changes or automated checks failed above, fix every finding and list what you changed.",
          writes_code=True),
    Agent("pixel", "Pixel", "Frontend developer",
          "Builds the UI in React and TypeScript from the design and the APIs.",
          "Implement the frontend tickets in React with TypeScript. Give the essential component code in "
          "fenced code blocks, each preceded by a line `File: <relative/path.tsx>`, under 120 lines in "
          "total. Give each file's complete contents. If Lens requested changes or automated checks failed "
          "above, fix every finding "
          "and list what you changed.",
          writes_code=True),
    Agent("lens", "Lens", "Code reviewer",
          "Reviews code and infrastructure for correctness, security, tests and cost.",
          "Review the most recent code or Terraform above. List findings as lines in the form "
          "[CRITICAL|MAJOR|MINOR] area: issue. Fix: how. Cover security, correctness, tests and cost. "
          "End with exactly one final line: VERDICT: APPROVE or VERDICT: CHANGES REQUESTED. Request "
          "changes only for CRITICAL or MAJOR findings."),
    Agent("probe", "Probe", "QA engineer",
          "Plans and specifies the tests that must pass before release.",
          "Write the QA test plan: a Markdown table of test cases (ID, scenario, type, expected result), "
          "the edge cases you worry about, and the exit criteria for release. Use any automated check "
          "results above; never claim results that are not shown there. Under 300 words. End with "
          "exactly one final line: QA: READY or QA: BLOCKED - <reason>."),
    Agent("terra", "Terra", "Infrastructure engineer",
          "Writes Terraform for AWS infrastructure changes.",
          "Write the Terraform (HCL) for AWS needed for this task in fenced code blocks, each preceded by "
          "a line `File: <relative/path.tf>`, under 100 lines, with variables and tags. Then summarize "
          "what the plan would add, change or destroy, and how to roll back. Give each file's complete "
          "contents. If Lens requested changes or automated checks failed above, fix every finding.",
          writes_code=True),
    Agent("relay", "Relay", "CI/CD release manager",
          "Writes the release plan once the owner approves: pipeline stages, rollout, rollback.",
          "The human owner has approved this release. Write the release plan: Jenkins pipeline stages, "
          "rollout strategy (canary or blue-green, with percentages), feature flag, pre-release "
          "checklist, and exact rollback steps. Under 250 words."),
    Agent("pulse", "Pulse", "Site reliability",
          "Defines what to watch after release and when to roll back.",
          "Write the post-release monitoring plan: SLOs, CloudWatch alarms with thresholds, the dashboard "
          "panels to watch, automatic rollback triggers, and the first-24-hours watch schedule. Under 200 words."),
    Agent("echo", "Echo", "Customer support",
          "Triages customer issues and tells customers when fixes ship."),
    Agent("quill", "Quill", "Marketing & sales",
          "Writes release notes and launch posts for what shipped.",
          "Write customer-facing release notes (3-5 bullets), a short launch post of about 120 words, and "
          "two lines of talking points for sales. No claims beyond what the team built."),
]}

ECHO_JOBS = {
    "triage": ("Triage this as support: customer impact, reproduction steps, severity (SEV1-SEV4) with "
               "reasoning, and a short holding reply to the affected customer. Under 250 words."),
    "voice": ("Give the customer view of this request: who asks for it, the problem in their words, and "
              "what would make them happy. Under 200 words."),
    "fixed": ("The fix has been released. Write the customer update announcing it: what was wrong, what "
              "changed, and what customers should do, if anything. Under 150 words, warm and plain."),
    "shipped": ("This change has been released. Write the customer announcement: what is new or changed, "
                "who it affects, and what they need to do, if anything. Under 150 words, warm and plain."),
    "launch": ("Prepare support for this launch: a help-center article draft (title and steps), three "
               "questions customers are likely to ask with answers, and a short reply support can send. "
               "Under 250 words."),
}


def echo_job(kind: str, team: list[str], position: int) -> str:
    """Pick Echo's job from the task type and whether code work comes before this step."""
    if kind == "growth":
        return ECHO_JOBS["launch"]
    after_code = any(a in CODERS for a in team[:position])
    if not after_code:
        return ECHO_JOBS["triage"] if kind == "bug" else ECHO_JOBS["voice"]
    return ECHO_JOBS["fixed"] if kind == "bug" else ECHO_JOBS["shipped"]


CODERS = {"byte", "pixel", "terra"}

DEFAULT_PIPELINES = {
    "feature": ["nova", "forge", "byte", "lens", "probe", "relay", "pulse", "quill"],
    "bug": ["echo", "byte", "lens", "probe", "relay", "echo"],
    "infra": ["forge", "terra", "lens", "relay", "pulse"],
    "growth": ["nova", "quill", "echo"],
}

DEFAULT_COMPANY = (
    "We are a small software company. Backend services are written in Python and run on AWS "
    "(EKS, RDS, S3). Infrastructure is managed with Terraform and released through Jenkins pipelines. "
    "The frontend is React with TypeScript. We value small, reviewable changes and safe rollbacks."
)
