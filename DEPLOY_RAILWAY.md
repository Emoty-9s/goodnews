# Railway 배포 가이드

> 최종 갱신: 2026-06-23
> 상태: **배포 완료** — `/health` 응답 확인됨

---

## 현재 배포 상태

| 항목 | 값 |
|---|---|
| 프로젝트명 | Good News |
| 환경 | production |
| 공개 도메인 | `https://apimain-production-daaf.up.railway.app` |
| /health 확인 | `{"status":"ok","version":"0.1.0"}` ✅ |
| GitHub 레포 | `Emoty-9s/goodnews` (main 브랜치 자동 배포) |

---

## 1. 서비스 구성

같은 레포에서 서비스를 **3개** 별도 생성한다.
`Dockerfile`에 `CMD`가 없으므로 각 서비스의 **Start Command** 로 역할을 구분한다.

| 서비스 이름 | Start Command | 도메인 | 상태 |
|---|---|---|---|
| `API_MAIN` | `sh -c "uvicorn app.api.main:app --host 0.0.0.0 --port $PORT"` | 있음 | Online ✅ |
| `worker` | `celery -A app.scheduler.tasks worker --loglevel=info` | 없음 | Online |
| `beat` | `celery -A app.scheduler.tasks beat --loglevel=info` | 없음 | Online |
| `Redis` | (Railway 관리형) | 없음 | Online ✅ |

> **중요**: `worker` / `beat` 서비스는 공개 도메인(Generate Domain)을 붙이지 않는다.
> 도메인을 붙이면 Railway가 헬스체크 실패로 서비스를 재시작 루프에 빠뜨릴 수 있다.

---

## 2. 환경변수 체크리스트

세 서비스(API_MAIN, worker, beat) 모두 동일한 변수 세트를 공유한다.

### 필수 — 값을 직접 입력

| 키 | 설명 |
|---|---|
| `FMP_API_KEY` | Financial Modeling Prep API 키 |
| `GEMINI_API_KEY` | Google Gemini API 키 |
| `DATABASE_URL` | Supabase DB 연결 문자열 (아래 주의사항 참고) |
| `APP_ENV` | `production` 고정 |
| `PORT` | `8000` 고정 (API_MAIN 전용 — 아래 포트 주의사항 참고) |

### 필수 — Railway 내부 참조식

| 키 | 값 | 설명 |
|---|---|---|
| `REDIS_URL` | `${{Redis.REDIS_URL}}` | Railway Redis 플러그인 자동 참조 |

### 선택 — 기본값이 있으나 필요 시 오버라이드

| 키 | 기본값 | 설명 |
|---|---|---|
| `GEMINI_MODEL` | `gemini-2.5-flash` | 주간·섹터·미드텀 요약 모델 |
| `GEMINI_MODEL_LITE` | `gemini-2.5-flash-lite` | 일간 요약 경량 모델 |
| `LOG_LEVEL` | `INFO` | 로그 레벨 |
| `NTFY_TOPIC` | `goodnews-alerts-xy7k2` | ntfy.sh 알림 토픽 |
| `TICKER_BATCH_SIZE` | `30` | FMP 뉴스 수집 배치 크기 |
| `MAX_NEWS_PER_TICKER` | `50` | 종목당 최대 뉴스 수집 건수 |
| `LLM_MAX_TOKENS_MAP` | `800` | LLM map 단계 최대 토큰 |
| `LLM_MAX_TOKENS_REDUCE` | `1500` | LLM reduce 단계 최대 토큰 |
| `UNIVERSE_MIN_MARKET_CAP` | `100000000.0` | 유니버스 최소 시가총액 (USD) |
| `UNIVERSE_EXCHANGES` | `NASDAQ,NYSE,AMEX` | 수집 대상 거래소 |

---

## 3. 주의사항

### DATABASE_URL — Supabase 트랜잭션 풀러 사용
Supabase 대시보드 → **Settings → Database → Connection string → Transaction pooler** 에서 복사.
포트는 반드시 **6543** (트랜잭션 풀러)을 사용한다.

```
postgresql+asyncpg://postgres.[project-ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres
```

### PORT 환경변수 — 고정값 사용
Railway는 `$PORT` 환경변수로 포트를 주입하지만, Docker 환경에서 셸 변수 치환이
동작하지 않는 문제가 있다. 해결 방법:

1. Variables에 `PORT = 8000` 으로 **고정값을 직접 입력**한다.
2. Start Command를 `sh -c "uvicorn ... --port $PORT"` 형태로 감싼다.
3. Generate Domain 시 포트 입력칸에 `8000`을 입력한다.

> `$PORT`를 그대로 Start Command에 쓰면
> `Error: Invalid value for '--port': '$PORT' is not a valid integer` 에러가 발생한다.

### APP_ENV=production 필수
`production`이 아니면 서버 기동 시 `create_tables()`가 실행되어
운영 DB 스키마를 덮어쓸 위험이 있다.

### REDIS_URL 참조식
Railway Redis 플러그인을 같은 프로젝트에 추가한 뒤, 각 서비스 Variables에서:
```
REDIS_URL = ${{Redis.REDIS_URL}}
```
Railway가 런타임에 실제 URL로 치환해 준다.

---

## 4. 신규 배포 절차 (재배포 또는 새 환경 구성 시)

1. **Railway 가입** → GitHub 계정 연동 (OAuth)
2. **GitHub App 설치** → `Emoty-9s/goodnews` 레포 접근 권한 부여
3. **Hobby 플랜 결제** (월 $5, 카드 등록 필요)
4. **New Project** → 빈 프로젝트 생성
5. **`+ Add` → Database → Redis** 추가
6. **`+ Add` → GitHub Repo → goodnews** 로 서비스 3개 생성
   - 각각 Settings → Deploy → Start Command 입력 (위 표 참고)
   - API_MAIN에만 Networking → Generate Domain (포트 `8000` 입력)
7. **각 서비스 Variables**에 환경변수 입력 (위 체크리스트 참고)
8. 재배포 후 `https://<domain>/health` 접속 → `{"status":"ok"}` 확인

---

## 5. 트러블슈팅 기록

| 증상 | 원인 | 해결 |
|---|---|---|
| `Error: '$PORT' is not a valid integer` | Docker 환경에서 $PORT 셸 변수 미치환 | Variables에 `PORT=8000` 고정 + `sh -c` 래핑 |
| 빌드는 성공하나 `Completed`로 끝남 | Start Command 미입력 → 프로세스가 즉시 종료 | Settings → Deploy → Start Command 입력 |
| `Application failed to respond` (502) | uvicorn 포트와 Railway 라우팅 포트 불일치 | PORT 고정값 통일 |
| Generate Domain 버튼 비활성화 | 포트 입력칸이 비어있으면 비활성 | `8000` 입력 후 활성화 |
| `/health` → `{"detail":"Not Found"}` | 루트(`/`) 접속 — 해당 경로 없음 | URL 끝에 `/health` 명시 |

---

## 6. 다음 할 일

- [ ] Supabase DB 스키마 최신화 (init_db.sql 재작성 후 SQL Editor 실행)
- [ ] 백필 데이터 Supabase 업로드 (`upload_backfill.py`)
- [ ] `/summary/AAPL?digest_type=daily` 실제 데이터 응답 확인
- [ ] Celery worker/beat ET 시각 트리거 및 장시간 안정성 검증
- [ ] articles / market_news_articles 90일 자동 삭제 로직 추가
- [ ] midterm 1년 자동 삭제 로직 추가
