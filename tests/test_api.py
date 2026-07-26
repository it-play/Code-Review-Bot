"""웹훅 HTTP 계층 테스트 (PLAN.md L2).

서명 검증 거절, 트리거 판정, 즉시 응답(202)을 실제 ASGI 앱을 통해 확인한다.
GitHub은 웹훅 응답을 10초 안에 요구하므로 '즉시 반환'이 계약이다.
"""

import json

import pytest
from fastapi.testclient import TestClient

from crbot.config import get_settings
from crbot.main import app
from tests.test_webhook import SECRET, make_payload, sign


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("GITHUB_APP_ID", "")  # 자격 증명 없이도 앱이 떠야 한다
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


def post(client, payload: dict, *, event: str = "issue_comment", secret: str = SECRET):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": sign(body, secret),
            "X-GitHub-Event": event,
            "Content-Type": "application/json",
        },
    )


class TestSignatureGate:
    def test_wrong_secret_is_rejected(self, client):
        response = post(client, make_payload(), secret="wrong")
        assert response.status_code == 401
        assert response.json()["status"] == "invalid signature"

    def test_missing_signature_header_is_rejected(self, client):
        response = client.post("/webhook", json=make_payload())
        assert response.status_code == 401

    def test_body_tampering_is_rejected(self, client):
        body = json.dumps(make_payload()).encode()
        response = client.post(
            "/webhook",
            content=json.dumps(make_payload(body="/review --evil")).encode(),
            headers={"X-Hub-Signature-256": sign(body), "X-GitHub-Event": "issue_comment"},
        )
        assert response.status_code == 401


class TestTriggerRouting:
    def test_non_pr_comment_is_ignored(self, client):
        response = post(client, make_payload(is_pr=False))
        assert response.status_code == 200
        assert response.json()["status"] == "not_a_pr"

    def test_comment_without_trigger_is_ignored(self, client):
        response = post(client, make_payload(body="LGTM"))
        assert response.json()["status"] == "no_trigger"

    def test_outsider_is_rejected(self, client):
        response = post(client, make_payload(association="NONE"))
        assert response.json()["status"] == "forbidden"

    def test_unrelated_event_is_ignored(self, client):
        response = post(client, make_payload(), event="push")
        assert response.json()["status"] == "wrong_action"

    def test_valid_trigger_without_credentials_reports_503(self, client):
        # 자격 증명이 없으면 조용히 삼키지 말고 명확히 알린다
        response = post(client, make_payload())
        assert response.status_code == 503
        assert response.json()["status"] == "github not configured"


class TestHealth:
    def test_health_reports_configuration(self, client):
        body = client.get("/health").json()
        assert body["ok"] is True
        assert body["github_configured"] is False
        assert body["active_jobs"] == 0
        # LLM 도달 여부는 환경에 따라 다르므로 값이 있다는 것만 확인한다
        assert "llm_reachable" in body
        assert "model" in body
