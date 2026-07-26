"""LLM 클라이언트 테스트 (PLAN.md L1).

gemma4는 추론 모델이라 사고 과정을 `delta.reasoning`으로 따로 흘린다.
이걸 본문과 섞거나 무시하면 계측이 통째로 거짓이 된다 — 실제로 그랬다.
사고에 28초를 쓰고 본문을 한 글자도 못 쓴 호출이 "TTFT 28초"로 기록되면서
프롬프트 처리가 느린 것처럼 보였다.
"""

import httpx
import pytest
import respx

from crbot.llm import LLMClient

URL = "http://llm.test/v1/chat/completions"


def sse(*events: str) -> str:
    return "".join(f"data: {e}\n\n" for e in events) + "data: [DONE]\n\n"


def delta(content: str = "", reasoning: str = "") -> str:
    fields = {}
    if content:
        fields["content"] = content
    if reasoning:
        fields["reasoning"] = reasoning
    import json

    return json.dumps({"choices": [{"index": 0, "delta": fields, "finish_reason": None}]})


def finish(reason: str = "stop", prompt: int = 10, completion: int = 5) -> str:
    import json

    return json.dumps(
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
        }
    )


@pytest.fixture
def llm():
    return LLMClient("http://llm.test/v1", "gemma4:26b")


class TestReasoningSeparation:
    @respx.mock
    async def test_reasoning_is_kept_out_of_text(self, llm):
        respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                text=sse(
                    delta(reasoning="사용자가 코드를 물어봤다. 음..."),
                    delta(content="await가 누락됐습니다."),
                    finish(completion=20),
                ),
            )
        )
        async with llm:
            result = await llm.complete([{"role": "user", "content": "x"}])

        assert result.text == "await가 누락됐습니다."
        assert "음..." in result.reasoning
        assert "음..." not in result.text, "사고가 리뷰 본문에 새면 안 된다"

    @respx.mock
    async def test_thinking_time_is_separated_from_prefill(self, llm):
        respx.post(URL).mock(
            return_value=httpx.Response(
                200, text=sse(delta(reasoning="생각"), delta(content="답"), finish())
            )
        )
        async with llm:
            result = await llm.complete([{"role": "user", "content": "x"}])

        # 첫 토큰(사고)이 본문보다 먼저 왔으므로 prefill < ttft
        assert result.first_token_s <= result.ttft_s
        assert result.thinking_s == pytest.approx(result.ttft_s - result.first_token_s)

    @respx.mock
    async def test_reasoning_only_response_is_visible_as_empty_and_truncated(self, llm):
        """사고가 예산을 다 먹고 본문을 못 쓴 경우. 조용히 넘어가면 안 된다."""
        respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                text=sse(
                    delta(reasoning="한참 생각하는 중..." * 20),
                    finish(reason="length", completion=700),
                ),
            )
        )
        async with llm:
            result = await llm.complete([{"role": "user", "content": "x"}], max_tokens=700)

        assert result.text == ""
        assert result.truncated, "max_tokens에 걸린 사실이 드러나야 한다"
        assert result.reasoning

    @respx.mock
    async def test_reasoning_content_field_is_also_handled(self, llm):
        # 서버에 따라 reasoning_content 를 쓴다
        import json

        event = json.dumps(
            {"choices": [{"delta": {"reasoning_content": "생각"}, "finish_reason": None}]}
        )
        respx.post(URL).mock(
            return_value=httpx.Response(200, text=sse(event, delta(content="답"), finish()))
        )
        async with llm:
            result = await llm.complete([{"role": "user", "content": "x"}])
        assert result.reasoning == "생각"
        assert result.text == "답"


class TestMetrics:
    @respx.mock
    async def test_usage_is_parsed(self, llm):
        respx.post(URL).mock(
            return_value=httpx.Response(
                200, text=sse(delta(content="답"), finish(prompt=123, completion=45))
            )
        )
        async with llm:
            result = await llm.complete([{"role": "user", "content": "x"}])
        assert (result.prompt_tokens, result.completion_tokens) == (123, 45)

    @respx.mock
    async def test_speed_is_zero_when_decode_window_is_too_small(self, llm):
        """decode_s가 0에 수렴할 때 나누면 수천 tok/s 같은 쓰레기 값이 나온다."""
        respx.post(URL).mock(
            return_value=httpx.Response(200, text=sse(delta(content="답"), finish(completion=700)))
        )
        async with llm:
            result = await llm.complete([{"role": "user", "content": "x"}])
        # 모킹은 즉시 끝나므로 decode 구간이 사실상 0이다
        assert result.tokens_per_s == 0.0

    @respx.mock
    async def test_completion_tokens_estimated_when_usage_missing(self, llm):
        respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                text=sse(
                    delta(content="상당히 긴 응답 본문입니다"),
                    '{"choices":[{"delta":{},"finish_reason":"stop"}]}',
                ),
            )
        )
        async with llm:
            result = await llm.complete([{"role": "user", "content": "x"}])
        assert result.completion_tokens > 0


class TestRequestShape:
    @respx.mock
    async def test_reasoning_effort_is_sent(self):
        route = respx.post(URL).mock(
            return_value=httpx.Response(200, text=sse(delta(content="답"), finish()))
        )
        async with LLMClient("http://llm.test/v1", "gemma4:26b", reasoning_effort="none") as llm:
            await llm.complete([{"role": "user", "content": "x"}])

        import json

        body = json.loads(route.calls[0].request.content)
        assert body["reasoning_effort"] == "none", "사고를 끄는 설정이 실제로 전달돼야 한다"

    @respx.mock
    async def test_reasoning_effort_omitted_when_blank(self):
        route = respx.post(URL).mock(
            return_value=httpx.Response(200, text=sse(delta(content="답"), finish()))
        )
        async with LLMClient("http://llm.test/v1", "m", reasoning_effort="") as llm:
            await llm.complete([{"role": "user", "content": "x"}])

        import json

        assert "reasoning_effort" not in json.loads(route.calls[0].request.content)

    @respx.mock
    async def test_max_tokens_and_temperature_are_sent(self):
        route = respx.post(URL).mock(
            return_value=httpx.Response(200, text=sse(delta(content="답"), finish()))
        )
        async with LLMClient("http://llm.test/v1", "m") as llm:
            await llm.complete([{"role": "user", "content": "x"}], max_tokens=300, temperature=0.1)

        import json

        body = json.loads(route.calls[0].request.content)
        assert body["max_tokens"] == 300
        assert body["temperature"] == 0.1
        assert body["stream"] is True


class TestStreaming:
    @respx.mock
    async def test_stream_yields_content_deltas_only(self, llm):
        respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                text=sse(
                    delta(reasoning="생각은 흘리지 않는다"),
                    delta(content="첫"),
                    delta(content=" 번째"),
                    finish(),
                ),
            )
        )
        async with llm:
            chunks = [c async for c in llm.stream([{"role": "user", "content": "x"}])]
        assert chunks == ["첫", " 번째"]
