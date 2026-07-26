"""진행 상태 표시 테스트 (PLAN.md L2).

핵심 검증: 화면에 뜨는 분수가 전부 실제 diff에서 나온 사실인가.
지어낸 값이 하나라도 섞이면 실패로 본다.
"""

from crbot.github.progress import ProgressReporter, Stage, WorkUnit
from crbot.review.models import build_work_units
from tests.conftest import make_file


class TestDenominatorIsFactual:
    """분모는 모델이 아니라 diff에서 나와야 한다."""

    def test_weight_is_changed_lines_not_file_count(self):
        files = [make_file("a.ts", 3), make_file("b.ts", 150, 50)]
        units = build_work_units(files)
        assert [u.weight for u in units] == [3, 200]
        # 파일 개수로 세면 3라인과 200라인이 같은 한 칸을 먹는다
        assert sum(u.weight for u in units) == 203

    def test_skipped_files_excluded_from_denominator(self):
        # lockfile 5천 라인이 분모를 지배하면 남은 분수가 의미를 잃는다
        files = [make_file("src/a.ts", 10), make_file("pnpm-lock.yaml", 5000)]
        units = build_work_units(files)
        assert units[1].skipped
        assert units[1].weight == 0
        assert sum(u.weight for u in units) == 10

    def test_binary_file_without_patch_is_skipped(self):
        units = build_work_units([make_file("logo.png", 0, patch=None)])
        assert units[0].skipped_reason == "리뷰 대상 아님"

    def test_hunk_count_comes_from_patch(self):
        patch = "@@ -1,2 +1,3 @@\n+a\n@@ -10,2 +11,3 @@\n+b"
        units = build_work_units([make_file("a.ts", 2, patch=patch)])
        assert units[0].hunks == 2


class TestRendering:
    async def test_headline_shows_both_fractions(self, reporter, gh):
        await reporter.plan([WorkUnit("a.ts", 10), WorkUnit("b.ts", 90)])
        await reporter.complete_unit("a.ts", findings=2)
        body = gh.bodies[-1]
        assert "1/2 파일" in body
        assert "10/100 라인" in body

    async def test_no_percentage_anywhere(self, reporter, gh):
        """단일 LLM 호출의 진척도는 알 수 없다. %를 쓰면 지어낸 숫자가 된다."""
        await reporter.plan([WorkUnit("a.ts", 10), WorkUnit("b.ts", 90)])
        await reporter.begin_unit("a.ts")
        assert "%" not in gh.bodies[-1]

    async def test_no_eta_shown(self, reporter, gh):
        """남은 시간은 추정할 근거가 없다. 경과 시간만 쓴다."""
        await reporter.plan([WorkUnit("a.ts", 10)])
        body = gh.bodies[-1]
        assert "경과" in body
        for forbidden in ("남은", "예상", "ETA"):
            assert forbidden not in body

    async def test_active_unit_marked_without_progress(self, reporter, gh):
        await reporter.plan([WorkUnit("a.ts", 10), WorkUnit("b.ts", 10)])
        await reporter.begin_unit("b.ts")
        body = gh.bodies[-1]
        assert "`b.ts` ← 분석 중" in body

    async def test_completed_unit_shows_finding_count(self, reporter, gh):
        await reporter.plan([WorkUnit("a.ts", 10)])
        await reporter.complete_unit("a.ts", findings=3)
        assert "- [x] `a.ts` — 3건" in gh.bodies[-1]

    async def test_clean_unit_says_so(self, reporter, gh):
        await reporter.plan([WorkUnit("a.ts", 10)])
        await reporter.complete_unit("a.ts", findings=0)
        assert "지적 없음" in gh.bodies[-1]

    async def test_large_pr_folds_the_checklist(self, reporter, gh):
        # 체크리스트를 통째로 그리면 큰 PR에서 코멘트가 화면을 덮는다
        await reporter.plan([WorkUnit(f"f{i}.ts", 10) for i in range(40)])
        body = gh.bodies[-1]
        assert "외 " in body and "개 파일 대기" in body
        assert body.count("- [ ]") <= 13

    async def test_failure_renders_reason(self, reporter, gh):
        await reporter.fail("LLM 서버에 연결할 수 없습니다")
        body = gh.bodies[-1]
        assert "실패" in body
        assert "LLM 서버에 연결할 수 없습니다" in body


class TestDebounce:
    async def test_updates_are_debounced(self, gh):
        # GitHub secondary rate limit — 단위마다 갱신하면 큰 PR에서 403으로 막힌다
        reporter = ProgressReporter(gh, "o", "r", model="m", debounce_s=60.0)
        await reporter.start(1)
        before = gh.update_count
        for i in range(10):
            await reporter.begin_unit(f"f{i}.ts")
        assert gh.update_count == before, "debounce 중에는 PATCH가 나가면 안 된다"

    async def test_flush_forces_pending_state_out(self, gh):
        reporter = ProgressReporter(gh, "o", "r", model="m", debounce_s=60.0)
        await reporter.start(1)
        await reporter.plan([WorkUnit("a.ts", 5)])
        await reporter.complete_unit("a.ts", findings=1)
        before = gh.update_count
        await reporter.flush()
        # 마지막 상태는 debounce와 무관하게 반드시 반영돼야 한다
        assert gh.update_count == before + 1
        assert "1/1 파일" in gh.bodies[-1]

    async def test_flush_is_noop_when_clean(self, gh):
        reporter = ProgressReporter(gh, "o", "r", model="m", debounce_s=0.0)
        await reporter.start(1)
        await reporter.set_stage(Stage.REVIEWING)
        before = gh.update_count
        await reporter.flush()
        assert gh.update_count == before
