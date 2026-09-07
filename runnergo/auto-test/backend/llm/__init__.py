"""LLM 调用封装（OpenAI Compatible）。"""

from backend.llm.provider import (  # noqa: F401
    ModelProviderError,
    chat,
    chat_json,
    get_provider_config,
    is_configured,
)
