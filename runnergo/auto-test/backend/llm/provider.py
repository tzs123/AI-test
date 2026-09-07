"""OpenAI Compatible LLM Provider。

配置（环境变量，不写死）：
    MODEL_PROVIDER   模型服务商标识，例如 openai / ollama / deepseek / 自定义
    BASE_URL         OpenAI Compatible Base URL，例如 https://api.openai.com/v1
    API_KEY          API Key
    MODEL_NAME       模型名称，例如 gpt-4o-mini / qwen2.5:7b

兼容旧配置：未设置 MODEL_NAME 时回退到 AI_AGENT_MODEL，
未设置 BASE_URL 时回退到 AI_AGENT_BASE_URL，API_KEY 同理。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import requests


class ModelProviderError(RuntimeError):
    """模型调用失败。"""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def get_provider_config() -> dict:
    """读取模型配置；未配置 MODEL_NAME/AI_AGENT_MODEL 时返回空 dict。"""
    model = _env("MODEL_NAME") or _env("AI_AGENT_MODEL")
    if not model:
        return {}
    return {
        "provider": _env("MODEL_PROVIDER", "custom"),
        "model": model,
        "base_url": (
            _env("BASE_URL")
            or _env("AI_AGENT_BASE_URL")
            or "http://ollama:11434/v1"
        ).rstrip("/"),
        "api_key": _env("API_KEY") or _env("AI_AGENT_API_KEY") or "ollama",
        "timeout": float(_env("AI_AGENT_TIMEOUT", "45") or 45),
        "max_tokens": int(_env("AI_AGENT_MAX_TOKENS", "1024") or 1024),
        "temperature": float(_env("AI_AGENT_TEMPERATURE", "0.2") or 0.2),
    }


def is_configured() -> bool:
    return bool(get_provider_config())


def chat(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    json_mode: bool = True,
) -> str:
    """调用 OpenAI Compatible chat/completions，返回模型文本。"""
    config = get_provider_config()
    if not config:
        raise ModelProviderError("未配置模型（MODEL_NAME / AI_AGENT_MODEL）")
    base_url = config["base_url"]
    if base_url.endswith("/chat/completions"):
        url = base_url
    elif base_url.endswith("/v1"):
        url = f"{base_url}/chat/completions"
    else:
        url = f"{base_url}/v1/chat/completions"
    payload: dict[str, Any] = {
        "model": config["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": config["temperature"] if temperature is None else temperature,
        "max_tokens": min(
            config["max_tokens"], int(max_tokens or config["max_tokens"])
        ),
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {config['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=config["timeout"])
        response.raise_for_status()
        data = response.json()
        return str(data["choices"][0]["message"]["content"])
    except ModelProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一包装为 Provider 错误
        raise ModelProviderError(f"模型调用失败: {exc}") from exc


def extract_json_object(content: str) -> dict:
    """从模型输出中提取 JSON 对象（容忍 markdown 代码块与前后噪声）。"""
    text = str(content or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            pass
    return {}


def chat_json(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> dict:
    """调用模型并解析为 JSON 对象；解析失败抛 ModelProviderError。"""
    content = chat(
        system_prompt,
        user_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        json_mode=True,
    )
    result = extract_json_object(content)
    if not result:
        raise ModelProviderError(f"模型未返回合法 JSON: {content[:200]}")
    return result
