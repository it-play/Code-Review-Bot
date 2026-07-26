"""코멘트 렌더링 테스트 (PLAN.md L1).

GitHub alert 블록은 문법이 까다롭다. 안의 모든 줄이 `>` 로 시작해야 하고,
한 줄이라도 빠지면 블록이 거기서 끊기고 나머지가 평문으로 새어 나온다.
"""

import pytest

from crbot.review.models import Finding, Severity


def make_finding(severity=Severity.HIGH, body="본문") -> Finding:
    return Finding(path="src/a.ts", line=10, severity=severity, title="제목", body=body)


class TestSeverityMapping:
    @pytest.mark.parametrize(
        ("severity", "alert", "label"),
        [
            (Severity.HIGH, "CAUTION", "심각"),
            (Severity.MEDIUM, "WARNING", "보통"),
            (Severity.LOW, "NOTE", "사소"),
        ],
    )
    def test_three_levels_map_to_distinct_alerts(self, severity, alert, label):
        assert severity.alert == alert
        assert severity.label == label

    def test_alerts_are_distinct(self):
        alerts = {s.alert for s in Severity}
        assert len(alerts) == 3, "심각도가 같은 색으로 렌더링되면 구분이 안 된다"

    def test_only_valid_github_alert_types(self):
        valid = {"NOTE", "TIP", "IMPORTANT", "WARNING", "CAUTION"}
        for severity in Severity:
            assert severity.alert in valid


class TestFindingRender:
    def test_starts_with_alert_marker(self):
        assert make_finding().render().startswith("> [!CAUTION]\n")

    def test_every_line_is_quoted(self):
        rendered = make_finding(body="첫 줄\n둘째 줄\n\n넷째 줄").render()
        for line in rendered.splitlines():
            assert line.startswith(">"), f"인용되지 않은 줄이 있다: {line!r}"

    def test_blank_body_lines_stay_inside_the_block(self):
        rendered = make_finding(body="첫 줄\n\n셋째 줄").render()
        # 빈 줄이 그냥 "" 이면 블록이 끊긴다. ">" 만 있는 줄이어야 한다.
        assert "\n>\n" in rendered
        assert "\n\n" not in rendered

    def test_title_is_bold(self):
        assert "> **제목**" in make_finding().render()

    def test_body_follows_after_separator(self):
        rendered = make_finding(body="설명").render()
        assert rendered == "> [!CAUTION]\n> **제목**\n>\n> 설명"

    def test_empty_body_renders_title_only(self):
        rendered = make_finding(body="").render()
        assert rendered == "> [!CAUTION]\n> **제목**"

    def test_code_block_inside_body_stays_quoted(self):
        body = "이렇게 고치세요:\n```ts\nconst a = await b();\n```"
        rendered = make_finding(body=body).render()
        assert "> ```ts" in rendered
        assert "> const a = await b();" in rendered
        for line in rendered.splitlines():
            assert line.startswith(">")

    @pytest.mark.parametrize("severity", list(Severity))
    def test_all_severities_render(self, severity):
        rendered = make_finding(severity=severity).render()
        assert rendered.startswith(f"> [!{severity.alert}]")
