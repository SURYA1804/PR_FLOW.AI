# GitHub AI Changelog Agent

A centralized GitHub App that listens for `push` and `pull_request_review` events,
generates a README changelog entry from the diff using an LLM, opens a PR, and
re-runs itself with reviewer feedback when changes are requested.

## Stack
- Python 3.12, FastAPI (webhook server)
- LangGraph (agent orchestration + SQLite checkpointing)
- ChatNVIDIA (`langchain_nvidia_ai_endpoints`) as the LLM
- A separate FastAPI process as the "Email MCP" service, decoupled from the core agent

## 1. Register the GitHub App
1. GitHub → Settings → Developer settings → GitHub Apps → New GitHub App
2. Webhook URL: use `ngrok http 8000` locally, point it at `<ngrok-url>/webhook`
3. Permissions: Contents (Read & write), Pull requests (Read & write), Metadata (Read)
4. Subscribe to events: `push`, `pull_request_review`
5. Generate a private key (.pem), copy its contents into `GITHUB_PRIVATE_KEY` in `.env`
6. Install the app on your target repo(s)

## 2. Local setup
```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env .env.local   # fill in real values, keep .env as the template
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
Update the GitHub App's webhook URL to the ngrok URL + `/webhook`.

## 3. Test it
Push a commit to a repo where the app is installed. Watch the FastAPI logs —
you should see the webhook accepted, the diff fetched, a changelog generated,
and a PR opened. Request changes on that PR as a reviewer to trigger the
feedback loop; the agent re-runs and pushes an update to the same PR.

## 4. Known limitation
GitHub often masks a committer's email behind a `noreply` address if they've
enabled "keep my email private." You'll need a fallback (e.g. an org-maintained
username → email mapping) for full email coverage.

## 5. Deploy to Render
1. Push this repo to GitHub
2. Render → New → Web Service → connect the repo (uses the included `Dockerfile`)
3. Set all `.env` variables in Render's dashboard as environment variables
4. Update the GitHub App's webhook URL to the Render service's public URL + `/webhook`
5. Deploy the email service as a second Render Web Service (or a background worker),
   and point `EMAIL_MCP_URL` at its internal/public URL

## Project structure
```
app/
├── main.py                # FastAPI webhook receiver
├── github_auth.py         # GitHub App JWT + installation tokens
├── github_client.py       # GitHub REST API calls
├── config.py               # env-driven settings
├── agent/
│   ├── graph.py            # LangGraph StateGraph + checkpointing
│   ├── nodes.py            # individual graph node functions
│   └── llm.py               # ChatNVIDIA wrapper
└── mcp/
    └── email_server.py      # isolated email microservice
```
