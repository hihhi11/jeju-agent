"""LLM 호출. 루프는 Conversation 의 두 메서드와 Turn 만 알면 되어 모델을 바꿔도 루프는 그대로다.

LLM_PROVIDER 로 고른다. gemini 와 ollama 는 같은 OpenAI 호환 방식이고 claude 는 전용 SDK를 쓴다.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Protocol

import anthropic
import requests

from agent.common import PROJECT_ROOT  # noqa: F401  .env 를 읽기 위해 먼저 불러온다

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
DEFAULT_CLAUDE_EFFORT = "medium"
MAX_TOKENS = 16000
# 안전 분류기가 요청을 거절하면 서버가 다른 모델로 다시 실행한다
FALLBACK_BETA = "server-side-fallback-2026-07-01"

DEFAULT_PROVIDER = "gemini"
OPENAI_COMPATIBLE = {
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY", "model": "gemini-3.6-flash"},
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": None, "model": None},
}
# 무료 한도의 분당 제한에 걸리면 기다렸다가 다시 보낸다
MAX_ATTEMPTS = 4
RETRY_WAIT_SECONDS = 20
RETRY_STATUS = {429, 500, 502, 503, 504}
# 로컬 모델은 응답이 느릴 수 있다
REQUEST_TIMEOUT_SECONDS = 300


class LLMError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 호출 실패."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ToolOutcome:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall]
    # done: 답변 끝, tool_use: 도구 실행 필요, refused: 거절, truncated: 길이 초과로 잘림
    stop: str
    usage: dict = field(default_factory=dict)


class Conversation(Protocol):
    def send_user(self, text: str) -> Turn: ...

    def send_tool_results(self, outcomes: list[ToolOutcome]) -> Turn: ...


_STOP = {"end_turn": "done", "stop_sequence": "done", "tool_use": "tool_use", "refusal": "refused", "max_tokens": "truncated"}


class ClaudeConversation:
    """Claude 대화 하나. 이전 응답 블록을 그대로 다시 보내야 해서 기록을 덧붙이기만 한다."""

    def __init__(self, system: str, tools: list[dict], model: str | None = None, effort: str | None = None, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic()
        self.model = model or os.getenv("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL)
        self.effort = effort or os.getenv("CLAUDE_EFFORT", DEFAULT_CLAUDE_EFFORT)
        self.system = system
        self.tools = tools
        self.messages: list[dict] = []

    def send_user(self, text: str) -> Turn:
        self.messages.append({"role": "user", "content": text})
        return self._call()

    def send_tool_results(self, outcomes: list[ToolOutcome]) -> Turn:
        blocks = [{"type": "tool_result", "tool_use_id": o.call_id, "content": o.content, **({"is_error": True} if o.is_error else {})} for o in outcomes]
        self.messages.append({"role": "user", "content": blocks})
        return self._call()

    def _call(self) -> Turn:
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                cache_control={"type": "ephemeral"},
                output_config={"effort": self.effort},
                system=self.system,
                tools=self.tools,
                messages=self.messages,
            )
        except anthropic.AuthenticationError as exc:
            raise LLMError("Claude API 인증에 실패했습니다. .env 의 ANTHROPIC_API_KEY 를 확인해 주세요.") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Claude API 호출 한도를 넘었습니다. 잠시 뒤 다시 시도해 주세요.") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Claude API에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요.") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API 오류입니다 (HTTP {exc.status_code}, 요청 {exc.request_id}).") from exc

        self.messages.append({"role": "assistant", "content": response.content})
        text = "".join(b.text for b in response.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, b.input) for b in response.content if b.type == "tool_use"]
        return Turn(
            text=text,
            tool_calls=calls,
            stop=_STOP.get(response.stop_reason, "truncated"),
            usage={
                "inputTokens": response.usage.input_tokens,
                "outputTokens": response.usage.output_tokens,
                "cacheReadTokens": response.usage.cache_read_input_tokens or 0,
                "cacheWriteTokens": response.usage.cache_creation_input_tokens or 0,
            },
        )


class OpenAICompatibleConversation:
    """/chat/completions 를 쓰는 모델과의 대화. 모델이 준 assistant 메시지는 고치지 않고 그대로 다시 보낸다."""

    def __init__(self, system: str, tools: list[dict], base_url: str, model: str, api_key: str | None = None, provider: str = "모델", retry_wait: float = RETRY_WAIT_SECONDS):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.provider = provider
        self.retry_wait = retry_wait
        self.tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]
        self.messages: list[dict] = [{"role": "system", "content": system}]

    def send_user(self, text: str) -> Turn:
        self.messages.append({"role": "user", "content": text})
        return self._call()

    def send_tool_results(self, outcomes: list[ToolOutcome]) -> Turn:
        self.messages.extend({"role": "tool", "tool_call_id": o.call_id, "content": o.content} for o in outcomes)
        return self._call()

    def _post(self) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model, "messages": self.messages, "tools": self.tools}
        for attempt in range(MAX_ATTEMPTS):
            last_try = attempt == MAX_ATTEMPTS - 1
            try:
                response = requests.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
            except requests.RequestException as exc:
                if last_try:
                    raise LLMError(f"{self.provider} 에 연결하지 못했습니다. 인터넷 연결이나 실행 상태를 확인해 주세요.") from exc
                time.sleep(self.retry_wait)
                continue
            if response.ok:
                return response.json()
            if response.status_code in (401, 403):
                raise LLMError(f"{self.provider} 인증에 실패했습니다. .env 의 키를 확인해 주세요.")
            # 하루 한도는 기다려도 풀리지 않는다
            if response.status_code not in RETRY_STATUS or last_try or "per day" in response.text:
                if response.status_code == 429:
                    raise LLMError(f"{self.provider} 사용 한도를 넘었습니다. 잠시 뒤나 내일 다시 시도해 주세요.")
                raise LLMError(f"{self.provider} 오류입니다 (HTTP {response.status_code}): {response.text[:200]}")
            time.sleep(self.retry_wait)
        raise LLMError(f"{self.provider} 응답을 받지 못했습니다.")

    def _call(self) -> Turn:
        data = self._post()
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.provider} 가 빈 응답을 보냈습니다.")
        message, finish = choices[0]["message"], choices[0].get("finish_reason")
        self.messages.append(message)
        calls = [ToolCall(c["id"], c["function"]["name"], _parse_arguments(c["function"].get("arguments"))) for c in message.get("tool_calls") or []]
        if finish == "length":
            stop = "truncated"
        elif finish == "content_filter":
            stop = "refused"
        elif calls:
            stop = "tool_use"
        else:
            stop = "done"
        usage = data.get("usage") or {}
        return Turn(
            text=message.get("content") or "",
            tool_calls=calls,
            stop=stop,
            usage={"inputTokens": usage.get("prompt_tokens", 0), "outputTokens": usage.get("completion_tokens", 0)},
        )


def _parse_arguments(raw) -> dict:
    """인자를 읽는다. 깨진 JSON 은 입력 확인에서 걸려 모델에게 되돌아가도록 그대로 담는다."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"unparsedArguments": raw}
    return parsed if isinstance(parsed, dict) else {"unparsedArguments": raw}


def current_model() -> tuple[str, str | None]:
    """지금 설정의 (제공자, 모델)."""
    provider = os.getenv("LLM_PROVIDER", DEFAULT_PROVIDER)
    if provider == "claude":
        return provider, os.getenv("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL)
    if provider not in OPENAI_COMPATIBLE:
        raise LLMError(f"LLM_PROVIDER 는 claude, {', '.join(OPENAI_COMPATIBLE)} 중 하나여야 합니다: {provider}")
    return provider, os.getenv("LLM_MODEL") or OPENAI_COMPATIBLE[provider]["model"]


def make_conversation(system: str, tools: list[dict]) -> Conversation:
    provider, model = current_model()
    if provider == "claude":
        return ClaudeConversation(system, tools)
    config = OPENAI_COMPATIBLE[provider]
    if not model:
        raise LLMError(f"{provider} 는 .env 의 LLM_MODEL 에 모델 이름을 지정해야 합니다.")
    api_key = os.getenv(config["key_env"]) if config["key_env"] else None
    if config["key_env"] and not api_key:
        raise LLMError(f".env 에 {config['key_env']} 가 없습니다.")
    return OpenAICompatibleConversation(system, tools, os.getenv("LLM_BASE_URL") or config["base_url"], model, api_key, provider)
