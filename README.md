# GoodNews AI 🚀

미국 주식 4,000여 종목 뉴스를 AI로 요약하는 백엔드 서비스

> 상세 설계/의사결정 배경은 [`PROJECT_GUIDE.md`](./PROJECT_GUIDE.md) 참고.

## 빠른 시작

### 1. 환경변수 설정
```bash
cp .env.example .env
# .env 파일에서 FMP_API_KEY, GEMINI_API_KEY, DATABASE_URL 입력
```

### 2. Docker로 전체 실행
```bash
docker-compose up -d
# API: http://localhost:8000
# Docs: http://localhost:8000/docs
```

### 3. 파이프라인 빠른 테스트 (Docker 없이)
```bash
pip install -r requirements.txt
python scripts/test_pipeline.py
```

### 4. 기간 시뮬레이션 (운영 DB 없이 전체 경로 검증)
```bash
# 운영 DB에 쓰지 않고 sim_results/dry_run_*/ 에 로컬 저장
python scripts/simulate_range.py --start 2026-01-01 --end 2026-01-31 \
    --tickers AAPL,NVDA,MSFT --dry-run
```

---

## 프로젝트 구조
```
goodnews/
├── app/
│   ├── api/main.py              FastAPI 서버 + 엔드포인트
│   ├── scheduler/
│   │   ├── tasks.py             Celery 배치 태스크 (daily/weekly/midterm)
│   │   ├── fmp_collector.py     FMP 뉴스 수집
│   │   └── price_collector.py   가격/섹터 벤치마크 수집
│   ├── summarizer/
│   │   └── llm_summarizer.py    Gemini 구조화 출력 요약
│   ├── models/database.py       SQLAlchemy 모델 + DB 연결
│   └── core/config.py           환경변수 설정
├── scripts/
│   ├── simulate_range.py        기간 시뮬레이션 (dry-run 지원)
│   ├── select_sample_100.py     샘플 100종목 선정
│   ├── test_pipeline.py         빠른 동작 테스트
│   └── init_db.sql              DB 초기화 SQL (참고용 — 최신 스키마는 PROJECT_GUIDE.md 4절)
├── tests/                       pytest 단위 테스트
└── requirements.txt
```

---

## 리포트 주기

현재 3가지 주기를 제공한다 (monthly/yearly는 제거되어 더 이상 존재하지 않음).

| 주기 | 생성 시점 | 내용 |
|------|-----------|------|
| daily | 매일 21:00 ET(closing) / 08:00 ET(premarket) | 당일 호재/악재/투자자 관점 |
| weekly | 월요일 08:00 ET(draft) → 금요일 21:00 ET(final) | 주간 흐름 + 가격 변동률 |
| midterm | 금요일 22:00 ET | 최근 최대 12주 누적 추세 + 벤치마크 대비 성과 |

리포트 본문은 LLM이 정해진 JSON 스키마의 내용 필드만 채우고, 헤더·순서·
구두점은 코드가 고정 템플릿으로 조립한다(자유 텍스트 생성 방식이
아님 — 자세한 내용은 PROJECT_GUIDE.md 8절).

---

## API 엔드포인트

| Method | URL | 설명 |
|--------|-----|------|
| GET | `/health` | 서버 상태 확인 |
| GET | `/summary/{ticker}?digest_type=daily` | 단일 종목 요약 조회 (daily/weekly/midterm) |
| GET | `/summary/{ticker}/all` | 모든 주기 요약 한번에 |
| GET | `/feed?tickers=AAPL,NVDA&digest_type=daily` | 관심 종목 피드 |
| GET | `/universe/stats` | 현재 유니버스 통계 |

> ⚠️ FastAPI 코드는 작성되어 있으나 아직 실제 기동/테스트가 되지
> 않은 상태다. 실행 전 `digest_type` validation 패턴이 옛 주기
> (`monthly`/`yearly`)를 포함하고 있는지 확인할 것.

### 예시 응답
```json
{
  "ticker": "AAPL",
  "digest_type": "daily",
  "summary_text": "[오늘의 핵심 한 줄]\nAAPL 실적 호조로 주가 상승\n\n[호재]\n- 3분기 매출 기대치 상회\n...",
  "sentiment": "bullish",
  "source_urls": ["https://..."],
  "updated_at": "2026-06-17T06:00:00Z"
}
```

---

## 배치 스케줄 (미국 ET 기준)

| 주기 | 실행 시각 | 대상 |
|------|-----------|------|
| daily-closing | 매일 21:00 | 장 마감 후 |
| daily-premarket | 매일 08:00 | 장 시작 전 |
| weekly-draft | 매주 월요일 08:00 | 이번 주 초안 |
| weekly-final | 매주 금요일 21:00 | 이번 주 최종 + 벤치마크 |
| weekly-sector-news | 매주 금요일 21:30 | 섹터별 시장 뉴스 |
| weekly-midterm | 매주 금요일 22:00 | 최근 12주 누적 리포트 |
| universe-weekly | 매주 일요일 02:00 | 유니버스 재빌드 |

### 수동 배치 실행
```bash
celery -A app.scheduler.tasks call tasks.daily_digest
celery -A app.scheduler.tasks worker --loglevel=info
celery -A app.scheduler.tasks beat --loglevel=info
```

> ⚠️ 위 Celery 명령들은 코드상으로는 준비되어 있으나, 실제 Celery+Redis
> 환경에서 장시간 가동 검증은 아직 하지 않았다. 지금까지의 파이프라인
> 검증은 `scripts/simulate_range.py`(Celery 없이 순차 실행)로 진행했다.

---

## 개발 로드맵

- [x] FMP API 뉴스/가격 수집 (백필 + 일간)
- [x] Gemini 구조화 출력 요약 (daily/weekly/midterm)
- [x] PostgreSQL Upsert (articles/news_summaries/weekly_benchmarks/sector_news_summaries)
- [x] dry-run 기간 시뮬레이션 + midterm 트리거 버그 수정
- [x] monthly/yearly → midterm(12주 집계) 구조 전환
- [ ] FastAPI 서버 실제 기동/테스트
- [ ] Celery + Redis 실가동 검증
- [ ] 4,000종목 전체 유니버스 실운영 전환
- [ ] 사용자 관심 종목 등록/관리 API
- [ ] 요약 품질 A/B 테스트
- [ ] 프론트엔드 (대시보드)
- [ ] 클라우드 배포

---

## 비용 현황 (추정)

| 항목 | 비용 |
|------|------|
| FMP API | 기존 플랜 사용 중 |
| Gemini (flash + flash-lite) | 샘플 100종목 dry-run 기준 추정 중 — 전체 유니버스 확장 시 비용은 `scripts/analyze_simulation_results.py` 결과 참고 |
| Supabase | $0 (무료 플랜) |
| 서버 | 미정 (현재 로컬 실행) |
