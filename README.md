# GoodNews AI 🚀
미국 주식 4,000여 종목 뉴스를 AI로 요약하는 백엔드 서비스

## 빠른 시작

### 1. 환경변수 설정
```bash
cp .env.example .env
# .env 파일에서 FMP_API_KEY, ANTHROPIC_API_KEY 입력
```

### 2. Docker로 전체 실행
```bash
docker-compose up -d
# API: http://localhost:8000
# Docs: http://localhost:8000/docs
```

### 3. 파이프라인 테스트 (Docker 없이 빠르게)
```bash
pip install -r requirements.txt
python scripts/test_pipeline.py
```

---

## 프로젝트 구조
```
goodnews/
├── app/
│   ├── api/
│   │   └── main.py          ← FastAPI 서버 + 엔드포인트
│   ├── scheduler/
│   │   ├── tasks.py         ← Celery 배치 태스크 (4가지 주기)
│   │   └── fmp_collector.py ← FMP API 뉴스 수집
│   ├── summarizer/
│   │   └── llm_summarizer.py← Claude LLM Map-Reduce 요약
│   ├── models/
│   │   └── database.py      ← SQLAlchemy 모델 + DB 연결
│   └── core/
│       └── config.py        ← 환경변수 설정 (pydantic-settings)
├── scripts/
│   ├── test_pipeline.py     ← 빠른 테스트
│   └── init_db.sql          ← DB 초기화 SQL
├── .cursor/rules            ← 커서 AI 컨텍스트
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

---

## API 엔드포인트

| Method | URL | 설명 |
|--------|-----|------|
| GET | `/health` | 서버 상태 확인 |
| GET | `/summary/{ticker}?digest_type=daily` | 단일 종목 요약 조회 |
| GET | `/summary/{ticker}/all` | 모든 주기 요약 한번에 |
| GET | `/feed?tickers=AAPL,NVDA&digest_type=daily` | 관심 종목 피드 |

### 예시 응답
```json
{
  "ticker": "AAPL",
  "digest_type": "daily",
  "summary_text": "## AAPL 일간 요약\n\n**[호재]** Q4 매출 $94.9B...",
  "sentiment": "bullish",
  "source_urls": ["https://..."],
  "updated_at": "2025-01-15T06:00:00Z"
}
```

---

## 배치 스케줄 (미국 EST 기준)

| 주기 | 실행 시각 | 대상 데이터 |
|------|-----------|------------|
| daily | 매일 6/9/12/15/18/21시 | 최근 24시간 |
| weekly | 매일 06:00 | 이번 주 월요일~현재 |
| monthly | 매주 수요일 06:00 | 이번 달 1일~현재 |
| yearly | 매달 15일 06:00 | 올해 1월 1일~현재 |

### 수동 배치 실행
```bash
# daily 즉시 실행
celery -A app.scheduler.tasks call tasks.daily_digest

# worker 시작
celery -A app.scheduler.tasks worker --loglevel=info

# beat(스케줄러) 시작
celery -A app.scheduler.tasks beat --loglevel=info
```

---

## 개발 로드맵

- [x] FMP API 뉴스 수집 (배치 병렬 호출)
- [x] Claude LLM Map-Reduce 요약
- [x] PostgreSQL Upsert
- [x] FastAPI REST API
- [x] Celery 배치 스케줄러
- [ ] 4,000 티커 전체 목록 연동 (FMP /stock/list)
- [ ] 사용자 관심 종목 등록/관리 API
- [ ] 요약 품질 A/B 테스트
- [ ] 프론트엔드 (Next.js 대시보드)
- [ ] AWS/Supabase 배포
