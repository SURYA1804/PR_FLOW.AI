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
from app.logging_config import setup_logging, get_logger
from app.agent.graph import build_graph, run_push_event, run_review_event

setup_logging()
logger = get_logger(__name__)

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


async def _run_push_event_logged(graph, payload: dict):
    delivery_id = payload.get("_delivery_id", "unknown")
    repo = payload.get("repository", {}).get("full_name", "unknown")
    logger.info(f"[push] starting agent run | repo={repo} delivery={delivery_id}")
    try:
        await run_push_event(graph, payload)
        logger.info(f"[push] agent run completed | repo={repo} delivery={delivery_id}")
    except Exception:
        logger.exception(f"[push] agent run FAILED | repo={repo} delivery={delivery_id}")


async def _run_review_event_logged(graph, payload: dict):
    delivery_id = payload.get("_delivery_id", "unknown")
    repo = payload.get("repository", {}).get("full_name", "unknown")
    pr_number = payload.get("pull_request", {}).get("number", "unknown")
    logger.info(f"[review] starting agent run | repo={repo} pr={pr_number} delivery={delivery_id}")
    try:
        await run_review_event(graph, payload)
        logger.info(f"[review] agent run completed | repo={repo} pr={pr_number} delivery={delivery_id}")
    except Exception:
        logger.exception(f"[review] agent run FAILED | repo={repo} pr={pr_number} delivery={delivery_id}")


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    event_type = request.headers.get("X-GitHub-Event")

    logger.info(f"webhook received | event={event_type} delivery={delivery_id}")

    if not _verify_signature(raw_body, signature):
        logger.warning(f"invalid webhook signature | delivery={delivery_id}")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if delivery_id in _seen_deliveries:
        logger.info(f"duplicate delivery ignored | delivery={delivery_id}")
        return {"status": "duplicate, ignored"}
    _seen_deliveries.add(delivery_id)

    payload = json.loads(raw_body)
    payload["_delivery_id"] = delivery_id  # threaded through for log correlation

    if event_type == "push":
        background_tasks.add_task(_run_push_event_logged, graph, payload)
    elif event_type == "pull_request_review":
        background_tasks.add_task(_run_review_event_logged, graph, payload)
    else:
        logger.info(f"ignored event type | event={event_type} delivery={delivery_id}")
        return {"status": f"ignored event: {event_type}"}

    return {"status": "accepted"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}