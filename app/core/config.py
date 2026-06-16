from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # FMP API
    fmp_api_key: str
    fmp_base_url: str = "https://financialmodelingprep.com/api/v3"

    # Gemini
    gemini_api_key: str
    gemini_model: str = "gemini-2.5-flash"          # 월간/연간/섹터뉴스
    gemini_model_lite: str = "gemini-2.5-flash-lite" # 일간/주간
    anthropic_api_key: str = "dummy"  # 하위 호환용, 미사용
    claude_model: str = "dummy"  # .env 하위 호환용

    # Database
    database_url: str

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # App
    app_env: str = "development"
    log_level: str = "INFO"

    # 알림 (ntfy.sh) — 토픽은 .env에서 오버라이드 권장
    ntfy_topic: str = "goodnews-alerts-xy7k2"

    # 수집 설정
    ticker_batch_size: int = 30
    max_news_per_ticker: int = 50
    llm_max_tokens_map: int = 800
    llm_max_tokens_reduce: int = 1500

    # API 서버
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # 유니버스 빌드
    universe_data_dir: str = "./data/universe"
    universe_min_market_cap: float = 100_000_000.0  # USD 1억
    universe_exchanges: str = "NASDAQ,NYSE,AMEX"

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()
