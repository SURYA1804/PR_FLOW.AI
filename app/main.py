"""
FastAPI entrypoint. Receives GitHub webhooks, verifies the signature,
and dispatches to the LangGraph agent. Returns 200 immediately and processes
in the background so GitHub doesn't retry on a slow LLM call.
"""
import hashlib
import hmac
import json

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks

from app.config import settings
from app.agent.graph import build_graph, run_push_event, run_review_event

app = FastAPI(title="GitHub AI Changelog Agent")
graph = build_graph()

# Simple in-memory dedupe on GitHub's delivery ID. Swap for Redis/DB in multi-instance prod.
_seen_deliveries: set[str] = set()


def _verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        settings.GITHUB_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not _verify_signature(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    if delivery_id in _seen_deliveries:
        return {"status": "duplicate, ignored"}
    _seen_deliveries.add(delivery_id)

    event_type = request.headers.get("X-GitHub-Event")
    payload = json.loads(raw_body)

    if event_type == "push":
        background_tasks.add_task(run_push_event, graph, payload)
    elif event_type == "pull_request_review":
        background_tasks.add_task(run_review_event, graph, payload)
    else:
        return {"status": f"ignored event: {event_type}"}

    return {"status": "accepted"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
