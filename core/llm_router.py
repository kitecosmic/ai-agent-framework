"""
LLM Router — Capa de abstracción para múltiples proveedores de LLM.

Permite cambiar entre Anthropic, OpenAI (u otros) sin cambiar código.
Soporta streaming, tool calling, y structured output.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import structlog

logger = structlog.get_logger()


@dataclass
class LLMMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMResponse:
    content: str
    model: str
    provider: str
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)
    raw: Any = None


class LLMProvider(ABC):
    """Interfaz base para proveedores de LLM."""

    name: str

    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse: ...

    @abstractmethod
    async def stream(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        **kwargs,
    ) -> AsyncIterator[str]: ...


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, default_model: str = "claude-sonnet-4-20250514"):
        import anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.default_model = default_model

    async def complete(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        # Separar system message
        system = ""
        api_messages = []
        for msg in messages:
            if msg.role == "system":
                system = msg.content
            else:
                api_messages.append({"role": msg.role, "content": msg.content})

        params: dict[str, Any] = {
            "model": model or self.default_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": api_messages,
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = tools

        response = await self.client.messages.create(**params)

        return LLMResponse(
            content=response.content[0].text if response.content else "",
            model=response.model,
            provider=self.name,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            raw=response,
        )

    async def stream(self, messages: list[LLMMessage], model: str | None = None, **kwargs) -> AsyncIterator[str]:
        system = ""
        api_messages = []
        for msg in messages:
            if msg.role == "system":
                system = msg.content
            else:
                api_messages.append({"role": msg.role, "content": msg.content})

        params: dict[str, Any] = {
            "model": model or self.default_model,
            "max_tokens": kwargs.get("max_tokens", 4096),
            "messages": api_messages,
        }
        if system:
            params["system"] = system

        async with self.client.messages.stream(**params) as stream:
            async for text in stream.text_stream:
                yield text


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, default_model: str = "gpt-4o"):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(api_key=api_key)
        self.default_model = default_model

    async def complete(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        api_messages = [{"role": m.role, "content": m.content} for m in messages]

        params: dict[str, Any] = {
            "model": model or self.default_model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": api_messages,
        }
        if tools:
            params["tools"] = tools

        response = await self.client.chat.completions.create(**params)
        choice = response.choices[0]

        return LLMResponse(
            content=choice.message.content or "",
            model=response.model,
            provider=self.name,
            usage={
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens if response.usage else 0,
            },
            raw=response,
        )

    async def stream(self, messages: list[LLMMessage], model: str | None = None, **kwargs) -> AsyncIterator[str]:
        api_messages = [{"role": m.role, "content": m.content} for m in messages]
        stream = await self.client.chat.completions.create(
            model=model or self.default_model,
            messages=api_messages,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content


class OllamaProvider(LLMProvider):
    """Provider para modelos locales via Ollama (compatible con OpenAI API)."""
    name = "ollama"

    def __init__(self, base_url: str = "http://localhost:11434", default_model: str = "deepseek-r1:14b"):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(
            api_key="ollama",  # Ollama no requiere API key real
            base_url=f"{base_url}/v1",
        )
        self.default_model = default_model

    async def complete(
        self,
        messages: list[LLMMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        api_messages = [{"role": m.role, "content": m.content} for m in messages]

        params: dict[str, Any] = {
            "model": model or self.default_model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": api_messages,
        }
        if tools:
            params["tools"] = tools

        response = await self.client.chat.completions.create(**params)
        choice = response.choices[0]

        return LLMResponse(
            content=choice.message.content or "",
            model=response.model,
            provider=self.name,
            usage={
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens if response.usage else 0,
            },
            raw=response,
        )

    async def stream(self, messages: list[LLMMessage], model: str | None = None, **kwargs) -> AsyncIterator[str]:
        api_messages = [{"role": m.role, "content": m.content} for m in messages]
        stream = await self.client.chat.completions.create(
            model=model or self.default_model,
            messages=api_messages,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content


class LLMRouter:
    """
    Router que gestiona múltiples proveedores y permite cambiar
    entre ellos dinámicamente.
    """

    def __init__(self):
        self._providers: dict[str, LLMProvider] = {}
        self._default: str = ""

    def add_provider(self, provider: LLMProvider, default: bool = False):
        self._providers[provider.name] = provider
        if default or not self._default:
            self._default = provider.name

    def get_provider(self, name: str | None = None) -> LLMProvider:
        provider_name = name or self._default
        if provider_name not in self._providers:
            raise ValueError(f"Provider '{provider_name}' not registered. Available: {list(self._providers.keys())}")
        return self._providers[provider_name]

    async def complete(self, messages: list[LLMMessage], provider: str | None = None, **kwargs) -> LLMResponse:
        return await self.get_provider(provider).complete(messages, **kwargs)

    async def stream(self, messages: list[LLMMessage], provider: str | None = None, **kwargs) -> AsyncIterator[str]:
        async for chunk in self.get_provider(provider).stream(messages, **kwargs):
            yield chunk


# Singleton global
llm_router = LLMRouter()
