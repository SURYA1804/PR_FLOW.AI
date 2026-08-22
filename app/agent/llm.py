from langchain_nvidia_ai_endpoints import ChatNVIDIA
from langchain_groq import ChatGroq
from app.config import settings


_client = None


def get_llm():
    global _client

    if _client is None:

        if settings.USE_GROQ:
            _client = ChatGroq(
                model=settings.GROQ_MODEL_NAME,
                api_key=settings.GROQ_API_KEY,
                temperature=0.4,
                max_tokens=2048,
            )

        else:
            _client = ChatNVIDIA(
                model=settings.NVIDIA_MODEL_NAME,
                api_key=settings.NVIDIA_API_KEY,
                temperature=0.4,
                top_p=0.95,
                max_tokens=2048,
                extra_body={
                    "chat_template_kwargs": {
                        "thinking": False
                    }
                },
            )

    return _client