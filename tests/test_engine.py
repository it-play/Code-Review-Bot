"""리뷰 엔진 통합 테스트 (PLAN.md L2 — LLM 없이).

검증 대상: 진행 상태 갱신, 건너뛴 파일 처리, 범위 밖 지적 폐기, 부분 실패 복원력.
"""

from crbot.config import Settings
from crbot.review.engine import ReviewEngine
from crbot.review.models import Finding, Severity
from tests.conftest import make_file

# 유효한 head 줄 번호는 10, 11, 12, 13 (문맥 2줄 + 추가 2줄)
PATCH = """@@ -10,3 +10,5 @@ export function load() {
 const a = 1;
+const b = await fetch(url);
+const c = b.json();
 return a;"""


def ts_file(name: str = "src/a.ts", additions: int = 2, **kwargs):
    return make_file(name, additions=additions, patch=PATCH, **kwargs)


class ScriptedAnalyzer:
    """지정한 지적을 그대로 돌려준다. 호출된 파일을 기록한다."""

    def __init__(self, findings_by_path=None, raises_on=None):
        self.findings_by_path = findings_by_path or {}
        self.raises_on = raises_on or set()
        self.calls: list[str] = []

    async def analyze(self, path, hunks, context=""):
        self.calls.append(path)
        if path in self.raises_on:
            raise RuntimeError("모델 호출 실패")
        return list(self.findings_by_path.get(path, []))


def make_engine(analyzer, **overrides) -> ReviewEngine:
    settings = Settings(_env_file=None, **overrides)
    return ReviewEngine(llm=None, settings=settings, analyzer=analyzer)


class TestHappyPath:
    async def test_findings_are_returned_and_counted(self, reporter, pull_request):
        analyzer = ScriptedAnalyzer(
            {"src/a.ts": [Finding("src/a.ts", 11, Severity.HIGH, "await 누락", "본문")]}
        )
        result = await make_engine(analyzer).review(pull_request, [ts_file()], reporter)

        assert len(result.findings) == 1
        assert result.findings[0].line == 11
        assert "1건을 지적했습니다" in result.summary

    async def test_clean_review_says_nothing_found(self, reporter, pull_request):
        result = await make_engine(ScriptedAnalyzer()).review(
            pull_request, [ts_file()], reporter
        )
        assert result.findings == []
        assert "찾지 못했습니다" in result.summary

    async def test_progress_marks_every_unit_done(self, reporter, pull_request):
        files = [ts_file("a.ts"), ts_file("b.ts")]
        await make_engine(ScriptedAnalyzer()).review(pull_request, files, reporter)
        assert all(u.done for u in reporter.state.units)
        assert reporter.state.done_units == 2


class TestSkipping:
    async def test_lockfile_is_skipped_without_analysis(self, reporter, pull_request):
        analyzer = ScriptedAnalyzer()
        files = [ts_file("src/a.ts"), ts_file("pnpm-lock.yaml", additions=5000)]
        result = await make_engine(analyzer).review(pull_request, files, reporter)

        assert analyzer.calls == ["src/a.ts"], "잠금 파일은 모델에 보내지 않는다"
        assert ("pnpm-lock.yaml", "잠금 파일") in result.skipped

    async def test_skipped_file_stays_out_of_denominator(self, reporter, pull_request):
        files = [ts_file("src/a.ts", additions=10), ts_file("yarn.lock", additions=5000)]
        await make_engine(ScriptedAnalyzer()).review(pull_request, files, reporter)
        # 5000줄짜리 잠금 파일이 분모를 지배하면 남은 분수가 의미를 잃는다
        assert reporter.state.total_weight == 10

    async def test_binary_file_is_skipped(self, reporter, pull_request):
        analyzer = ScriptedAnalyzer()
        files = [make_file("logo.png", patch=None)]
        await make_engine(analyzer).review(pull_request, files, reporter)
        assert analyzer.calls == []


class TestLineValidation:
    async def test_out_of_range_finding_is_dropped(self, reporter, pull_request):
        # 모델이 diff에 없는 줄을 지목했다. 올리면 리뷰 전체가 422다.
        analyzer = ScriptedAnalyzer(
            {"src/a.ts": [Finding("src/a.ts", 999, Severity.HIGH, "엉뚱한 줄", "본문")]}
        )
        result = await make_engine(analyzer).review(pull_request, [ts_file()], reporter)
        assert result.findings == []

    async def test_near_miss_is_snapped_into_range(self, reporter, pull_request):
        # 유효 줄은 10~13. 15는 두 칸 밖(허용 ±3)이므로 13으로 당긴다.
        analyzer = ScriptedAnalyzer(
            {"src/a.ts": [Finding("src/a.ts", 15, Severity.LOW, "근처", "본문")]}
        )
        result = await make_engine(analyzer).review(pull_request, [ts_file()], reporter)
        assert len(result.findings) == 1
        assert result.findings[0].line == 13


class TestResilience:
    async def test_one_file_failure_does_not_abort_the_review(self, reporter, pull_request):
        analyzer = ScriptedAnalyzer(
            findings_by_path={"b.ts": [Finding("b.ts", 11, Severity.LOW, "정상 지적", "본문")]},
            raises_on={"a.ts"},
        )
        files = [ts_file("a.ts"), ts_file("b.ts")]
        result = await make_engine(analyzer).review(pull_request, files, reporter)
        assert len(result.findings) == 1
        assert result.findings[0].path == "b.ts"

    async def test_failed_file_is_still_marked_done(self, reporter, pull_request):
        analyzer = ScriptedAnalyzer(raises_on={"a.ts"})
        await make_engine(analyzer).review(pull_request, [ts_file("a.ts")], reporter)
        # 완료 표시가 안 되면 진행 상태가 영원히 멈춘 것처럼 보인다
        assert reporter.state.done_units == 1


class TestFallbackMode:
    async def test_large_pr_enters_fallback(self, reporter, pull_request):
        files = [ts_file(f"f{i}.ts", additions=100) for i in range(5)]
        result = await make_engine(ScriptedAnalyzer(), review_max_lines=300).review(
            pull_request, files, reporter
        )
        assert result.fallback_mode
        assert "요약 모드" in result.summary

    async def test_small_pr_does_not(self, reporter, pull_request):
        result = await make_engine(ScriptedAnalyzer(), review_max_lines=300).review(
            pull_request, [ts_file(additions=5)], reporter
        )
        assert not result.fallback_mode

    async def test_fallback_caps_findings_per_file(self, reporter, pull_request):
        many = [Finding("src/a.ts", 11, Severity.LOW, f"지적{i}", "본문") for i in range(5)]
        files = [ts_file("src/a.ts", additions=400)]
        result = await make_engine(
            ScriptedAnalyzer({"src/a.ts": many}), review_max_lines=300
        ).review(pull_request, files, reporter)
        assert result.fallback_mode
        assert len(result.findings) == 2
