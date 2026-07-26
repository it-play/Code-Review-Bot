"""웹훅 서명 검증과 트리거 판정.

여기는 신뢰할 수 없는 입력을 처음 만나는 지점이다. 순수 함수로 유지해서
네트워크 없이 전부 유닛 테스트할 수 있게 한다 (PLAN.md L2).
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any

# 리뷰를 요청할 수 있는 관계. 아무나 트리거하면 남의 PR에서 우리 GPU를 태울 수 있다.
ALLOWED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def verify_signature(payload: bytes, header: str | None, secret: str) -> bool:
    """`X-Hub-Signature-256` 헤더를 검증한다.

    secret이 비어 있으면 항상 실패시킨다. 설정 누락이 '검증 통과'로 이어지면
    누구나 임의의 페이로드를 밀어넣을 수 있다.
    """
    if not secret or not header:
        return False
    algo, _, sent = header.partition("=")
    if algo != "sha256" or not sent:
        return False
    expected = hmac.new(secret.encode(), payload, sha256).hexdigest()
    return hmac.compare_digest(expected, sent)


class TriggerDecision(StrEnum):
    ACCEPT = "accept"
    NOT_A_PR = "not_a_pr"
    NO_TRIGGER = "no_trigger"
    FORBIDDEN = "forbidden"
    SELF_AUTHORED = "self_authored"
    WRONG_ACTION = "wrong_action"


@dataclass(frozen=True)
class ReviewRequest:
    owner: str
    repo: str
    pr_number: int
    comment_id: int
    requester: str
    installation_id: int


@dataclass(frozen=True)
class TriggerResult:
    decision: TriggerDecision
    request: ReviewRequest | None = None

    @property
    def accepted(self) -> bool:
        return self.decision is TriggerDecision.ACCEPT


def parse_trigger(
    event: str,
    payload: dict[str, Any],
    *,
    trigger: str = "/review",
    bot_login: str | None = None,
) -> TriggerResult:
    """`issue_comment` 이벤트가 리뷰 요청인지 판정한다.

    거절 사유를 열거형으로 돌려주는 이유: 조용히 무시하면 "왜 안 도나"를 디버깅할 수 없다.
    """
    if event != "issue_comment":
        return TriggerResult(TriggerDecision.WRONG_ACTION)
    if payload.get("action") not in {"created", "edited"}:
        return TriggerResult(TriggerDecision.WRONG_ACTION)

    issue = payload.get("issue") or {}
    # issue_comment는 일반 이슈에서도 발생한다. PR인지는 이 키로만 구분된다.
    if not issue.get("pull_request"):
        return TriggerResult(TriggerDecision.NOT_A_PR)

    comment = payload.get("comment") or {}
    author = (comment.get("user") or {}).get("login") or ""

    # 봇 자신의 코멘트에 반응하면 무한 루프가 된다.
    if bot_login and author.lower() == bot_login.lower():
        return TriggerResult(TriggerDecision.SELF_AUTHORED)

    if not _has_trigger(comment.get("body") or "", trigger):
        return TriggerResult(TriggerDecision.NO_TRIGGER)

    if (comment.get("author_association") or "").upper() not in ALLOWED_ASSOCIATIONS:
        return TriggerResult(TriggerDecision.FORBIDDEN)

    repository = payload.get("repository") or {}
    owner = ((repository.get("owner") or {}).get("login")) or ""
    name = repository.get("name") or ""
    installation_id = int((payload.get("installation") or {}).get("id") or 0)

    if not owner or not name or not issue.get("number"):
        return TriggerResult(TriggerDecision.WRONG_ACTION)

    return TriggerResult(
        TriggerDecision.ACCEPT,
        ReviewRequest(
            owner=owner,
            repo=name,
            pr_number=int(issue["number"]),
            comment_id=int(comment.get("id") or 0),
            requester=author,
            installation_id=installation_id,
        ),
    )


def _has_trigger(body: str, trigger: str) -> bool:
    """트리거는 어느 줄이든 그 줄 맨 앞에 단독으로 와야 한다.

    부분 문자열 검색을 쓰면 "저는 /review 안 돌렸는데요" 같은 코멘트나
    인용된 이전 코멘트에도 반응한다.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):  # 인용문 무시
            continue
        if stripped == trigger or stripped.startswith(f"{trigger} "):
            return True
    return False
