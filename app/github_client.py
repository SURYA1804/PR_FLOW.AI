"""
Thin wrapper around the GitHub REST API for everything the agent needs:
fetching diffs, creating/updating PRs, reading review comments, resolving
the committer's email.
"""
import base64
import httpx

from app.github_auth import get_installation_token

GITHUB_API = "https://api.github.com"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


async def get_compare_diff(installation_id: int, owner: str, repo: str, base: str, head: str) -> dict:
    """Returns changed files + patches between base and head."""
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/compare/{base}...{head}",
            headers=_headers(token),
        )
        resp.raise_for_status()
        return resp.json()


async def get_file_content(installation_id: int, owner: str, repo: str, path: str, ref: str) -> tuple[str, str]:
    """Returns (decoded_content, sha) for a file, or ("", None) if it doesn't exist yet."""
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
            headers=_headers(token),
            params={"ref": ref},
        )
        if resp.status_code == 404:
            return "", None
        resp.raise_for_status()
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]


async def create_branch(installation_id: int, owner: str, repo: str, new_branch: str, from_branch: str):
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient() as client:
        ref_resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/ref/heads/{from_branch}",
            headers=_headers(token),
        )
        ref_resp.raise_for_status()
        base_sha = ref_resp.json()["object"]["sha"]

        create_resp = await client.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/git/refs",
            headers=_headers(token),
            json={"ref": f"refs/heads/{new_branch}", "sha": base_sha},
        )
        # 422 = branch already exists, which is fine on a re-run
        if create_resp.status_code not in (201, 422):
            create_resp.raise_for_status()


async def update_file(
    installation_id: int, owner: str, repo: str, path: str,
    new_content: str, branch: str, message: str,
):
    """Creates or updates a file on the given branch (used for README + re-runs)."""
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
        resp.raise_for_status()
        return resp.json()


async def create_pull_request(
    installation_id: int, owner: str, repo: str,
    head_branch: str, base_branch: str, title: str, body: str,
) -> dict:
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
            headers=_headers(token),
            json={"title": title, "head": head_branch, "base": base_branch, "body": body},
        )
        resp.raise_for_status()
        return resp.json()


async def get_review_comments(installation_id: int, owner: str, repo: str, pr_number: int) -> list[str]:
    """Combines top-level review bodies and inline comments into a flat feedback list."""
    token = await get_installation_token(installation_id)
    feedback = []
    async with httpx.AsyncClient() as client:
        reviews_resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            headers=_headers(token),
        )
        reviews_resp.raise_for_status()
        for review in reviews_resp.json():
            if review.get("body"):
                feedback.append(review["body"])

        comments_resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/comments",
            headers=_headers(token),
        )
        comments_resp.raise_for_status()
        for comment in comments_resp.json():
            feedback.append(f"[{comment.get('path', '')}] {comment['body']}")

    return feedback


async def get_committer_email(installation_id: int, owner: str, repo: str, sha: str) -> str | None:
    """
    Best-effort resolution. GitHub often masks emails behind a noreply address
    if the user has 'keep my email private' enabled — see caveat in project notes.
    """
    token = await get_installation_token(installation_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits/{sha}",
            headers=_headers(token),
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("commit", {}).get("author", {}).get("email")
