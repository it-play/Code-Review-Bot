"""OpenAI 호환 LLM 클라이언트.

Ollama(로컬)와 vLLM(배포) 모두 /v1/chat/completions 를 제공하므로 base_url 교체만으로 전환된다.

openai SDK 대신 httpx를 직접 쓰는 이유: 30초 SLO를 관리하려면 TTFT(첫 토큰까지 시간)와
생성 속도를 구간별로 계측해야 하는데, 그 계측을 클라이언트 안에 심어두면
벤치마크와 운영이 같은 코드 경로를 쓰게 된다. 벤치마크만 따로 만들면 실제와 어긋난다.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

Message = dict[str, str]


@dataclass
class CompletionResult:
    """생성 결과 + 계측값. 벤치마크와 운영 로깅이 같은 것을 본다.

    gemma4는 추론 모델이라 사고 과정을 `delta.reasoning`으로 따로 흘린다.
    이걸 구분하지 않으면 계측이 통째로 거짓이 된다 — 사고에 30초를 쓰고 본문을
    한 글자도 못 쓴 호출이 "TTFT 30초"로 기록되면서 마치 프롬프트 처리가
    느린 것처럼 보인다. 실제로 그런 일이 있었다.
    """

    text: str
    reasoning: str = ""
    """모델의 사고 과정. 본문이 아니므로 리뷰 결과에는 쓰지 않는다."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    """사고 토큰을 포함한 전체 생성 토큰."""
    first_token_s: float = 0.0
    """종류를 가리지 않은 첫 토큰까지의 시간. 이것이 진짜 prefill 비용이다."""
    ttft_s: float = 0.0
    """첫 **본문** 토큰까지의 시간. first_token_s와의 차이가 사고에 쓴 시간이다."""
    total_s: float = 0.0
    finish_reason: str = ""
    raw_usage: dict[str, Any] = field(default_factory=dict)

    @property
    def thinking_s(self) -> float:
        """사고에 쓴 시간. 30초 예산에서 가장 큰 단일 비용일 수 있다."""
        return max(self.ttft_s - self.first_token_s, 0.0)

    @property
    def decode_s(self) -> float:
        """첫 토큰 이후 생성에 쓴 시간."""
        return max(self.total_s - self.first_token_s, 0.0)

    @property
    def tokens_per_s(self) -> float:
        """디코딩 속도. 30초 예산에서 출력 토큰 수가 지배적이므로 핵심 지표다."""
        # decode_s가 0에 수렴할 때 나누면 수천 tok/s 같은 쓰레기 값이 나온다
        if self.decode_s < 0.05 or self.completion_tokens <= 0:
            return 0.0
        return self.completion_tokens / self.decode_s

    @property
    def truncated(self) -> bool:
        """max_tokens에 걸려 잘렸는지. 사고가 예산을 다 먹으면 여기가 켜진다."""
        return self.finish_reason == "length"


class LLMClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "ollama",
        timeout_s: float = 120.0,
        reasoning_effort: str = "none",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.reasoning_effort = reasoning_effort
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> LLMClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s))
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("LLMClient must be used as an async context manager")
        return self._client

    def _payload(
        self,
        messages: Sequence[Message],
        *,
        stream: bool,
        max_tokens: int | None,
        temperature: float,
        extra: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "stream": stream,
            "temperature": temperature,
        }
        if self.reasoning_effort:
            # "none"이면 사고를 완전히 끈다. gemma4 기준 4.1s -> 0.6s.
            payload["reasoning_effort"] = self.reasoning_effort
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if stream:
            # Ollama/vLLM 모두 스트리밍 중 usage를 마지막 청크로 실어준다
            payload["stream_options"] = {"include_usage": True}
        if extra:
            payload.update(extra)
        return payload

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """토큰 델타를 순서대로 흘려준다.

        진행률 표시가 이 스트림에 붙는다. 소비자가 중간에 빠져나가면 요청도 함께 끊긴다.
        """
        payload = self._payload(
            messages, stream=True, max_tokens=max_tokens, temperature=temperature, extra=extra
        )
        async with self.client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=self._headers,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                delta = _parse_sse_delta(line)
                if delta:
                    yield delta

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        extra: dict[str, Any] | None = None,
    ) -> CompletionResult:
        """스트리밍으로 받되 전체를 모아서 돌려준다. TTFT와 생성 속도를 함께 계측한다."""
        payload = self._payload(
            messages, stream=True, max_tokens=max_tokens, temperature=temperature, extra=extra
        )

        chunks: list[str] = []
        thoughts: list[str] = []
        usage: dict[str, Any] = {}
        finish_reason = ""
        started = time.perf_counter()
        ttft: float | None = None
        first_token: float | None = None

        async with self.client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=self._headers,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                event = _parse_sse_event(line)
                if event is None:
                    continue
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    # 추론 모델은 사고를 별도 필드로 흘린다. 필드명은 서버마다 다르다.
                    thought = delta.get("reasoning") or delta.get("reasoning_content")

                    if (content or thought) and first_token is None:
                        first_token = time.perf_counter() - started
                    if thought:
                        thoughts.append(thought)
                    if content:
                        if ttft is None:
                            ttft = time.perf_counter() - started
                        chunks.append(content)
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                if event.get("usage"):
                    usage = event["usage"]

        total = time.perf_counter() - started
        text = "".join(chunks)
        reasoning = "".join(thoughts)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        if completion_tokens == 0 and (text or reasoning):
            # usage를 안 주는 서버 대비 근사치. 정확한 값이 아님을 알 수 있게 남긴다.
            completion_tokens = max((len(text) + len(reasoning)) // 3, 1)

        return CompletionResult(
            text=text,
            reasoning=reasoning,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=completion_tokens,
            first_token_s=first_token if first_token is not None else total,
            ttft_s=ttft if ttft is not None else total,
            total_s=total,
            finish_reason=finish_reason,
            raw_usage=usage,
        )

    async def health(self) -> bool:
        try:
            response = await self.client.get(f"{self.base_url}/models", headers=self._headers)
            return response.status_code == 200
        except httpx.HTTPError:
            return False


def _parse_sse_event(line: str) -> dict[str, Any] | None:
    if not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if not data or data == "[DONE]":
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def _parse_sse_delta(line: str) -> str | None:
    event = _parse_sse_event(line)
    if event is None:
        return None
    for choice in event.get("choices") or []:
        delta = (choice.get("delta") or {}).get("content")
        if delta:
            return delta
    return None
