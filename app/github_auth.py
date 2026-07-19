"""
GitHub App authentication: signs a short-lived JWT with the App's private key,
then exchanges it for an installation access token scoped to one repo/org install.
Installation tokens expire after 1 hour — cache and refresh, don't regenerate per request.
"""
import time
import jwt
import httpx

from app.config import settings

GITHUB_API = "https://api.github.com"

_installation_token_cache: dict[int, dict] = {}  # installation_id -> {"token": str, "expires_at": float}


def _generate_app_jwt() -> str:
    now = int(time.time())
    payload = {
        "iat": now - 60,        # backdate 60s to allow for clock drift
        "exp": now + (9 * 60),  # GitHub max is 10 min; stay under it
        "iss": settings.GITHUB_APP_ID,
    }
    return jwt.encode(payload, settings.GITHUB_PRIVATE_KEY, algorithm="RS256")


async def get_installation_token(installation_id: int) -> str:
    cached = _installation_token_cache.get(installation_id)
    if cached and cached["expires_at"] > time.time() + 60:
        return cached["token"]

    app_jwt = _generate_app_jwt()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    _installation_token_cache[installation_id] = {
        "token": data["token"],
        "expires_at": time.time() + 55 * 60,  # refresh a bit early
    }
    return data["token"]
