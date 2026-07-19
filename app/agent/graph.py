"""
The LangGraph state machine wiring together diff fetch -> changelog synthesis
-> README patch -> PR create/update -> email notification, with a branch that
routes new PRs vs. feedback re-runs to different terminal steps.
"""
from typing import TypedDict, Optional

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.config import settings
from app.github_client import get_review_comments
from app.agent.nodes import (
    fetch_diff,
    synthesize_changelog,
    generate_readme_patch,
    create_branch_and_pr,
    push_feedback_update,
    notify_email,
)


class AgentState(TypedDict, total=False):
    installation_id: int
    owner: str
    repo: str
    base_branch: str
    head_sha: str
    diff: str
    committer_email: Optional[str]
    changelog: str
    readme_content: str
    branch_name: str
    pr_number: int
    pr_url: str
    review_feedback: list
    retry_count: int


def _route_after_readme(state: AgentState) -> str:
    """First run creates a new PR; a feedback re-run updates the existing one."""
    if state.get("pr_number"):
        return "push_feedback_update"
    return "create_branch_and_pr"


def _build_workflow() -> StateGraph:
    workflow = StateGraph(AgentState)

    workflow.add_node("fetch_diff", fetch_diff)
    workflow.add_node("synthesize_changelog", synthesize_changelog)
    workflow.add_node("generate_readme_patch", generate_readme_patch)
    workflow.add_node("create_branch_and_pr", create_branch_and_pr)
    workflow.add_node("push_feedback_update", push_feedback_update)
    workflow.add_node("notify_email", notify_email)

    workflow.add_edge(START, "fetch_diff")
    workflow.add_edge("fetch_diff", "synthesize_changelog")
    workflow.add_edge("synthesize_changelog", "generate_readme_patch")
    workflow.add_conditional_edges(
        "generate_readme_patch",
        _route_after_readme,
        {
            "create_branch_and_pr": "create_branch_and_pr",
            "push_feedback_update": "push_feedback_update",
        },
    )
    workflow.add_edge("create_branch_and_pr", "notify_email")
    workflow.add_edge("push_feedback_update", "notify_email")
    workflow.add_edge("notify_email", END)
    return workflow


def build_graph() -> StateGraph:
    """
    Returns the UNCOMPILED workflow. main.py holds onto this and passes it to
    run_push_event/run_review_event, which compile it fresh with an
    AsyncSqliteSaver attached per call — a CompiledGraph can't have its
    checkpointer swapped after compilation, so we compile at call time instead.
    """
    return _build_workflow()


def _thread_config(owner: str, repo: str, pr_number: int | None, head_sha: str | None):
    key = f"pr-{owner}-{repo}-{pr_number or head_sha}"
    return {"configurable": {"thread_id": key}}


async def run_push_event(graph, payload: dict):
    """Entry point for a `push` webhook."""
    repo_full = payload["repository"]["full_name"]
    owner, repo = repo_full.split("/")
    installation_id = payload["installation"]["id"]
    head_sha = payload["after"]
    ref = payload["ref"]  # e.g. "refs/heads/main"
    base_branch = ref.replace("refs/heads/", "")

    initial_state: AgentState = {
        "installation_id": installation_id,
        "owner": owner,
        "repo": repo,
        "base_branch": base_branch,
        "head_sha": head_sha,
    }

    async with AsyncSqliteSaver.from_conn_string(settings.CHECKPOINT_DB_PATH) as checkpointer:
        compiled = graph.compile(checkpointer=checkpointer)
        config = _thread_config(owner, repo, pr_number=None, head_sha=head_sha)
        await compiled.ainvoke(initial_state, config=config)


async def run_review_event(graph, payload: dict):
    """Entry point for a `pull_request_review` webhook."""
    review = payload["review"]
    if review["state"] != "changes_requested":
        return  # approvals are handled by notify_email already having run; extend here if needed

    repo_full = payload["repository"]["full_name"]
    owner, repo = repo_full.split("/")
    installation_id = payload["installation"]["id"]
    pr_number = payload["pull_request"]["number"]
    head_sha = payload["pull_request"]["head"]["sha"]

    feedback = await get_review_comments(installation_id, owner, repo, pr_number)

    async with AsyncSqliteSaver.from_conn_string(settings.CHECKPOINT_DB_PATH) as checkpointer:
        compiled = graph.compile(checkpointer=checkpointer)
        config = _thread_config(owner, repo, pr_number=pr_number, head_sha=head_sha)

        current = await compiled.aget_state(config)
        retry_count = (current.values or {}).get("retry_count", 0)
        if retry_count >= settings.MAX_FEEDBACK_RETRIES:
            # Stop auto-looping; a human needs to take it from here.
            return

        # Merge feedback + known PR number into the persisted state, then re-run from START.
        # fetch_diff will see the cached diff and skip re-fetching; generate_readme_patch's
        # routing sees pr_number is set and takes the update path instead of creating a new PR.
        await compiled.aupdate_state(config, {"review_feedback": feedback, "pr_number": pr_number})
        await compiled.ainvoke(None, config=config)
