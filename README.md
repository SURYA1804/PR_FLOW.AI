# PRflow.AI Agent

**PRflow.AI** is a centralized GitHub App that turns every push into documentation,
automatically. It listens for `push` and `pull_request_review` events, generates a
changelog entry for each commit using an LLM, and keeps a single long-lived PR
up to date — reusing it across pushes, re-running with reviewer feedback when
changes are requested, and opening a fresh PR on the same branch if the previous
one gets merged.

No developer intervention required. Push code, PRflow.AI documents it.

## Stack
- Python 3.12, FastAPI (webhook server)
- LangGraph (agent orchestration + SQLite checkpointing, one thread per repo)
- LLM provider toggle: `ChatGroq` or `ChatNVIDIA` (`langchain_nvidia_ai_endpoints`), switched via `USE_GROQ` in `.env`
- A separate email service, decoupled from the core agent, for milestone notifications
- Centralized logging to console + rotating file (`logs/app.log`)

## How PRflow.AI behaves

- **One branch, forever**: `ai-changelog/updates`. Never recreated once it exists.
- **One changelog file per commit**: `changelogs/<date>_<sha7>.md`, containing the
  commit SHA, date, commit message, and an LLM-written summary of the diff.
- **One open PR at a time** on that branch. Every push while the PR is open adds
  a new file to the same PR (no new branch, no new PR).
- **If the PR gets merged**, the next real push detects this, and opens a **new**
  PR on the **same** existing branch — the branch is never recreated.
- **Bot-loop protected**: pushes to `ai-changelog/*` are ignored outright, and a
  push whose diff is 100% confined to `changelogs/` (e.g. the merge commit from
  merging PRflow.AI's own PR) is also skipped before ever calling the LLM.
- **Reviewer feedback loop**: submitting a formal "Request changes" review on the
  open PR re-runs the agent with that feedback and pushes an update to the same
  branch/PR, rather than creating anything new.

## 1. Register the GitHub App
1. GitHub → Settings → Developer settings → GitHub Apps → New GitHub App
2. Name it (e.g. "PRflow.AI Agent")
3. Webhook URL: use `ngrok http 8000` locally, point it at `<ngrok-url>/webhook`
4. Permissions: Contents (Read & write), Pull requests (Read & write), Metadata (Read)
5. Subscribe to events: `push`, `pull_request_review`
6. Generate a private key (.pem) — see step 2 for how it's loaded
7. Install the app on your target repo(s)

## 2. Local setup

```bash
python3.12 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Save your GitHub App's downloaded private key file at the project root as
`github-app-private-key.pem` (real line breaks, not a single unbroken string —
cramming a multi-line PEM into a single `.env` value breaks JWT signing).

Fill in `.env` (see `.env` template in the repo):

```
GITHUB_APP_ID=...
GITHUB_PRIVATE_KEY_PATH=github-app-private-key.pem
GITHUB_WEBHOOK_SECRET=...

USE_GROQ=true
GROQ_API_KEY=...
GROQ_MODEL_NAME=llama-3.3-70b-versatile
# or, with USE_GROQ=false:
NVIDIA_API_KEY=...
NVIDIA_MODEL_NAME=meta/llama-3.1-70b-instruct

EMAIL_MCP_URL=http://localhost:8100
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...

MAX_FEEDBACK_RETRIES=3
CHECKPOINT_DB_PATH=checkpoints.sqlite
```

Run the two services (separate terminals):
```bash
uvicorn app.main:app --reload --port 8000
uvicorn app.mcp.email_server:app --reload --port 8100
```

Tunnel the webhook:
```bash
ngrok http 8000
```
Update the GitHub App's webhook URL to the ngrok URL + `/webhook`. Note the free
ngrok tier assigns a new URL on every restart — update the GitHub App setting
each time, or upgrade for a stable domain.

## 3. Test it

Push a commit to a repo where PRflow.AI is installed. Watch `logs/app.log` (and
the console) — you should see the webhook accepted, the diff fetched, a
changelog generated, and a PR opened on `ai-changelog/updates`.

To test the feedback loop: on that PR, go to **Files changed → Review changes
→ Request changes → Submit review** (a plain comment or closing the PR does
**not** trigger it — only a formal review submission does). PRflow.AI re-runs
with your feedback and pushes an update to the same PR.

To test PR-recycling: merge the open PR, then push another commit — a new PR
should open on the same branch rather than a new branch being created.

## 4. Known limitations

- GitHub often masks a committer's email behind a `noreply` address if they've
  enabled "keep my email private." You'll need a fallback (e.g. an org-maintained
  username → email mapping) for full email coverage.
- Local dev requires ngrok (or similar) since GitHub needs a public URL to reach
  your webhook — see Deploy section below to remove this requirement entirely.

## 5. Deploy to Render

1. Push this repo to GitHub — make sure `.env` and `*.pem` are in `.gitignore`
   and were never committed (check with `git log --all --full-history -- .env`;
   rotate all secrets immediately if they ever were)
2. Render → New → Web Service → connect the repo (uses the included `Dockerfile`)
3. Set all `.env` variables in Render's dashboard as environment variables
   (for the private key, use Render's secret file mount or paste the PEM content
   directly as `GITHUB_PRIVATE_KEY` — do not use `GITHUB_PRIVATE_KEY_PATH` in
   this environment unless you also upload the `.pem` file itself)
4. Update the GitHub App's webhook URL to the Render service's public URL + `/webhook`
5. Deploy the email service as a second Render Web Service, and point
   `EMAIL_MCP_URL` at its internal/public URL

## Project structure

```
app/
├── main.py                  # FastAPI webhook receiver, signature verification,
│                             #   delivery dedupe, bot-loop guards
├── config.py                 # env-driven settings, Groq/NVIDIA toggle, .pem loading
├── logging_config.py          # console + rotating file logging setup
├── github_auth.py              # GitHub App JWT signing + installation token cache
├── github_client.py             # GitHub REST API calls (diffs, branches, PRs, reviews)
├── agent/
│   ├── graph.py                  # LangGraph StateGraph, one checkpoint thread per repo
│   ├── nodes.py                    # fetch_diff, synthesize_changelog, check_pr_status,
│   │                               #   create_branch_and_pr, push_feedback_update, notify_email
│   └── llm.py                       # ChatGroq / ChatNVIDIA wrapper
└── Service/
    └── email_server.py                # isolated async email-sending service
```

---

*PRflow.AI — push code, get documentation.*