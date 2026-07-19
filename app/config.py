"""
Centralized configuration. Load once, import everywhere.
All secrets come from environment variables (.env locally, Render dashboard in prod).
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # --- GitHub App identity ---
    GITHUB_APP_ID: str = os.environ["GITHUB_APP_ID"]
    GITHUB_PRIVATE_KEY: str = os.environ["GITHUB_PRIVATE_KEY"]  # PEM contents, not a path
    GITHUB_WEBHOOK_SECRET: str = os.environ["GITHUB_WEBHOOK_SECRET"]

    # --- NVIDIA / model ---
    NVIDIA_API_KEY: str = os.environ["NVIDIA_API_KEY"]
    MODEL_NAME: str = os.environ.get("MODEL_NAME", "deepseek-ai/deepseek-v4-pro")

    # --- Email MCP ---
    EMAIL_MCP_URL: str = os.environ.get("EMAIL_MCP_URL", "http://localhost:8100")
    SMTP_HOST: str = os.environ.get("SMTP_HOST", "")
    SMTP_PORT: int = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER: str = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD: str = os.environ.get("SMTP_PASSWORD", "")

    # --- Behavior ---
    MAX_FEEDBACK_RETRIES: int = int(os.environ.get("MAX_FEEDBACK_RETRIES", "3"))
    CHECKPOINT_DB_PATH: str = os.environ.get("CHECKPOINT_DB_PATH", "checkpoints.sqlite")


settings = Settings()
