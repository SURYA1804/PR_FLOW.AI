"""
Individual LangGraph node functions. Each takes the shared state dict and
returns a partial update to merge back in.
"""
import httpx
from langchain_core.messages import SystemMessage, HumanMessage

from app.config import settings
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


async def fetch_diff(state: dict) -> dict:
    """Only fetches on the first run of a PR — a feedback re-run already has the diff cached."""
    if state.get("diff"):
        return {}

    compare = await get_compare_diff(
        state["installation_id"], state["owner"], state["repo"],
        base=state["base_branch"], head=state["head_sha"],
    )
    patches = "\n".join(
        f"--- {f['filename']} ---\n{f.get('patch', '(binary or too large to diff)')}"
        for f in compare.get("files", [])
    )
    committer_email = await get_committer_email(
        state["installation_id"], state["owner"], state["repo"], state["head_sha"]
    )
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
        feedback_block = (
            "\n\nA reviewer requested changes to a previous draft. Address this feedback:\n"
            + "\n".join(f"- {f}" for f in feedback)
        )

    messages = [
        SystemMessage(content=CHANGELOG_SYSTEM_PROMPT),
        HumanMessage(content=f"Diff:\n{state['diff']}{feedback_block}"),
    ]
    response = await llm.ainvoke(messages)
    return {"changelog": response.content}


async def generate_readme_patch(state: dict) -> dict:
    existing, _ = await get_file_content(
        state["installation_id"], state["owner"], state["repo"], "README.md", state["base_branch"]
    )
    marker = "## Recent changes"
    new_entry = f"### {state['head_sha'][:7]}\n{state['changelog']}\n"

    if marker in existing:
        head, _, rest = existing.partition(marker)
        updated = f"{head}{marker}\n\n{new_entry}\n{rest.lstrip()}"
    else:
        updated = f"{existing.rstrip()}\n\n{marker}\n\n{new_entry}\n" if existing else f"{marker}\n\n{new_entry}\n"

    return {"readme_content": updated}


async def create_branch_and_pr(state: dict) -> dict:
    branch_name = f"ai-changelog/{state['head_sha'][:7]}"

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
    return {"branch_name": branch_name, "pr_number": pr["number"], "pr_url": pr["html_url"]}


async def push_feedback_update(state: dict) -> dict:
    """Re-run path: push the revised README to the SAME branch (updates the existing PR)."""
    await update_file(
        state["installation_id"], state["owner"], state["repo"],
        path="README.md", new_content=state["readme_content"],
        branch=state["branch_name"], message="docs: address review feedback",
    )
    return {"retry_count": state.get("retry_count", 0) + 1}


async def notify_email(state: dict) -> dict:
    """Calls the isolated Email MCP server — the agent never touches SMTP creds directly."""
    if not state.get("committer_email"):
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

    message = send_email(state["committer_email"], subject, body)
    
    # async with httpx.AsyncClient() as client:
    #     await client.post(
    #         f"{settings.EMAIL_MCP_URL}/send-email",
    #         json={"to": state["committer_email"], "subject": subject, "body": body},
    #         timeout=10.0,
    #     )
    return {}
