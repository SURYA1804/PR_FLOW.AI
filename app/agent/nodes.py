"""
Individual LangGraph node functions. Each takes the shared state dict and
returns a partial update to merge back in.
"""
from langchain_core.messages import SystemMessage, HumanMessage

from app.config import settings
from app.logging_config import get_logger
from app.agent.llm import get_llm
from app.github_client import (
    create_branch,
    get_open_pull_request,
    is_pr_open,
    update_file,
    create_pull_request,
    get_compare_diff,
    get_single_commit_diff,
    get_committer_email,
)
from app.Service.email_server import send_email

logger = get_logger(__name__)

ZERO_SHA = "0000000000000000000000000000000000000000"
CHANGELOGS_DIR = "changelogs"


async def fetch_diff(state: dict) -> dict:
    """
    Only fetches on the first run of a PR — a feedback re-run already has the diff cached.

    IMPORTANT: the diff base is `before_sha` (the commit the branch pointed to BEFORE
    this push), NOT `base_branch` (the branch name). By webhook time, the branch has
    already advanced to include the new commit, so comparing branch-name...head_sha
    compares the branch against itself and always returns 0 changed files.
    """
    if state.get("diff"):
        logger.info("fetch_diff: diff already cached, skipping re-fetch")
        return {"diff": state["diff"], "committer_email": state.get("committer_email")}

    before_sha = state.get("before_sha")
    head_sha = state["head_sha"]

    if not before_sha or before_sha == ZERO_SHA:
        logger.info(f"fetch_diff: no valid before_sha (first commit on branch), "
                    f"using single-commit diff for {head_sha[:7]}")
        try:
            compare = await get_single_commit_diff(
                state["installation_id"], state["owner"], state["repo"], sha=head_sha,
            )
        except Exception:
            logger.exception("fetch_diff: get_single_commit_diff failed")
            raise
    else:
        logger.info(f"fetch_diff: fetching {state['owner']}/{state['repo']} "
                    f"{before_sha[:7]}...{head_sha[:7]}")
        try:
            compare = await get_compare_diff(
                state["installation_id"], state["owner"], state["repo"],
                base=before_sha, head=head_sha,
            )
        except Exception:
            logger.exception("fetch_diff: get_compare_diff failed")
            raise

    files = compare.get("files", [])
    patches = "\n".join(
        f"--- {f['filename']} ---\n{f.get('patch', '(binary or too large to diff)')}"
        for f in files
    )
    logger.info(f"fetch_diff: got {len(files)} changed file(s), diff length={len(patches)} chars")
    if files and all(f["filename"].startswith(f"{CHANGELOGS_DIR}/") for f in files):
        logger.info("fetch_diff: all changed files are under changelogs/ — "
                    "this is the bot's own PR being merged, skipping")
        return {"diff": patches, "skip_reason": "bot_only_changes"}
    try:
        committer_email = await get_committer_email(
            state["installation_id"], state["owner"], state["repo"], head_sha
        )
    except Exception:
        logger.exception("fetch_diff: get_committer_email failed, continuing without it")
        committer_email = None

    if not committer_email:
        logger.warning("fetch_diff: no committer email resolved (likely private/noreply email)")

    return {"diff": patches, "committer_email": committer_email}


CHANGELOG_SYSTEM_PROMPT = """You are a precise technical writer. Given a code diff, write a short,
factual changelog entry in Markdown.
Rules:
- Summarize what changed and why, based only on the diff provided.
- Use a bullet list, most significant change first.
- Do not invent functionality that isn't in the diff.
- Keep it under 150 words.
- Do NOT include a top-level heading, date, or commit info — that's added separately."""


async def synthesize_changelog(state: dict) -> dict:
    llm = get_llm()
    feedback = state.get("review_feedback") or []
    feedback_block = ""
    if feedback:
        logger.info(f"synthesize_changelog: incorporating {len(feedback)} feedback item(s)")
        feedback_block = (
            "\n\nA reviewer requested changes to a previous draft. Address this feedback:\n"
            + "\n".join(f"- {f}" for f in feedback)
        )

    messages = [
        SystemMessage(content=CHANGELOG_SYSTEM_PROMPT),
        HumanMessage(content=f"Diff:\n{state['diff']}{feedback_block}"),
    ]

    active_model = settings.GROQ_MODEL_NAME if settings.USE_GROQ else settings.NVIDIA_MODEL_NAME
    logger.info(f"synthesize_changelog: calling LLM "
                f"(provider={'groq' if settings.USE_GROQ else 'nvidia'}, model={active_model})")
    try:
        response = await llm.ainvoke(messages)
    except Exception:
        logger.exception("synthesize_changelog: LLM call failed")
        raise

    logger.info(f"synthesize_changelog: got changelog, length={len(response.content)} chars")
    return {"changelog": response.content}


def _changelog_file_path(head_sha: str, commit_date: str) -> str:
    """
    One file per push, under changelogs/. e.g. changelogs/2026-08-22_38a7071.md
    commit_date is expected as YYYY-MM-DD (already trimmed from the ISO timestamp upstream).
    """
    return f"{CHANGELOGS_DIR}/{commit_date}_{head_sha[:7]}.md"


async def generate_readme_patch(state: dict) -> dict:
    head_sha = state["head_sha"]
    commit_date = state.get("commit_date") or "unknown-date"
    commit_message = state.get("commit_message") or "(no commit message)"

    file_path = _changelog_file_path(head_sha, commit_date)  # always fresh — no cached fallback

    content = (
        f"# Changelog Entry\n\n"
        f"- **Commit:** `{head_sha[:7]}`\n"
        f"- **Date:** {commit_date}\n"
        f"- **Message:** {commit_message}\n\n"
        f"---\n\n"
        f"{state['changelog']}\n"
    )

    logger.info(f"generate_readme_patch: writing changelog file '{file_path}' ({len(content)} chars)")
    return {"readme_content": content, "changelog_file_path": file_path}


async def create_branch_and_pr(state: dict) -> dict:
    branch_name = "ai-changelog/updates"  # single persistent branch, reused across pushes
    file_path = state["changelog_file_path"]
    logger.info(f"create_branch_and_pr: using branch '{branch_name}' (creates if missing)")

    try:
        await create_branch(
            state["installation_id"], state["owner"], state["repo"],
            new_branch=branch_name, from_branch=state["base_branch"],
        )  # no-ops with a log line if the branch already exists — see create_branch's 422 handling
        await update_file(
            state["installation_id"], state["owner"], state["repo"],
            path=file_path, new_content=state["readme_content"],
            branch=branch_name, message=f"docs: add changelog entry for {state['head_sha'][:7]}",
        )

        existing_pr = await get_open_pull_request(
            state["installation_id"], state["owner"], state["repo"],
            head_branch=branch_name, base_branch=state["base_branch"],
        )
        if existing_pr:
            logger.info(f"create_branch_and_pr: reusing existing PR #{existing_pr['number']}")
            pr = existing_pr
        else:
            pr = await create_pull_request(
                state["installation_id"], state["owner"], state["repo"],
                head_branch=branch_name, base_branch=state["base_branch"],
                title="Changelog updates",
                body=state["changelog"],
            )
    except Exception:
        logger.exception(f"create_branch_and_pr: failed for branch '{branch_name}'")
        raise

    logger.info(f"create_branch_and_pr: PR #{pr['number']} at {pr['html_url']}")
    return {"branch_name": branch_name, "pr_number": pr["number"], "pr_url": pr["html_url"]}

async def check_pr_status(state: dict) -> dict:
    """If we have a cached pr_number, verify it's still open. If it was merged/closed,
    clear pr_number so routing treats this as a fresh PR — same branch, new PR."""
    pr_number = state.get("pr_number")
    if not pr_number:
        return {"pr_number": None}  # explicit no-op write, satisfies LangGraph's write requirement

    still_open = await is_pr_open(
        state["installation_id"], state["owner"], state["repo"], pr_number
    )
    if not still_open:
        logger.info(f"check_pr_status: PR #{pr_number} is merged/closed — will open a new PR "
                    f"on the same branch")
        return {"pr_number": None, "pr_url": None}

    return {"pr_number": pr_number}  # explicit no-op write when still open
async def push_feedback_update(state: dict) -> dict:
    """Re-run path: push the revised changelog file to the SAME branch/path (updates the existing PR)."""
    retry_count = state.get("retry_count", 0) + 1
    file_path = state["changelog_file_path"]
    logger.info(f"push_feedback_update: updating '{file_path}' on branch '{state['branch_name']}' "
                f"(retry #{retry_count})")
    try:
        await update_file(
            state["installation_id"], state["owner"], state["repo"],
            path=file_path, new_content=state["readme_content"],
            branch=state["branch_name"], message="docs: address review feedback",
        )
    except Exception:
        logger.exception(f"push_feedback_update: failed to update '{file_path}' "
                          f"on branch '{state['branch_name']}'")
        raise

    logger.info(f"push_feedback_update: PR #{state.get('pr_number')} updated successfully")
    return {"retry_count": retry_count}


async def notify_email(state: dict) -> dict:
    """Sends the milestone email. Failures here are logged but never block the PR/PR-update
    that already succeeded — a missing email shouldn't roll back real GitHub work."""
    if not state.get("committer_email"):
        logger.warning("notify_email: no committer_email in state, skipping notification")
        return {"email_sent": False}

    is_followup = bool(state.get("review_feedback"))
    subject = (
        "Your PR changelog was updated after review feedback"
        if is_followup else
        "Your push generated a changelog PR"
    )
    body = (
        f"Hi,\n\nYour changes have been documented and a pull request is ready:\n"
        f"{state['pr_url']}\n\nChangelog:\n{state['changelog']}\n"
    )

    logger.info(f"notify_email: sending to {state['committer_email']} (followup={is_followup})")
    try:
        await send_email(state["committer_email"], subject, body)
    except Exception:
        logger.exception(f"notify_email: send_email failed for {state['committer_email']}")
        return {"email_sent": False}

    logger.info(f"notify_email: sent successfully to {state['committer_email']}")
    return {"email_sent": True}