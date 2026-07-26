"""설정. 모든 환경 의존성은 여기로 모은다.

로컬(Ollama)과 배포(vLLM)의 차이는 LLM_BASE_URL / LLM_MODEL 두 값에만 존재해야 한다.
이 원칙이 깨지면 Phase 5 배포가 재작성 작업이 된다.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # GitHub App
    github_app_id: str = ""
    github_webhook_secret: str = ""
    github_private_key_path: Path = Path("./secrets/github-app.pem")

    # LLM (OpenAI 호환)
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "gemma4:26b"
    llm_api_key: str = "ollama"
    llm_timeout_s: float = 120.0

    llm_reasoning_effort: str = "none"
    """gemma4는 추론 모델이라 기본값으로 사고 과정에 토큰을 쓴다.

    "none"이면 사고를 끈다. 30초 예산에서는 사고가 가장 큰 단일 비용이고,
    켜두면 max_tokens를 전부 사고에 쓰고 본문을 못 쓴 채 잘리는 일이 생긴다.
    품질과의 교환비는 scripts/evaluate.py 로 측정해서 정한다.
    """

    embed_model: str = "embeddinggemma"

    # 리뷰 동작
    review_trigger: str = "/review"
    progress_debounce_s: float = 2.0

    review_max_lines: int = 100_000
    """이 라인 수를 넘으면 파일당 지적 수를 줄이는 요약 모드로 폴백한다.

    사실상 꺼둔 상태다. 지금의 폴백은 이름값을 못 한다 — 지적 개수만 줄일 뿐
    리뷰하는 파일 수를 줄이지 않아서 총 시간이 거의 안 준다. 시간을 실제로 묶으려면
    파일 수를 자르거나 여러 파일을 한 호출로 묶어야 하는데, 둘 다 아직 안 한다.
    반쯤 동작하는 장치로 리뷰 품질만 깎느니 꺼두고 제대로 만들 때 되살린다.
    """

    review_max_output_tokens: int = 800
    """파일 하나를 리뷰할 때의 출력 토큰 상한.

    실측(gemma4:26b, 28 tok/s)에서 정상 리뷰는 80~174토큰이라 여유가 크다.
    잘림을 막는 안전장치로만 남긴다.
    """

    review_max_findings_per_file: int = 5

    log_level: str = "INFO"

    @property
    def github_private_key(self) -> str:
        return self.github_private_key_path.read_text()


@lru_cache
def get_settings() -> Settings:
    return Settings()
