from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


class LLMProvider:
    """OpenAI Compatible API 封装。

    环境变量:
    MODEL_PROVIDER=openai-compatible
    BASE_URL=https://api.openai.com/v1
    API_KEY=...
    MODEL_NAME=gpt-4.1-mini
    """

    def __init__(
        self,
        provider: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
    ):
        self.provider = provider or getattr(settings, 'MODEL_PROVIDER', os.getenv('MODEL_PROVIDER', 'openai-compatible'))
        self.base_url = (base_url or getattr(settings, 'BASE_URL', os.getenv('BASE_URL', ''))).rstrip('/')
        self.api_key = api_key or getattr(settings, 'API_KEY', os.getenv('API_KEY', ''))
        self.model_name = model_name or getattr(settings, 'MODEL_NAME', os.getenv('MODEL_NAME', ''))
        self.configuration_source = 'environment' if self.base_url and self.api_key and self.model_name else ''
        self.configuration_name = self.model_name
        self.last_error = ''

        if not (base_url or api_key or model_name) and not self.enabled:
            self._load_platform_model()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model_name)

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        *,
        timeout: float = 30.0,
        temperature: float = 0.2,
    ) -> Optional[Any]:
        self.last_error = ''
        if not self.enabled:
            self.last_error = '未配置可用的 AI 模型'
            return None
        retry_count = max(0, min(int(getattr(settings, 'LLM_REQUEST_RETRY_COUNT', 2)), 5))
        for attempt in range(retry_count + 1):
            try:
                response = httpx.post(
                    self._chat_completions_url(),
                    headers={
                        'Authorization': f'Bearer {self.api_key}',
                        'api-key': self.api_key,
                        'Content-Type': 'application/json',
                    },
                    json={
                        'model': self.model_name,
                        'messages': messages,
                        'temperature': temperature,
                        'response_format': {'type': 'json_object'},
                    },
                    timeout=timeout,
                )
                response.raise_for_status()
                content = response.json()['choices'][0]['message']['content']
                return self._parse_json_content(content)
            except httpx.HTTPStatusError as exc:
                self.last_error = str(exc)[:1000]
                if 400 <= exc.response.status_code < 500 or attempt >= retry_count:
                    break
                logger.warning('LLM 网关返回 %s，准备重试', exc.response.status_code)
            except httpx.TimeoutException as exc:
                self.last_error = str(exc)[:1000]
                break
            except httpx.TransportError as exc:
                self.last_error = str(exc)[:1000]
                if attempt >= retry_count:
                    break
                logger.warning('LLM 网络连接中断，准备重试: %s', exc)
            except Exception as exc:
                self.last_error = str(exc)[:1000]
                break
        logger.warning('LLM 调用失败，使用规则降级: %s', self.last_error)
        return None

    def _load_platform_model(self) -> None:
        """Reuse the model configured in the platform instead of silently disabling AI."""
        try:
            from django.db import models

            from apps.requirement_analysis.models import AIModelConfig

            preferred = (
                AIModelConfig.objects.filter(is_active=True)
                .filter(
                    models.Q(model_usage='requirement_analysis')
                    | models.Q(model_usage='general')
                    | models.Q(model_usage='test_case_generation')
                    | models.Q(role='writer')
                )
                .order_by(
                    models.Case(
                        models.When(model_usage='requirement_analysis', then=0),
                        models.When(model_usage='general', then=1),
                        models.When(model_usage='test_case_generation', then=2),
                        default=3,
                    ),
                    '-updated_at',
                )
                .first()
            )
            if preferred is None:
                return
            self.provider = preferred.model_type or self.provider
            self.base_url = str(preferred.base_url or '').rstrip('/')
            self.api_key = preferred.get_api_key()
            self.model_name = str(preferred.model_name or '')
            self.configuration_source = 'platform'
            self.configuration_name = str(preferred.name or preferred.model_name or '')
        except Exception as exc:
            # Model discovery must not make imports or an unavailable DB fatal for
            # callers which deliberately rely on the rules fallback.
            self.last_error = str(exc)[:1000]
            logger.warning('读取平台 AI 模型配置失败: %s', exc)

    def _chat_completions_url(self) -> str:
        base_url = self.base_url.rstrip('/')
        if base_url.endswith('/chat/completions'):
            return base_url
        if re.search(r'/v\d+$', base_url):
            return f'{base_url}/chat/completions'
        return f'{base_url}/v1/chat/completions'

    def _parse_json_content(self, content: Any) -> Any:
        if not isinstance(content, str):
            return content
        text = content.strip()
        fenced = re.search(r'```(?:json)?\s*([\s\S]*?)```', text, flags=re.I)
        if fenced:
            text = fenced.group(1).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Some OpenAI-compatible reasoning gateways emit a short JSON draft
        # followed by the final JSON object. Decode all complete documents and
        # use the last one, which is the model's final answer.
        decoder = json.JSONDecoder()
        documents = []
        offset = 0
        while offset < len(text):
            match = re.search(r'[\[{]', text[offset:])
            if not match:
                break
            start = offset + match.start()
            try:
                document, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                offset = start + 1
                continue
            documents.append(document)
            offset = end
        if documents:
            return documents[-1]
        raise ValueError('模型未返回有效 JSON')
