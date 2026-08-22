"""
Individual LangGraph node functions. Each takes the shared state dict and
returns a partial update to merge back in.
"""

from app.config import settings
from app.logging_config import get_logger
from app.agent.llm import get_llm
from app.github_client import (
    create_branch,
    update_file,
    create_pull_request,
    get_file_content,
    get_compare_diff,
    get_committer_email,
)
from app.Service.email_server import send_email
from langchain_core.messages import SystemMessage, HumanMessage

logger = get_logger(__name__)


async def fetch_diff(state: dict) -> dict:
    """Only fetches on the first run of a PR — a feedback re-run already has the diff cached."""
    if state.get("diff"):
        logger.info("fetch_diff: diff already cached, skipping re-fetch")
        return {}

    logger.info(f"fetch_diff: fetching {state['owner']}/{state['repo']} "
                f"{state['base_branch']}...{state['head_sha'][:7]}")
    try:
        compare = await get_compare_diff(
            state["installation_id"], state["owner"], state["repo"],
            base=state["base_branch"], head=state["head_sha"],
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

    try:
        committer_email = await get_committer_email(
            state["installation_id"], state["owner"], state["repo"], state["head_sha"]
        )
    except Exception:
        logger.exception("fetch_diff: get_committer_email failed, continuing without it")
        committer_email = None

    if not committer_email:
        logger.warning("fetch_diff: no committer email resolved (likely private/noreply email)")

    return {"diff": patches, "committer_email": committer_email}


CHANGELOG_SYSTEM_PROMPT = """You are a precise technical writer. Given a code diff, write a short,
factual changelog entry in Markdown suitable for a README's "Recent changes" section.
Rules:
- Summarize what changed and why, based only on the diff provided.
- Use a bullet list, most significant change first.
- Do not invent functionality that isn't in the diff.
- Keep it under 150 words."""


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

    logger.info(f"synthesize_changelog: calling LLM (model={settings.GROQ_MODEL_NAME})")
    try:
        response = await llm.ainvoke(messages)
    except Exception:
        logger.exception("synthesize_changelog: LLM call failed")
        raise

    logger.info(f"synthesize_changelog: got changelog, length={len(response.content)} chars")
    return {"changelog": response.content}


async def generate_readme_patch(state: dict) -> dict:
    logger.info("generate_readme_patch: fetching existing README.md")
    try:
        existing, _ = await get_file_content(
            state["installation_id"], state["owner"], state["repo"], "README.md", state["base_branch"]
        )
    except Exception:
        logger.exception("generate_readme_patch: failed to fetch existing README.md")
        raise

    marker = "## Recent changes"
    new_entry = f"### {state['head_sha'][:7]}\n{state['changelog']}\n"

    if marker in existing:
        head, _, rest = existing.partition(marker)
        updated = f"{head}{marker}\n\n{new_entry}\n{rest.lstrip()}"
    else:
        updated = f"{existing.rstrip()}\n\n{marker}\n\n{new_entry}\n" if existing else f"{marker}\n\n{new_entry}\n"

    logger.info(f"generate_readme_patch: new README length={len(updated)} chars")
    return {"readme_content": updated}


async def create_branch_and_pr(state: dict) -> dict:
    branch_name = f"ai-changelog/{state['head_sha'][:7]}"
    logger.info(f"create_branch_and_pr: creating branch '{branch_name}' from '{state['base_branch']}'")

    try:
        await create_branch(
            state["installation_id"], state["owner"], state["repo"],
            new_branch=branch_name, from_branch=state["base_branch"],
        )
        await update_file(
            state["installation_id"], state["owner"], state["repo"],
            path="README.md", new_content=state["readme_content"],
            branch=branch_name, message="docs: AI-generated changelog entry",
        )
        pr = await create_pull_request(
            state["installation_id"], state["owner"], state["repo"],
            head_branch=branch_name, base_branch=state["base_branch"],
            title=f"Changelog update: {state['head_sha'][:7]}",
            body=state["changelog"],
        )
    except Exception:
        logger.exception(f"create_branch_and_pr: failed for branch '{branch_name}'")
        raise

    logger.info(f"create_branch_and_pr: PR #{pr['number']} opened at {pr['html_url']}")
    return {"branch_name": branch_name, "pr_number": pr["number"], "pr_url": pr["html_url"]}


async def push_feedback_update(state: dict) -> dict:
    """Re-run path: push the revised README to the SAME branch (updates the existing PR)."""
    retry_count = state.get("retry_count", 0) + 1
    logger.info(f"push_feedback_update: updating branch '{state['branch_name']}' "
                f"(retry #{retry_count})")
    try:
        await update_file(
            state["installation_id"], state["owner"], state["repo"],
            path="README.md", new_content=state["readme_content"],
            branch=state["branch_name"], message="docs: address review feedback",
        )
    except Exception:
        logger.exception(f"push_feedback_update: failed to update branch '{state['branch_name']}'")
        raise

    logger.info(f"push_feedback_update: PR #{state.get('pr_number')} updated successfully")
    return {"retry_count": retry_count}


async def notify_email(state: dict) -> dict:
    """Sends the milestone email. Failures here are logged but never block the PR/PR-update
    that already succeeded — a missing email shouldn't roll back real GitHub work."""
    if not state.get("committer_email"):
        logger.warning("notify_email: no committer_email in state, skipping notification")
        return {}

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
        send_email(state["committer_email"], subject, body)
    except Exception:
        logger.exception(f"notify_email: send_email failed for {state['committer_email']}")
        return {}

    logger.info(f"notify_email: sent successfully to {state['committer_email']}")
    return {}