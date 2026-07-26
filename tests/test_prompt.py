"""프롬프트 조립·응답 파싱 테스트 (PLAN.md L1).

로컬 모델은 형식을 가끔 어긴다. 파서는 부분 실패를 견뎌야 한다 —
블록 하나가 깨졌다고 파일 전체 리뷰를 버리면 실사용이 안 된다.
"""

from crbot.review.diff import parse_patch
from crbot.review.models import Severity
from crbot.review.prompt import build_messages, parse_findings

PATCH = """@@ -10,3 +10,5 @@ export function load() {
 const a = 1;
+const b = await fetch(url);
+const c = b.json();
 return a;"""


class TestBuildMessages:
    def test_diff_carries_real_line_numbers(self):
        # 모델이 위치를 숫자로 지목해야 코멘트를 정확한 줄에 달 수 있다
        messages = build_messages(path="src/a.ts", hunks=parse_patch(PATCH))
        user = messages[1]["content"]
        assert "   11 +const b = await fetch(url);" in user
        assert "   12 +const c = b.json();" in user

    def test_path_is_included(self):
        messages = build_messages(path="src/hooks/useAuth.ts", hunks=parse_patch(PATCH))
        assert "src/hooks/useAuth.ts" in messages[1]["content"]

    def test_system_prompt_suppresses_false_positives(self):
        messages = build_messages(path="a.ts", hunks=parse_patch(PATCH))
        system = messages[0]["content"]
        # 거짓 양성이 이 봇의 성패를 가른다
        assert "지적하지 말 것" in system
        assert "포매팅" in system
        assert "확신이 서지 않는 것" in system

    def test_injection_warning_is_present(self):
        """PR 코드는 신뢰할 수 없는 입력이다 (PLAN.md L6)."""
        messages = build_messages(path="a.ts", hunks=parse_patch(PATCH))
        user = messages[1]["content"]
        assert "리뷰 대상 데이터" in user
        assert "따르지 마세요" in user

    def test_context_block_only_when_given(self):
        without = build_messages(path="a.ts", hunks=parse_patch(PATCH))[1]["content"]
        assert "관련 코드베이스 맥락" not in without
        with_ctx = build_messages(
            path="a.ts", hunks=parse_patch(PATCH), context="export type User = {...}"
        )[1]["content"]
        assert "관련 코드베이스 맥락" in with_ctx


VALID_OUTPUT = """\
확인했습니다.

<<<FINDING
LINE: 11
SEVERITY: high
TITLE: await 누락으로 Promise가 반환됩니다
BODY: `b.json()`은 Promise를 돌려주므로 `await`가 필요합니다.
그대로 두면 이후 코드가 Promise 객체를 값으로 다루게 됩니다.
FINDING>>>

<<<FINDING
LINE: 12
SEVERITY: medium
TITLE: fetch 실패 처리 없음
BODY: 응답 상태를 확인하지 않습니다.
FINDING>>>"""


class TestParseFindings:
    def test_parses_multiple_blocks(self):
        findings = parse_findings(VALID_OUTPUT, "src/a.ts")
        assert len(findings) == 2
        assert findings[0].line == 11
        assert findings[0].severity is Severity.HIGH
        assert findings[0].title == "await 누락으로 Promise가 반환됩니다"
        assert findings[1].line == 12
        assert findings[1].severity is Severity.MEDIUM

    def test_multiline_body_is_preserved(self):
        findings = parse_findings(VALID_OUTPUT, "src/a.ts")
        assert "Promise 객체를 값으로 다루게 됩니다" in findings[0].body

    def test_prose_outside_blocks_is_ignored(self):
        findings = parse_findings(VALID_OUTPUT, "src/a.ts")
        assert all("확인했습니다" not in f.body for f in findings)

    def test_path_is_attached(self):
        findings = parse_findings(VALID_OUTPUT, "src/hooks/useAuth.ts")
        assert all(f.path == "src/hooks/useAuth.ts" for f in findings)

    def test_no_findings_is_valid(self):
        # 지적할 게 없는 것이 정상적인 결과다
        assert parse_findings("문제를 찾지 못했습니다.", "a.ts") == []

    def test_empty_output(self):
        assert parse_findings("", "a.ts") == []


class TestParserTolerance:
    def test_broken_block_does_not_kill_the_rest(self):
        text = """\
<<<FINDING
SEVERITY: high
TITLE: 줄 번호가 없어서 버려질 블록
FINDING>>>

<<<FINDING
LINE: 20
SEVERITY: low
TITLE: 살아남는 지적
BODY: 내용
FINDING>>>"""
        findings = parse_findings(text, "a.ts")
        assert len(findings) == 1
        assert findings[0].line == 20

    def test_block_without_title_is_dropped(self):
        text = "<<<FINDING\nLINE: 5\nSEVERITY: high\nBODY: 제목이 없다\nFINDING>>>"
        assert parse_findings(text, "a.ts") == []

    def test_line_with_extra_text_is_recovered(self):
        # 모델이 "LINE: 42번째 줄" 처럼 쓰는 경우
        text = "<<<FINDING\nLINE: 42번째 줄\nSEVERITY: low\nTITLE: 제목\nBODY: 본문\nFINDING>>>"
        findings = parse_findings(text, "a.ts")
        assert findings[0].line == 42

    def test_unknown_severity_falls_back_to_low(self):
        # 과하게 심각하다고 표시하면 신뢰를 잃는다
        text = "<<<FINDING\nLINE: 3\nSEVERITY: 치명적\nTITLE: 제목\nBODY: 본문\nFINDING>>>"
        assert parse_findings(text, "a.ts")[0].severity is Severity.LOW

    def test_missing_body_falls_back_to_title(self):
        text = "<<<FINDING\nLINE: 3\nSEVERITY: low\nTITLE: 제목만 있음\nFINDING>>>"
        finding = parse_findings(text, "a.ts")[0]
        assert finding.body == "제목만 있음"

    def test_unterminated_block_is_ignored(self):
        text = "<<<FINDING\nLINE: 3\nTITLE: 닫히지 않음\nBODY: 내용"
        assert parse_findings(text, "a.ts") == []

    def test_echoed_template_produces_no_findings(self):
        """작은 모델은 형식 설명을 그대로 따라 쓴다. 실제로 관찰된 실패 모드다.

        이걸 통과시키면 PR에 플레이스홀더가 그대로 코멘트로 달린다.
        """
        echoed = """<<<FINDING
LINE: (지적할 줄 번호, 반드시 diff에 표시된 번호 중 하나)
SEVERITY: high | medium | low
TITLE: (한 줄 요약)
BODY: (문제와 수정 방향. 3~4줄 이내)
FINDING>>>"""
        assert parse_findings(echoed, "a.ts") == []
