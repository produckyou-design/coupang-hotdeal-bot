# 쿠팡파트너스 x 텔레그램 자동화 — SPEC (기술 명세)

> PRD(`docs/PRD.md`)의 구현 명세. 이 파일만 보고 코드를 짤 수 있어야 한다.
> 기존 파일은 수정·확장하고, 새로 만들지 말아야 할 건 만들지 않는다.

## 0. 실행 환경

- Python 3.10+ (로컬 PC 상시 실행)
- 설치: `pip install -r requirements.txt`
- 실행: `python bot.py` (엔트리포인트, 상시 실행)
- 테스트: `python test_api.py` (골드박스 호출 확인)
- `.env`는 `.env.example` 복사 후 값 채우기. `.env`는 커밋 금지(`.gitignore`에 이미 포함)

```bash
cp .env.example .env
pip install -r requirements.txt
python test_api.py
```

## 1. 파일 구조 (최종)

```
coupang_api.py      # [수정/확장] 기존 클래스 유지 + 리포트 3종 + 재시도 + 페이징
config.py           # [신규] .env 로드, 상수/키워드/스케줄/필터 설정
db.py               # [신규] sqlite 초기화 + CRUD
collector.py        # [신규] 수집 + 필터 (판정 사유까지 반환)
publisher.py        # [신규] 딥링크 변환 + 송출 포맷 + 채널 발행
reporter.py         # [신규] 일일/주간결산 + 취소감지 + 포맷
bot.py              # [신규] 명령어 핸들러 + APScheduler + 엔트리포인트
test_api.py         # [기존 유지] 필요 시 리포트 테스트 추가 가능
requirements.txt    # [기존 유지] 추가 패키지 금지 (sqlite는 표준라이브러리)
data/app.db         # [자동생성] 커밋 금지 (.gitignore에 data/ 이미 포함)
```

## 2. 쿠팡 API 명세 (기존 `coupang_api.py` 기준)

- DOMAIN: `https://api-gateway.coupang.com`
- 인증: `CEA algorithm=HmacSHA256, access-key=..., signed-date=..., signature=...`
  - `signed-date = UTC now, 포맷 "%y%m%dT%H%M%SZ"`
  - `message = signed-date + METHOD(대문자) + path + ("?"+query if query)`
  - `signature = HMAC-SHA256(secret_key, message).hexdigest()`
- 타임아웃 10초, `Content-Type: application/json;charset=UTF-8`

### 2.1 기존 엔드포인트 (그대로 사용, 시그니처 변경 금지)

| 메서드 | path | params/body |
|---|---|---|
| GET | `/v2/providers/affiliate_open_api/apis/openapi/v1/products/goldbox` | `subId?` |
| POST | `/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink` | body `{coupangUrls: string[], subId?}` (20개 이하 권장) |
| GET | `/v2/providers/affiliate_open_api/apis/openapi/products/search` | `keyword, limit, subId?` |
| GET | `/v2/providers/affiliate_open_api/apis/openapi/products/bestcategories/{category_id}` | `limit, subId?` |
| GET | `/v2/providers/affiliate_open_api/apis/openapi/v1/reports/orders` | `startDate=YYYYMMDD, endDate=YYYYMMDD, page=0` |

### 2.2 추가할 엔드포인트 (orders 패턴 준용, 구현 후 실측으로 path 확정)

`coupang_api.py`에 추가. orders와 동일한 `startDate/endDate/page` 쿼리 사용.
실측 시 rCode가 에러면 path를 의심하고, orders 호출 성공 케이스와 비교해서 수정할 것.

- `get_clicks_report(start_date, end_date, page=0)` → 일별 클릭
- `get_commission_report(start_date, end_date, page=0)` → 일별 수익/클릭/주문/취소/GMV
- `get_cancels_report(start_date, end_date, page=0)` → 취소 (주문 필드 + orderDate)
- 기존 `get_order_report`에 `subId` 옵션 추가 (자동vs수동 구분용)

### 2.3 공통 개선 (coupang_api.py에 적용)

- 429/5xx/타임아웃 시 지수 백오프 재시도 (최대 3회: 1s → 2s → 4s)
- 401은 재시도 없이 즉시 예외 (대시보드 알림용으로 구분 가능하게)
- 리포트 조회는 `page` 순회로 전량 수집 (페이지당 최대 1000개 가정, 빈 페이지에서 중단)
- 응답 `rCode != "0"`이면 예외 또는 `(False, rMessage)` 반환 — 호출부가 대시보드에 기록할 수 있게. 절대 조용히 무시 금지.

## 3. DB 명세 (`db.py`, SQLite `data/app.db`)

```sql
CREATE TABLE IF NOT EXISTS products (
  product_id   INTEGER PRIMARY KEY,
  name         TEXT NOT NULL,
  price        INTEGER NOT NULL,
  url          TEXT,
  image        TEXT,
  is_rocket    INTEGER DEFAULT 0,
  last_seen_at TEXT NOT NULL,   -- ISO8601
  min_price    INTEGER          -- 관측된 최저가
);

CREATE TABLE IF NOT EXISTS price_history (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL,
  price      INTEGER NOT NULL,
  seen_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history_pid ON price_history(product_id, seen_at);

CREATE TABLE IF NOT EXISTS posts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id   INTEGER NOT NULL,
  sub_id       TEXT NOT NULL,   -- send | manual | test
  source       TEXT NOT NULL,   -- goldbox | keyword | best | manual_url
  keyword      TEXT,
  price        INTEGER,
  shorten_url  TEXT,
  posted_at    TEXT NOT NULL,   -- ISO8601
  message_id   INTEGER,         -- 텔레그램 message_id (취소/추적용)
  status       TEXT NOT NULL    -- posted | skipped | failed (+사유는 skip_reason)
                 DEFAULT 'posted',
  skip_reason  TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_pid_date ON posts(product_id, posted_at);

CREATE TABLE IF NOT EXISTS daily_stats (
  date         TEXT PRIMARY KEY, -- YYYYMMDD
  clicks       INTEGER DEFAULT 0,
  orders       INTEGER DEFAULT 0,
  commission   INTEGER DEFAULT 0,
  cancels      INTEGER DEFAULT 0,
  gmv          INTEGER DEFAULT 0,
  updated_at   TEXT NOT NULL
);
```

- `products.last_seen_at`은 수집 때마다 갱신, `price_history`는 가격 변동 시에만 insert.
- 중복 판정: `posts`에서 `product_id`가 최근 7일 내 `status='posted'`면 스킵.

## 4. 설정 (`config.py`)

환경변수: `COUPANG_ACCESS_KEY`, `COUPANG_SECRET_KEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_PUBLIC_CHANNEL_ID`, `TELEGRAM_ADMIN_CHAT_ID` (없으면起動 시 ValueError로 즉시 종료).

```python
POST_MODE = "approval"   # "approval" | "auto". approval=승인 후 발행(기본), auto=완전자동
DAILY_LIMIT = 6
KEYWORDS = ["에어프라이어","로봇청소기","캠핑의자","기저귀","물티슈",
            "단백질쉐이크","무선청소기","전기장판","아기과자","냄비세트"]  # 로테이션용, 교체 가능
PRICE_MIN = 5000
PRICE_MAX = 300000
ROCKET_ONLY = False        # True면 로켓/무료배송만 통과 (기본 False, 운영하며 조정)
DEDUP_DAYS = 7
BLACKLIST = ["성인", "주류", "담배"]  # 상품명에 포함되면 제외
SUBID_SEND = "send"; SUBID_MANUAL = "manual"; SUBID_TEST = "test"
```

## 5. 스케줄 (APScheduler, `bot.py`, Asia/Seoul)

| 시간 | 작업 | 내용 |
|---|---|---|
| 08:00 | 아침 특가 | 골드박스 상위 2개 |
| 12:00 | 점심 키워드 | KEYWORDS 로테이션 1개 → 검색 상위 1개 |
| 18:00 | 저녁 베스트 | 카테고리베스트 2개 (카테고리ID는 config에 정의, 실측 후 확정) |
| 22:00 | 마감 | 당일 미발행 후보 중 1개 또는 골드박스 1개 |
| 16:00 | 일일결산 | 전일 리포트 → 대시보드 (15시 갱신 고려) |
| 월 09:00 | 주간결산 | 최근 7일 합산 → 대시보드 |

- 자동 수집 1회당 딥링크 변환은 최대 5개 URL로 묶어서 1회 호출.
- 검색 결과는 키워드별 1시간 캐시 (메모리 dict + 만료시각).
- POST_MODE=approval이면 자동 작업은 대시보드에 미리보기+승인버튼만 보내고 발행 대기.

## 6. 메시지 템플릿 (고정, 임의 변경 금지)

### 6.1 송출 카드 (공개 채널)

```
🔥 [태그] 상품명
💰 89,000원 (로켓배송)
👉 버튼: [쿠팡에서 보기] (url=shortenUrl)
하단 고정 문구:
이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다.
```

- 태그: `[골드박스]` / `[베스트]` / `[키워드:XXX]` / `[추천]`
- 이미지는 상품 이미지 URL로 photo 전송, 캡션에 위 텍스트. 이미지 없으면 텍스트만.
- HTML 파싱 사용, 상품명의 `<>&`는 escape.

### 6.2 대시보드 일일결산 (16:00)

```
📊 일일결산 11/03 (전일)
클릭 312 (▲12) / 주문 18 (▲3) / 취소 2
수익 24,300원 (▲3,100) / GMV 512,000원
🏆 TOP5
1. 상품명A — 5건 8,200원
...
```

### 6.3 대시보드 알림

- 송출로그: `✅ 송출 [골드박스] 상품명 89,000원 (send)` / `⏭ 스킵 상품명 — 사유: 7일내발행`
- 딥링크 실패: `⛔ 딥링크 실패 — 사유: {rMessage} / URL: {원본}` (원본 URL로 발행 금지)
- 에러: `🚨 API {401|429|...} — {함수명}: {메시지}` (401은 키 확인 문구 포함)

## 7. 봇 명령어 (`bot.py`, python-telegram-bot v21)

- 허용 사용자: `TELEGRAM_ADMIN_CHAT_ID`와 발신자가 일치할 때만 반응, 불일치 시 무시 (로그만).
- `/검색 <키워드>` → 상위 5개 인라인 버튼(번호) → 선택 시 미리보기+`[발행][취소]` → 발행 시 subId=manual.
- 쿠팡 URL 메시지 (`http`+`coupang.com` 포함) → 딥링크 변환 → 미리보기+`[발행][취소]`.
- `/상태` → 오늘 발행수/스킵수, POST_MODE, 다음 스케줄, 마지막 API 호출 상태.
- `/발행취소` → 대기 중인 승인 1건 취소 (approval 모드).
- 승인 대기 상태는 메모리 dict + `posts` status로 관리 (재시작 시 유실 허용, v1 단순화).

## 8. 수집·필터 로직 (`collector.py`)

```
collect(source) → raw 상품 리스트
  → BLACKLIST 제외
  → PRICE_MIN/MAX 범위 (상품검색 응답 productPrice 기준)
  → ROCKET_ONLY면 isRocket or isFreeShipping만
  → posts에서 DEDUP_DAYS 내 발행 이력 있으면 스킵 (skip_reason="7일내발행")
  → products/min_price와 비교: 하락or최저가면 우선순위↑ (첫 관측이면 통과 후 기록)
  → 반환: (통과 리스트, 스킵 리스트+사유)
```

- 상품검색 `limit` 최대 10, 키워드 50자 초과 시 단어 경계에서 자르기.
- 카테고리ID는 실측으로 유효한 것만 config에 등록 (무효 ID면 로그+스킵).

## 9. 에러·한도 정책

- 401 → 재시도 없음, 대시보드 `🚨 키 확인 필요` + 스케줄 작업 중단(다음 틱에 재시도).
- 429 → 해당 작업만 중단 + 백오프, 대시보드에 한도초과 기록. 검색 캐시 우선 사용.
- 네트워크/5xx → 최대 3회 재시도 후 실패 기록, 송출 스킵 (빈 카드 발행 금지).
- 딥링크 부분실패 → 성공분만 발행, 실패분은 대시보드에 기록.

## 10. 보안·정책

- `.env` 커밋 금지, Secret Key 스크린샷/채팅방 공유 금지, 노출 시 파트너스에서 재발급.
- 송출 채널 고지문구 삭제 금지. 활동 페이지(송출 채널 URL)는 파트너스 활동페이지에 등록.
- 로그에 Secret Key/토큰 전문 출력 금지 (마스킹).
