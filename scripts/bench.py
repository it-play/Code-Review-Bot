"""Phase 0 벤치마크 — 로컬에서 gemma4가 실제로 얼마나 나오는지 잰다.

운영과 같은 코드 경로(LLMClient + build_messages)를 쓴다. 벤치마크만 따로 만들면
측정치가 실제와 어긋나서 SLO 근거로 못 쓴다.

MacBook Air는 팬리스라 지속 부하에서 클럭이 떨어진다. 그래서 첫 측정만으로는
부족하고 `--sustain` 으로 연속 부하 후 수치를 따로 본다.

    uv run python -m scripts.bench
    uv run python -m scripts.bench --model gemma4:12b
    uv run python -m scripts.bench --sustain 300
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time

from crbot.config import get_settings
from crbot.llm import CompletionResult, LLMClient
from crbot.review.diff import parse_patch
from crbot.review.prompt import build_messages
from scripts.fixtures import ALL, Fixture


async def run_one(llm: LLMClient, fixture: Fixture) -> CompletionResult:
    messages = build_messages(path=fixture.path, hunks=parse_patch(fixture.patch))
    return await llm.complete(messages, max_tokens=700, temperature=0.1)


def summarize(label: str, results: list[CompletionResult]) -> None:
    if not results:
        return
    med = statistics.median
    latencies = [r.total_s for r in results]
    prefill = [r.first_token_s for r in results]
    thinking = [r.thinking_s for r in results]
    speeds = [r.tokens_per_s for r in results if r.tokens_per_s > 0]
    out_tokens = [r.completion_tokens for r in results]
    empty = sum(1 for r in results if not r.text.strip())
    truncated = sum(1 for r in results if r.truncated)

    print(f"\n  {label}  (n={len(results)})")
    print(f"    총 시간    중앙 {med(latencies):6.2f}s   "
          f"최소 {min(latencies):5.2f}s   최대 {max(latencies):5.2f}s")
    print(f"    prefill    중앙 {med(prefill):6.2f}s   (첫 토큰까지 — 종류 불문)")
    if max(thinking) > 0.01:
        print(f"    사고       중앙 {med(thinking):6.2f}s   최대 {max(thinking):5.2f}s "
              f"  <- 본문을 시작하기까지 낭비한 시간")
    if speeds:
        print(f"    생성 속도  중앙 {med(speeds):6.1f} tok/s  최소 {min(speeds):5.1f} tok/s")
    print(f"    생성 토큰  중앙 {med(out_tokens):6.0f}개  (사고 포함)")
    if truncated:
        print(f"    !! max_tokens에 걸려 잘림: {truncated}/{len(results)}회")
    if empty:
        print(f"    !! 본문이 빈 응답: {empty}/{len(results)}회")


async def warmup(llm: LLMClient) -> bool:
    """모델을 메모리에 올린다. 첫 호출은 로딩 시간이 섞여서 측정에 못 쓴다."""
    print("  모델 로딩 중...", end="", flush=True)
    started = time.perf_counter()
    try:
        await llm.complete([{"role": "user", "content": "1+1은?"}], max_tokens=8)
    except Exception as exc:  # noqa: BLE001
        print(f" 실패: {exc}")
        return False
    print(f" {time.perf_counter() - started:.1f}s")
    return True


async def bench(model: str, rounds: int, sustain_s: float) -> None:
    settings = get_settings()
    print(f"모델: {model}")
    print(f"엔드포인트: {settings.llm_base_url}")

    async with LLMClient(
        base_url=settings.llm_base_url,
        model=model,
        api_key=settings.llm_api_key,
        timeout_s=settings.llm_timeout_s,
        reasoning_effort=settings.llm_reasoning_effort,
    ) as llm:
        if not await llm.health():
            print("LLM 서버에 연결할 수 없습니다. `ollama serve` 실행 여부를 확인하세요.")
            return
        if not await warmup(llm):
            return

        print("\n=== 픽스처별 측정 ===")
        first_results: list[CompletionResult] = []
        for fixture in ALL:
            results = [await run_one(llm, fixture) for _ in range(rounds)]
            first_results.extend(results)
            summarize(f"{fixture.name} ({fixture.path})", results)
            print(f"    기대: {fixture.summary}")

        summarize("전체", first_results)

        if sustain_s > 0:
            print(f"\n=== 지속 부하 {sustain_s:.0f}초 (팬리스 스로틀링 확인) ===")
            deadline = time.monotonic() + sustain_s
            late: list[CompletionResult] = []
            while time.monotonic() < deadline:
                late.append(await run_one(llm, ALL[1]))
                print(f"    {len(late)}회 완료", end="\r", flush=True)
            summarize("지속 부하 후", late)

            early_speed = statistics.median(
                [r.tokens_per_s for r in first_results if r.tokens_per_s > 0]
            )
            late_speed = statistics.median([r.tokens_per_s for r in late if r.tokens_per_s > 0])
            drop = (1 - late_speed / early_speed) * 100 if early_speed else 0
            print(f"\n  초기 대비 생성 속도 변화: {drop:+.1f}% "
                  f"({early_speed:.1f} -> {late_speed:.1f} tok/s)")

        print("\n=== 리뷰 출력 샘플 (한국어 품질 확인용) ===")
        for fixture in ALL:
            result = await run_one(llm, fixture)
            print(f"\n--- {fixture.name} / {fixture.summary} ---")
            print(result.text.strip() or "(출력 없음)")


def main() -> None:
    # 파일로 리다이렉트하면 stdout이 블록 버퍼링돼서 진행이 안 보인다
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=None, help="기본값은 .env의 LLM_MODEL")
    parser.add_argument("--rounds", type=int, default=3, help="픽스처당 반복 횟수")
    parser.add_argument("--sustain", type=float, default=0, help="지속 부하 시간(초)")
    args = parser.parse_args()

    model = args.model or get_settings().llm_model
    asyncio.run(bench(model, args.rounds, args.sustain))


if __name__ == "__main__":
    main()
