# Code Review Bot

로컬 LLM(Gemma 4)으로 GitHub PR을 리뷰하는 봇. 코드가 외부 API로 나가지 않는다.

- 트리거: PR에 `/review` 코멘트
- 모델: `gemma4:26b` (MoE, 활성 3.8B) — 로컬 Ollama / 배포 vLLM
- 계획과 검증 전략은 [PLAN.md](./PLAN.md), 요구사항은 [spec.md](./spec.md)

## 개발 환경

```bash
uv sync                       # 의존성
ollama serve                  # 로컬 LLM 서버
ollama pull gemma4:26b        # 모델 (17GB)
ollama pull embeddinggemma    # RAG 임베딩 (Phase 3)
cp .env.example .env          # 설정 후 값 채우기
```

GitHub App 개인키는 `secrets/github-app.pem`에 둔다. `secrets/`와 `.env`는 커밋되지 않는다.

## 실행

```bash
./scripts/dev.sh              # smee 터널 + 웹훅 서버
```

또는 서버만:

```bash
uv run uvicorn crbot.main:app --reload --port 8000
curl localhost:8000/health
```

## 측정

```bash
uv run python -m scripts.bench                     # 속도 (Phase 0)
uv run python -m scripts.bench --sustain 300       # 팬리스 스로틀링 확인
uv run python -m scripts.evaluate --runs 3         # 리뷰 품질 (L3)
uv run python -m scripts.evaluate --reasoning low  # 사고 on/off 비교
```

## 테스트

```bash
uv run pytest
uv run ruff check src tests scripts
```

## 구조

```
src/crbot/
  config.py       설정. 로컬<->배포 차이는 LLM_BASE_URL/LLM_MODEL 두 값에만 존재
  llm/client.py   OpenAI 호환 클라이언트. 사고(reasoning)와 본문을 분리 계측
  github/
    auth.py       App JWT -> 설치 토큰
    webhook.py    HMAC 검증 + 트리거 판정 (순수 함수)
    client.py     PR 파일 / 코멘트 / 리뷰 API
    progress.py   진행 상태 코멘트
  review/
    diff.py       unified diff 파서 (줄 번호 매핑)
    models.py     작업 단위 산출 — 진행 상태의 분모
    prompt.py     프롬프트 + 관대한 응답 파서
    engine.py     오케스트레이션
  jobs.py         백그라운드 리뷰 잡
scripts/
  bench.py        속도 측정
  evaluate.py     검출률 / 거짓 양성률 측정
  fixtures.py     정답 라벨이 붙은 TS diff 픽스처
```

## 알아둘 것

**gemma4는 추론 모델이다.** 기본값으로 두면 `max_tokens`를 전부 사고에 쓰고 리뷰 본문을 못 쓴 채 잘린다. `LLM_REASONING_EFFORT=none`이 기본값인 이유다 (실측: 파일당 28초 → 3.3초).

**진행 표시는 "진행률"이 아니다.** LLM 호출 하나의 내부 진척도는 원리상 알 수 없다. 셀 수 있는 분모는 diff에 있고(파일·hunk·변경 라인), 그것만 분수로 보여준다. 남은 시간은 표시하지 않는다.

**GSM SV 배포 시 Docker MTU를 1400으로 맞춰야 한다.** 안 그러면 컨테이너의 외부 HTTP 요청이 무응답으로 실패한다. [PLAN.md 1절](./PLAN.md) 참조.
