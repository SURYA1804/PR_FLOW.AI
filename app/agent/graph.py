"""
The LangGraph state machine wiring together diff fetch -> changelog synthesis
-> README patch -> PR create/update -> email notification, with a branch that
routes new PRs vs. feedback re-runs to different terminal steps.
"""
from typing import TypedDict, Optional

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.config import settings
from app.logging_config import get_logger
from app.github_client import get_review_comments
from app.agent.nodes import (
    check_pr_status,
    fetch_diff,
    synthesize_changelog,
    generate_readme_patch,
    create_branch_and_pr,
    push_feedback_update,
    notify_email,
)

logger = get_logger(__name__)


class AgentState(TypedDict, total=False):
    installation_id: int
    owner: str
    repo: str
    base_branch: str
    head_sha: str
    before_sha: str
    commit_message: str
    commit_date: str
    changelog_file_path: str
    diff: str
    committer_email: Optional[str]
    changelog: str
    readme_content: str
    branch_name: str
    pr_number: int
    pr_url: str
    review_feedback: list
    retry_count: int
    email_sent: bool


def _route_after_readme(state: AgentState) -> str:
    """First run creates a new PR; a feedback re-run updates the existing one."""
    if state.get("pr_number"):
        logger.info(f"routing: pr_number={state['pr_number']} present -> push_feedback_update")
        return "push_feedback_update"
    logger.info("routing: no pr_number in state -> create_branch_and_pr")
    return "create_branch_and_pr"

def _route_after_diff(state: AgentState) -> str:
    if state.get("skip_reason"):
        return "skip"
    return "synthesize"

def _build_workflow() -> StateGraph:
    workflow = StateGraph(AgentState)

    workflow.add_node("fetch_diff", fetch_diff)
    workflow.add_node("synthesize_changelog", synthesize_changelog)
    workflow.add_node("generate_readme_patch", generate_readme_patch)
    workflow.add_node("create_branch_and_pr", create_branch_and_pr)
    workflow.add_node("push_feedback_update", push_feedback_update)
    workflow.add_node("notify_email", notify_email)
    workflow.add_node("check_pr_status", check_pr_status)

    workflow.add_edge(START, "fetch_diff")
    workflow.add_conditional_edges(
        "fetch_diff",
        _route_after_diff,
        {"synthesize": "synthesize_changelog", "skip": END},
    )
    workflow.add_edge("synthesize_changelog", "generate_readme_patch")
    workflow.add_edge("generate_readme_patch", "check_pr_status")
    workflow.add_conditional_edges(
        "check_pr_status",
        _route_after_readme,
        {
            "create_branch_and_pr": "create_branch_and_pr",
            "push_feedback_update": "push_feedback_update",
        },
    )
    workflow.add_edge("create_branch_and_pr", "notify_email")
    workflow.add_edge("push_feedback_update", "notify_email")
    workflow.add_edge("notify_email", END)

    logger.info("graph: workflow built with 6 nodes")
    return workflow


def build_graph() -> StateGraph:
    """
    Returns the UNCOMPILED workflow. main.py holds onto this and passes it to
    run_push_event/run_review_event, which compile it fresh with an
    AsyncSqliteSaver attached per call — a CompiledGraph can't have its
    checkpointer swapped after compilation, so we compile at call time instead.
    """
    return _build_workflow()


def _thread_config(owner: str, repo: str):
    """
    One thread per repo, forever — there's only ever one ai-changelog branch/PR
    per repo, so all pushes and all review cycles share the same checkpoint thread.
    """
    key = f"pr-{owner}-{repo}-ai-changelog-updates"
    return {"configurable": {"thread_id": key}}


async def run_push_event(graph, payload: dict):
    """Entry point for a `push` webhook."""
    repo_full = payload["repository"]["full_name"]
    owner, repo = repo_full.split("/")
    installation_id = payload["installation"]["id"]
    head_sha = payload["after"]
    before_sha = payload.get("before")  # commit the branch pointed to BEFORE this push
    ref = payload["ref"]  # e.g. "refs/heads/main"
    base_branch = ref.replace("refs/heads/", "")

    head_commit = payload.get("head_commit") or {}
    commit_message = (head_commit.get("message") or "").splitlines()[0] if head_commit.get("message") else "(no commit message)"
    commit_timestamp = head_commit.get("timestamp", "")  # ISO 8601, e.g. "2026-08-22T13:40:12+00:00"
    commit_date = commit_timestamp[:10] if commit_timestamp else "unknown-date"

    logger.info(f"run_push_event: {owner}/{repo} branch={base_branch} "
                f"before={before_sha[:7] if before_sha else 'none'} sha={head_sha[:7]}")



    initial_state: AgentState = {
        "installation_id": installation_id,
        "owner": owner,
        "repo": repo,
        "base_branch": base_branch,
        "head_sha": head_sha,
        "before_sha": before_sha,
        "commit_message": commit_message,
        "commit_date": commit_date,
    }

    try:
        async with AsyncSqliteSaver.from_conn_string(settings.CHECKPOINT_DB_PATH) as checkpointer:
            compiled = graph.compile(checkpointer=checkpointer)
            config = _thread_config(owner, repo)
            logger.info(f"run_push_event: invoking graph, thread_id={config['configurable']['thread_id']}")
            await compiled.ainvoke(initial_state, config=config)
        logger.info(f"run_push_event: completed for {owner}/{repo} sha={head_sha[:7]}")
    except Exception:
        logger.exception(f"run_push_event: graph invocation failed for {owner}/{repo} sha={head_sha[:7]}")
        raise


async def run_review_event(graph, payload: dict):
    """Entry point for a `pull_request_review` webhook."""
    review = payload["review"]
    if review["state"] != "changes_requested":
        logger.info(f"run_review_event: review state='{review['state']}', no action taken")
        return

    repo_full = payload["repository"]["full_name"]
    owner, repo = repo_full.split("/")
    installation_id = payload["installation"]["id"]
    pr_number = payload["pull_request"]["number"]
    head_sha = payload["pull_request"]["head"]["sha"]
    branch_name = payload["pull_request"]["head"]["ref"]  # e.g. "ai-changelog/535c537"

    logger.info(f"run_review_event: changes requested on {owner}/{repo} PR #{pr_number} "
                f"(branch={branch_name})")

    try:
        feedback = await get_review_comments(installation_id, owner, repo, pr_number)
    except Exception:
        logger.exception(f"run_review_event: get_review_comments failed for PR #{pr_number}")
        raise
    logger.info(f"run_review_event: fetched {len(feedback)} feedback item(s)")

    try:
        async with AsyncSqliteSaver.from_conn_string(settings.CHECKPOINT_DB_PATH) as checkpointer:
            compiled = graph.compile(checkpointer=checkpointer)
            config = _thread_config(owner, repo)

            current = await compiled.aget_state(config)
            if not current.values:
                logger.error(
                    f"run_review_event: no checkpoint state found for thread_id="
                    f"{config['configurable']['thread_id']} — the original push run may have "
                    f"failed before completing, or checkpoints.sqlite was reset. Cannot resume."
                )
                return

            retry_count = (current.values or {}).get("retry_count", 0)
            if retry_count >= settings.MAX_FEEDBACK_RETRIES:
                logger.warning(
                    f"run_review_event: PR #{pr_number} hit max retries "
                    f"({retry_count}/{settings.MAX_FEEDBACK_RETRIES}), stopping auto-loop"
                )
                return

            logger.info(f"run_review_event: resuming thread_id={config['configurable']['thread_id']} "
                        f"(retry {retry_count + 1}/{settings.MAX_FEEDBACK_RETRIES})")
            await compiled.ainvoke(
                {
                    "review_feedback": feedback,
                    "pr_number": pr_number,
                },
                config=config,
            )
        logger.info(f"run_review_event: completed for PR #{pr_number}")
    except Exception:
        logger.exception(f"run_review_event: graph invocation failed for PR #{pr_number}")
        raise