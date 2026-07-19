"""
Thin wrapper around ChatNVIDIA so the rest of the codebase doesn't need
to know model-specific kwargs.
"""
from langchain_nvidia_ai_endpoints import ChatNVIDIA

from app.config import settings

_client: ChatNVIDIA | None = None


def get_llm() -> ChatNVIDIA:
    global _client
    if _client is None:
        _client = ChatNVIDIA(
            model=settings.MODEL_NAME,
            api_key=settings.NVIDIA_API_KEY,
            temperature=0.4,       # lower than the default 1.0 — this is a factual summarization task
            top_p=0.95,
            max_tokens=2048,       # a changelog entry doesn't need 16384
            extra_body={"chat_template_kwargs": {"thinking": False}},
        )
    return _client
