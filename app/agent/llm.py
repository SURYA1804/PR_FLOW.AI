"""
Thin wrapper that switches between ChatGroq and ChatNVIDIA based on settings.USE_GROQ,
so the rest of the codebase doesn't need to know which provider is active.
"""
from langchain_nvidia_ai_endpoints import ChatNVIDIA
from langchain_groq import ChatGroq

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)

_client = None


def get_llm():
    global _client

    if _client is None:
        if settings.USE_GROQ:
            logger.info(f"get_llm: initializing ChatGroq (model={settings.GROQ_MODEL_NAME})")
            _client = ChatGroq(
                model=settings.GROQ_MODEL_NAME,
                api_key=settings.GROQ_API_KEY,
                temperature=0.4,
                max_tokens=2048,
                timeout=45,  # fail fast instead of hanging on a bad model/network issue
            )
        else:
            logger.info(f"get_llm: initializing ChatNVIDIA (model={settings.NVIDIA_MODEL_NAME})")
            _client = ChatNVIDIA(
                model=settings.NVIDIA_MODEL_NAME,
                api_key=settings.NVIDIA_API_KEY,
                temperature=0.4,
                top_p=0.95,
                max_tokens=2048,
                timeout=1,
                extra_body={
                    "chat_template_kwargs": {
                        "thinking": False
                    }
                },
            )

    return _client