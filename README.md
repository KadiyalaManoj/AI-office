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

## How a task flows

1. Atlas reads the request and chooses a pipeline (feature, bug, infra or growth) and the agents for it.
2. Each agent writes its deliverable from everything the agents before it wrote.
3. Lens reviews code and Terraform. If it requests changes, the work goes back to the author once.
4. The run stops before Relay. Nothing is released until you approve.
5. Relay writes the release plan, Pulse the monitoring plan, Quill or Echo the customer messaging.

Agents write deliverables (specs, code, Terraform, plans). They do not deploy, apply Terraform or merge anything.

## Two ways to run it

### In the browser: `office/index.html`

The interactive floor plan. It runs as a claude.ai artifact, where each agent calls Claude on the viewer's account and tasks are saved between visits. Opened outside claude.ai it shows the layout but the agents stay offline.

### From the command line: `agents/`

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...            # from console.anthropic.com
python -m agents.office "Add SSO login with Okta"
```

Options:

- `--company context.txt` gives the agents your own company context (stack, conventions, constraints).
- `--model <model-id>` picks the model for the agents. `AI_OFFICE_MODEL` and `AI_OFFICE_PLANNER_MODEL` set the defaults.
- `--out runs` sets where runs are saved.

Each run saves one Markdown file per agent in `runs/<date>-<task>/`, plus any code the agents wrote under `code/`. Review it, then commit what you want to keep on a branch and open a pull request.
