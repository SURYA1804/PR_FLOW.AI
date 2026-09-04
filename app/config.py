"""
Centralized configuration. Load once, import everywhere.
All secrets come from environment variables (.env locally, Render dashboard in prod).
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _load_private_key() -> str:
    """
    Reads the GitHub App private key from a .pem file, avoiding the escaping
    problems that come from cramming a multi-line PEM into a single .env value.
    """
    key_path = os.environ.get("GITHUB_PRIVATE_KEY_PATH")
    if key_path:
        content = Path(key_path).read_text()
        if "BEGIN" not in content or "END" not in content:
            raise ValueError(
                f"File at GITHUB_PRIVATE_KEY_PATH='{key_path}' doesn't look like a valid PEM file."
            )
        return content

    # Fallback: raw env var (only works if newlines survived intact)
    raw = os.environ.get("GITHUB_PRIVATE_KEY")
    if raw:
        return raw

    raise RuntimeError(
        "No private key configured. Set GITHUB_PRIVATE_KEY_PATH (recommended) "
        "or GITHUB_PRIVATE_KEY in .env."
    )


class Settings:
    # --- GitHub App identity ---
    GITHUB_APP_ID: str = os.environ["GITHUB_APP_ID"]
    GITHUB_PRIVATE_KEY: str = _load_private_key()
    GITHUB_WEBHOOK_SECRET: str = os.environ["GITHUB_WEBHOOK_SECRET"]

    # --- NVIDIA / model ---
    NVIDIA_API_KEY: str = os.environ.get("NVIDIA_API_KEY", "")
    NVIDIA_MODEL_NAME: str = os.environ.get("NVIDIA_MODEL_NAME", "meta/llama-3.1-70b-instruct")
    USE_GROQ: bool = os.environ.get("USE_GROQ", "false").lower() == "true"

    # --- Email MCP ---
    SMTP_HOST: str = os.environ.get("SMTP_HOST", "")
    SMTP_PORT: int = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER: str = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD: str = os.environ.get("SMTP_PASSWORD", "")
    GROQ_API_KEY: str = os.environ.get("GROQ_API_KEY", "")
    GROQ_MODEL_NAME: str = os.environ.get(
        "GROQ_MODEL_NAME",
        "llama-3.3-70b-versatile"
    )

    # --- Behavior ---
    MAX_FEEDBACK_RETRIES: int = int(os.environ.get("MAX_FEEDBACK_RETRIES", "3"))
    CHECKPOINT_DB_PATH: str = os.environ.get("CHECKPOINT_DB_PATH", "checkpoints.sqlite")

    def validate(self):
        """Fail loudly at startup if the active provider's key is missing,
        rather than deep inside a webhook-triggered background task later."""
        if self.USE_GROQ and not self.GROQ_API_KEY:
            raise RuntimeError("USE_GROQ=true but GROQ_API_KEY is not set in .env")
        if not self.USE_GROQ and not self.NVIDIA_API_KEY:
            raise RuntimeError("USE_GROQ=false but NVIDIA_API_KEY is not set in .env")


settings = Settings()
settings.validate()