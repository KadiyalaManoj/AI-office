# AI-office

An AI agent office for a software company: thirteen agents, each with one job, passing work from request to release. A human approves every release.

| Room | Agents |
|---|---|
| Executive suite | **Atlas**, CEO and orchestrator: sets priority, writes the brief, picks the team |
| Product studio | **Nova**, product manager · **Iris**, UX designer |
| Engineering bay | **Forge**, architect · **Byte**, backend (Python) · **Pixel**, frontend (React) · **Lens**, code reviewer |
| QA lab | **Probe**, QA engineer |
| Cloud ops center | **Terra**, Terraform on AWS · **Relay**, CI/CD release · **Pulse**, SRE |
| Customer desk | **Echo**, support · **Quill**, marketing and sales |

## How a proposal flows

In the browser office, start with **Proposal** (the default):

1. You tell Atlas your requirement.
2. Atlas briefs the team and picks who should give input. A first proposal always includes Nova, Iris and Forge.
3. Each agent gives a recommendation from their role, with hour estimates, costs to buy or run, and risks.
4. Atlas writes the proposal: summary, scope, solution and UX design, architecture, phased timeline, risks and assumptions. Iris draws a sample of the main screen.
5. **The budget is calculated by the office, not invented by the AI.** It's the team's hours × your hourly rate, plus one-off costs and Atlas's contingency (0–30%). Monthly running costs are listed separately. Set your rate and currency under Company context.
6. You review it, discuss it with Atlas in a chat, and then choose one:
   - **Approve and start delivery** creates a delivery task that carries the approved proposal into the flow below.
   - **Send changes to the team** sends your changes, with your discussion if you like, back to Atlas. Atlas takes them to the agents they affect, and a new version comes back showing what changed. Earlier versions stay listed.
   - **Decline**.

The sample screen is sanitized before display: scripts, event handlers and external links or images are removed.

## How a task flows

1. Atlas reads the request, picks a type (feature, bug, infra or growth) and proposes a team.
2. The office enforces the safety order no matter what Atlas proposes. Whenever code or Terraform is written, the team ends with **Lens → Probe → Relay**, followed by post-release work. These steps are never dropped or reordered.
3. Each agent writes its deliverable from everything before it. Reviewers always see the latest code in full.
4. Lens ends every review with `VERDICT: APPROVE` or `VERDICT: CHANGES REQUESTED`. If changes are requested, the code goes back to its author once.
5. Probe ends with `QA: READY` or `QA: BLOCKED - reason`.
6. The run stops at the **release gate**. It shows Lens's verdict, the critical and major findings, QA status and check results, and lists any blockers. Releasing over a blocker takes an explicit extra step.
7. After approval, Relay writes the release plan, Pulse the monitoring plan, and Quill or Echo the customer messaging.

Agents write deliverables: specs, code, Terraform and plans. They never deploy, apply Terraform or merge.

## Three ways to run it

### In the browser on claude.ai: `office/index.html`

The interactive floor plan. It runs as a claude.ai artifact, where each agent calls Claude on the viewer's account and tasks are saved between visits.

- Under **Company context**, describe your stack and attach **codebase files** (up to 60,000 characters). The architect, developers, reviewer and QA read them.
- The browser office doesn't execute code. Use the command line with `--run-checks` for real test runs.
- Opened on its own (a file or a plain static server), it shows the layout but the agents stay offline. Use the local server below to bring them online.

### On localhost with any model: `agents/server.py`

The same page, unmodified, served from your machine with the agents online. Nothing here needs Claude.

```bash
# Free and local: install Ollama (https://ollama.com), then
ollama serve &
ollama pull qwen2.5-coder:7b        # about 5 GB; fast enough for a 16 GB Mac
python -m agents.server --provider ollama      # open http://localhost:8000
```

`--provider anthropic` or `--provider openai` (any OpenAI-compatible API; set `OPENAI_API_KEY` and, for LM Studio or vLLM, `--base-url`) work too. The server listens on `127.0.0.1` only, refuses requests for other host names and from any page that isn't its own. Model calls go through `/api/sample`; tasks and proposals are saved in your browser's localStorage.

**GitHub and Gmail work locally too**, through your own accounts. The server prints what is ready when it starts.

| Button | Setup |
|---|---|
| **Send code to GitHub** | `brew install gh`, then `gh auth login`. The agents' files go on a new `office/<task>-<id>` branch and a pull request opens. Only this project's own `origin` is allowed; add others with `--github-repo owner/name`. Files under `.github/` and `.git/` are never sent. |
| **Email me this proposal** | Copy `.env.example` to `.env` and set `AI_OFFICE_SMTP_USER` and `AI_OFFICE_SMTP_PASSWORD` to a [Gmail App Password](https://myaccount.google.com/apppasswords). Mail can only go to that address (plus `AI_OFFICE_EMAIL_TO`). Restart the server after editing `.env`. |

Slack, calendar invites and "Build on my computer" need claude.ai and report that when clicked.

### From the command line: `agents/`

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...            # from console.anthropic.com
python -m agents.office "Add SSO login with Okta" --repo ../my-service --run-checks
python -m agents.office "Add SSO login" --provider ollama --run-checks --sandbox docker   # local model, sandboxed checks
```

| Option | What it does |
|---|---|
| `--repo PATH` | The agents read your repository's file tree and the files most related to the task, so their code fits it. Secret-looking files (`.env`, keys, Terraform state) are never read. |
| `--run-checks` | Runs real checks after each coding step: `pytest` and `ruff` (syntax errors and undefined names, on the agents' files only) plus advisory `mypy` for Python; `tsc` and `npm test` when the repo has a `package.json`; `terraform fmt`, `init` and `validate` in every Terraform directory. Results go to Lens, Probe and the release gate. **This executes code the agents wrote**, so use `--sandbox docker`, a container or a throwaway VM. API keys and tokens are removed from its environment. |
| `--sandbox docker` | Runs the checks in a throwaway container: no network (only `npm install --ignore-scripts` and `terraform init` get one), no capabilities, memory and process limits, nothing from your environment. The image is built from `agents/sandbox/Dockerfile` the first time. `--sandbox-image` picks another image. |
| `--max-repairs N` | When checks fail, the code goes back to its author with the failure output, up to N times (default 3, 0 turns it off). Whatever still fails blocks the release gate. |
| `--approve` | Pre-approves the release for unattended runs, but only when there are no blockers. Without it, a non-interactive run stops at the gate. |
| `--company FILE` | Your own company context: stack, conventions, constraints. |
| `--provider NAME` | `anthropic` (default), `ollama` (local) or `openai` (any OpenAI-compatible API). `AI_OFFICE_PROVIDER` sets the default. |
| `--base-url URL` | API address for `ollama` (default `http://localhost:11434`, or `OLLAMA_HOST`) or `openai` (or `OPENAI_BASE_URL`). |
| `--model ID`, `--planner-model ID` | Models for the agents and for Atlas's planning step. `AI_OFFICE_MODEL` and `AI_OFFICE_PLANNER_MODEL` set defaults. Ollama defaults to `qwen2.5-coder:7b` for both (use `--model qwen2.5-coder:14b` for better code on a bigger machine), run with a 16,384-token context (`AI_OFFICE_NUM_CTX`). |
| `--out DIR` | Where runs are saved (default `runs`). |

Every reply the pipeline depends on is checked: code needs `File:` blocks, Lens needs a `VERDICT:`, Probe a `QA:` line, and Atlas a JSON plan with a team. A reply that breaks its format is asked for again once, with the problem named. Rate limits, dropped connections and server errors are retried with a growing pause.

Each run gets its own folder, `runs/<date>-<task>[-N]/`. It holds one Markdown file per step, `GATE.md` with the release-gate summary, the agents' files under `code/`, and, with `--run-checks`, the test `workspace/`. The exit code is `0` when the run completes, `2` when it stops at the release gate and `1` when the model could not be reached.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q                    # runner: team rules, providers, repair loop, checks, sandbox, server, end-to-end runs
node tests/page/page_logic.test.js     # browser office follows the same rules
node tests/page/local_shim.test.js     # the localhost stand-in for claude.ai's model and storage
```

GitHub Actions runs all of them on every pull request.
