"""웹훅 서명 검증과 트리거 판정 테스트 (PLAN.md L2).

여기는 신뢰할 수 없는 입력을 처음 만나는 지점이라 거절 경로를 전부 덮는다.
"""

import hmac
from hashlib import sha256

import pytest

from crbot.github.webhook import (
    TriggerDecision,
    parse_trigger,
    verify_signature,
)

SECRET = "s3cr3t"


def sign(payload: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, sha256).hexdigest()


class TestVerifySignature:
    payload = b'{"action":"created"}'

    def test_valid_signature_passes(self):
        assert verify_signature(self.payload, sign(self.payload), SECRET)

    def test_wrong_secret_fails(self):
        assert not verify_signature(self.payload, sign(self.payload, "other"), SECRET)

    def test_tampered_payload_fails(self):
        header = sign(self.payload)
        assert not verify_signature(b'{"action":"deleted"}', header, SECRET)

    def test_missing_header_fails(self):
        assert not verify_signature(self.payload, None, SECRET)

    def test_unsupported_algorithm_fails(self):
        # sha1 서명은 받지 않는다
        digest = hmac.new(SECRET.encode(), self.payload, "sha1").hexdigest()
        assert not verify_signature(self.payload, f"sha1={digest}", SECRET)

    def test_missing_secret_fails_closed(self):
        # 설정 누락이 '검증 통과'가 되면 누구나 페이로드를 밀어넣을 수 있다
        assert not verify_signature(self.payload, sign(self.payload, ""), "")

    def test_malformed_header_fails(self):
        assert not verify_signature(self.payload, "garbage", SECRET)
        assert not verify_signature(self.payload, "sha256=", SECRET)


def make_payload(
    *,
    body: str = "/review",
    association: str = "MEMBER",
    is_pr: bool = True,
    author: str = "dino",
    action: str = "created",
) -> dict:
    issue: dict = {"number": 42}
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/..."}
    return {
        "action": action,
        "issue": issue,
        "comment": {
            "id": 999,
            "body": body,
            "user": {"login": author},
            "author_association": association,
        },
        "repository": {"name": "frontend", "owner": {"login": "GSMSV"}},
        "installation": {"id": 12345},
    }


class TestParseTrigger:
    def test_accepts_review_comment_on_pr(self):
        result = parse_trigger("issue_comment", make_payload())
        assert result.accepted
        assert result.request is not None
        assert (result.request.owner, result.request.repo) == ("GSMSV", "frontend")
        assert result.request.pr_number == 42
        assert result.request.installation_id == 12345

    def test_rejects_plain_issue(self):
        result = parse_trigger("issue_comment", make_payload(is_pr=False))
        assert result.decision is TriggerDecision.NOT_A_PR

    def test_rejects_other_events(self):
        assert parse_trigger("push", make_payload()).decision is TriggerDecision.WRONG_ACTION

    def test_rejects_deleted_action(self):
        result = parse_trigger("issue_comment", make_payload(action="deleted"))
        assert result.decision is TriggerDecision.WRONG_ACTION

    @pytest.mark.parametrize("association", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"])
    def test_rejects_outsiders(self, association):
        # 아무나 트리거하면 남의 PR에서 우리 GPU를 태울 수 있다
        result = parse_trigger("issue_comment", make_payload(association=association))
        assert result.decision is TriggerDecision.FORBIDDEN

    @pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
    def test_accepts_insiders(self, association):
        assert parse_trigger("issue_comment", make_payload(association=association)).accepted

    def test_ignores_own_comments(self):
        # 봇이 자기 코멘트에 반응하면 무한 루프가 된다
        result = parse_trigger(
            "issue_comment", make_payload(author="crbot[bot]"), bot_login="crbot[bot]"
        )
        assert result.decision is TriggerDecision.SELF_AUTHORED


class TestTriggerMatching:
    def test_bare_trigger(self):
        assert parse_trigger("issue_comment", make_payload(body="/review")).accepted

    def test_trigger_with_arguments(self):
        assert parse_trigger("issue_comment", make_payload(body="/review --full")).accepted

    def test_trigger_on_its_own_line(self):
        body = "이 PR 좀 봐주세요\n/review\n감사합니다"
        assert parse_trigger("issue_comment", make_payload(body=body)).accepted

    def test_mid_sentence_mention_does_not_trigger(self):
        # "저는 /review 안 돌렸는데요" 같은 코멘트에 반응하면 안 된다
        result = parse_trigger("issue_comment", make_payload(body="저는 /review 안 돌렸는데요"))
        assert result.decision is TriggerDecision.NO_TRIGGER

    def test_quoted_trigger_does_not_fire(self):
        # 이전 코멘트를 인용하면 인용문에도 트리거가 들어 있다
        result = parse_trigger("issue_comment", make_payload(body="> /review\n네 확인했습니다"))
        assert result.decision is TriggerDecision.NO_TRIGGER

    def test_unrelated_comment(self):
        result = parse_trigger("issue_comment", make_payload(body="LGTM 👍"))
        assert result.decision is TriggerDecision.NO_TRIGGER

    def test_custom_trigger_word(self):
        payload = make_payload(body="/리뷰")
        assert parse_trigger("issue_comment", payload, trigger="/리뷰").accepted
