"""
Thin wrapper around the GitHub REST API for everything the agent needs:
fetching diffs, creating/updating PRs, reading review comments, resolving
the committer's email.
"""
import base64
import httpx

from app.github_auth import get_installation_token
from app.logging_config import get_logger

GITHUB_API = "https://api.github.com"
logger = get_logger(__name__)


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


async def get_compare_diff(installation_id: int, owner: str, repo: str, base: str, head: str) -> dict:
    """Returns changed files + patches between base and head."""
    logger.info(f"get_compare_diff: {owner}/{repo} {base}...{head}")
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/compare/{base}...{head}",
            headers=_headers(token),
        )
        if resp.status_code >= 400:
            logger.error(f"get_compare_diff: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"get_compare_diff: {len(data.get('files', []))} file(s) changed")
        return data


async def get_single_commit_diff(installation_id: int, owner: str, repo: str, sha: str) -> dict:
    """
    Used for the very first commit on a branch, where `before` from the push
    payload is all zeros and there's no valid base to compare against.
    Returns the same 'files' shape as get_compare_diff for a drop-in fit.
    """
    logger.info(f"get_single_commit_diff: {owner}/{repo}@{sha}")
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits/{sha}",
            headers=_headers(token),
        )
        if resp.status_code >= 400:
            logger.error(f"get_single_commit_diff: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"get_single_commit_diff: {len(data.get('files', []))} file(s) changed")
        return data

async def get_open_pull_request(
    installation_id: int, owner: str, repo: str, head_branch: str, base_branch: str,
) -> dict | None:
    """Returns the existing open PR for this branch, or None if none exists."""
    logger.info(f"get_open_pull_request: {owner}/{repo} head={head_branch} base={base_branch}")
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
            headers=_headers(token),
            params={"head": f"{owner}:{head_branch}", "base": base_branch, "state": "open"},
        )
        if resp.status_code >= 400:
            logger.error(f"get_open_pull_request: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()
        results = resp.json()
        return results[0] if results else None

async def is_pr_open(installation_id: int, owner: str, repo: str, pr_number: int) -> bool:
    """Returns True if the PR is still open (not merged, not closed)."""
    logger.info(f"is_pr_open: {owner}/{repo} PR #{pr_number}")
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=_headers(token),
        )
        if resp.status_code >= 400:
            logger.error(f"is_pr_open: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()
        return data.get("state") == "open"
       
async def get_file_content(installation_id: int, owner: str, repo: str, path: str, ref: str) -> tuple[str, str]:
    """Returns (decoded_content, sha) for a file, or ("", None) if it doesn't exist yet."""
    logger.info(f"get_file_content: {owner}/{repo}/{path}@{ref}")
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
            headers=_headers(token),
            params={"ref": ref},
        )
        if resp.status_code == 404:
            logger.info(f"get_file_content: {path} does not exist yet on {ref}")
            return "", None
        if resp.status_code >= 400:
            logger.error(f"get_file_content: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]


async def create_branch(installation_id: int, owner: str, repo: str, new_branch: str, from_branch: str):
    logger.info(f"create_branch: {owner}/{repo} '{new_branch}' from '{from_branch}'")
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient() as client:
        ref_resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/ref/heads/{from_branch}",
            headers=_headers(token),
        )
        if ref_resp.status_code >= 400:
            logger.error(f"create_branch: failed to read ref for '{from_branch}' "
                         f"({ref_resp.status_code}) {ref_resp.text[:500]}")
        ref_resp.raise_for_status()
        base_sha = ref_resp.json()["object"]["sha"]

        create_resp = await client.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/refs",
            headers=_headers(token),
            json={"ref": f"refs/heads/{new_branch}", "sha": base_sha},
        )
        if create_resp.status_code == 422:
            logger.info(f"create_branch: '{new_branch}' already exists, continuing")
        elif create_resp.status_code not in (201, 422):
            logger.error(f"create_branch: {create_resp.status_code} {create_resp.text[:500]}")
            create_resp.raise_for_status()
        else:
            logger.info(f"create_branch: '{new_branch}' created")


async def update_file(
    installation_id: int, owner: str, repo: str, path: str,
    new_content: str, branch: str, message: str,
):
    """Creates or updates a file on the given branch (used for README + re-runs)."""
    logger.info(f"update_file: {owner}/{repo}/{path} on '{branch}' ({len(new_content)} chars)")
    token = await get_installation_token(installation_id)
    _, existing_sha = await get_file_content(installation_id, owner, repo, path, branch)

    payload = {
        "message": message,
        "content": base64.b64encode(new_content.encode("utf-8")).decode("utf-8"),
        "branch": branch,
    }
    if existing_sha:
        payload["sha"] = existing_sha

    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
            headers=_headers(token),
            json=payload,
        )
        if resp.status_code >= 400:
            logger.error(f"update_file: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()
        logger.info(f"update_file: '{path}' updated on '{branch}'")
        return resp.json()


async def create_pull_request(
    installation_id: int, owner: str, repo: str,
    head_branch: str, base_branch: str, title: str, body: str,
) -> dict:
    logger.info(f"create_pull_request: {owner}/{repo} '{head_branch}' -> '{base_branch}'")
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
            headers=_headers(token),
            json={"title": title, "head": head_branch, "base": base_branch, "body": body},
        )
        if resp.status_code >= 400:
            logger.error(f"create_pull_request: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"create_pull_request: PR #{data['number']} opened at {data['html_url']}")
        return data


async def get_review_comments(installation_id: int, owner: str, repo: str, pr_number: int) -> list[str]:
    """Combines top-level review bodies and inline comments into a flat feedback list."""
    logger.info(f"get_review_comments: {owner}/{repo} PR #{pr_number}")
    token = await get_installation_token(installation_id)
    feedback = []
    async with httpx.AsyncClient() as client:
        reviews_resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            headers=_headers(token),
        )
        if reviews_resp.status_code >= 400:
            logger.error(f"get_review_comments: reviews fetch {reviews_resp.status_code} "
                         f"{reviews_resp.text[:500]}")
        reviews_resp.raise_for_status()
        for review in reviews_resp.json():
            if review.get("body"):
                feedback.append(review["body"])

        comments_resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/comments",
            headers=_headers(token),
        )
        if comments_resp.status_code >= 400:
            logger.error(f"get_review_comments: comments fetch {comments_resp.status_code} "
                         f"{comments_resp.text[:500]}")
        comments_resp.raise_for_status()
        for comment in comments_resp.json():
            feedback.append(f"[{comment.get('path', '')}] {comment['body']}")

    logger.info(f"get_review_comments: {len(feedback)} feedback item(s) collected for PR #{pr_number}")
    return feedback


async def get_committer_email(installation_id: int, owner: str, repo: str, sha: str) -> str | None:
    """
    Best-effort resolution. GitHub often masks emails behind a noreply address
    if the user has 'keep my email private' enabled — see caveat in project notes.
    """
    logger.info(f"get_committer_email: {owner}/{repo}@{sha[:7]}")
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits/{sha}",
            headers=_headers(token),
        )
        if resp.status_code >= 400:
            logger.error(f"get_committer_email: {resp.status_code} {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()
        email = data.get("commit", {}).get("author", {}).get("email")
        if not email:
            logger.warning(f"get_committer_email: no email found for {sha[:7]}")
        return email