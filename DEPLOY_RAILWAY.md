# Railway 배포 가이드

## 1. 서비스 구성

같은 레포에서 서비스를 **3개** 별도 생성한다.  
`Dockerfile`에 `CMD`가 없으므로 각 서비스의 **Start Command** 로 역할을 구분한다.

| 서비스 이름 | Start Command |
|---|---|
| `api` | `uvicorn app.api.main:app --host 0.0.0.0 --port $PORT` |
| `worker` | `celery -A app.scheduler.tasks worker --loglevel=info` |
| `beat` | `celery -A app.scheduler.tasks beat --loglevel=info` |

> `worker` / `beat` 서비스는 공개 도메인(Generate Domain)을 붙이지 않는다.

---

## 2. 환경변수 체크리스트

Railway 대시보드 → 각 서비스 → Variables 에서 아래 키를 추가한다.  
세 서비스 모두 동일한 변수 세트를 공유한다 (Shared Variables 또는 복사).

### 필수 — 값을 직접 입력해야 하는 항목

| 키 | 설명 |
|---|---|
| `FMP_API_KEY` | Financial Modeling Prep API 키 |
| `GEMINI_API_KEY` | Google Gemini API 키 |
| `DATABASE_URL` | Supabase DB 연결 문자열 (아래 주의사항 참고) |

### 필수 — Railway 내부 참조식

| 키 | 값 | 설명 |
|---|---|---|
| `REDIS_URL` | `${{Redis.REDIS_URL}}` | Railway Redis 플러그인 자동 참조 |

### 앱 동작 설정 — 운영 환경 전용

| 키 | 권장값 | 설명 |
|---|---|---|
| `APP_ENV` | `production` | `production` 이면 `create_tables()` 를 건너뜀 (스키마는 마이그레이션으로 관리) |
| `LOG_LEVEL` | `INFO` | 로그 레벨 (`DEBUG` / `INFO` / `WARNING`) |

### 선택 — 기본값이 있으나 필요 시 오버라이드

| 키 | 기본값 | 설명 |
|---|---|---|
| `FMP_BASE_URL` | `https://financialmodelingprep.com/api/v3` | FMP API 베이스 URL |
| `GEMINI_MODEL` | `gemini-2.5-flash` | 주간·섹터·미드텀 요약에 사용하는 모델 |
| `GEMINI_MODEL_LITE` | `gemini-2.5-flash-lite` | 일간 요약에 사용하는 경량 모델 |
| `NTFY_TOPIC` | `goodnews-alerts-xy7k2` | ntfy.sh 알림 토픽 (보안상 변경 권장) |
| `TICKER_BATCH_SIZE` | `30` | FMP 뉴스 수집 배치 크기 |
| `MAX_NEWS_PER_TICKER` | `50` | 종목당 최대 뉴스 수집 건수 |
| `LLM_MAX_TOKENS_MAP` | `800` | LLM map 단계 최대 토큰 |
| `LLM_MAX_TOKENS_REDUCE` | `1500` | LLM reduce 단계 최대 토큰 |
| `API_HOST` | `0.0.0.0` | API 바인딩 호스트 (변경 불필요) |
| `API_PORT` | Railway `$PORT` 자동 처리 | 미설정 시 `PORT` 환경변수를 자동으로 읽음 |
| `UNIVERSE_DATA_DIR` | `./data/universe` | 유니버스 CSV 저장 경로 |
| `UNIVERSE_MIN_MARKET_CAP` | `100000000.0` | 유니버스 포함 최소 시가총액 (USD) |
| `UNIVERSE_EXCHANGES` | `NASDAQ,NYSE,AMEX` | 수집 대상 거래소 |

> **하위 호환 전용 (설정 불필요)**  
> `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` — 코드에 선언되어 있으나 미사용. 설정하지 않으면 내부 dummy 값이 사용된다.

---

## 3. 주의사항

### DATABASE_URL — Supabase 트랜잭션 풀러 사용
Supabase 대시보드 → **Settings → Database → Connection string → Transaction pooler** 에서 복사한다.  
포트는 반드시 **6543** (트랜잭션 풀러)을 사용한다. 직접 연결(5432)은 Railway의 단명 컨테이너 환경에서 커넥션 고갈을 유발한다.

```
postgresql+asyncpg://postgres.[project-ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres
```

### APP_ENV=production 필수
이 값이 `production` 이 아니면 서버 기동 시 `create_tables()` 가 실행되어 운영 DB 스키마를 덮어쓸 위험이 있다.  
스키마 변경은 반드시 마이그레이션(Alembic 등)으로 관리한다.

### worker / beat 에 공개 도메인 금지
`worker` 와 `beat` 서비스는 HTTP 포트를 열지 않으므로 Railway에서 **Generate Domain** 을 클릭하지 않는다.  
도메인을 붙이면 Railway가 헬스체크 실패로 서비스를 재시작 루프에 빠뜨릴 수 있다.

### REDIS_URL 참조식
Railway Redis 플러그인을 같은 프로젝트에 추가한 뒤, 각 서비스의 Variables 에서 아래와 같이 입력한다.

```
REDIS_URL = ${{Redis.REDIS_URL}}
```

Railway가 런타임에 실제 URL로 치환해 준다.
