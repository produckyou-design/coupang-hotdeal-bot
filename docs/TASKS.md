# 쿠팡파트너스 x 텔레그램 자동화 — TASKS (실행 순서)

> 어떤 AI든 이 파일 순서대로 실행하면 완성된다. Phase는 건너뛰지 않는다.
> 각 Phase 끝의 "확인"을 통과 못하면 다음 Phase로 가지 않는다.
> 파일 경로는 프로젝트 루트 기준. `docs/PRD.md`, `docs/SPEC.md`를 함께 참조.

## Phase 0. 환경 확인 (코드 작성 없음)

- [ ] `pip install -r requirements.txt`
- [ ] `.env.example`을 복사해 `.env` 생성, 5개 값 입력
- [ ] `python test_api.py` 실행 → `응답 코드: 0` 또는 에러 메시지 확인

**확인**: 골드박스 호출 결과가 `rCode=0`이면 통과.
401이면 키 재확인, 429면 1시간 후 재시도, 그 외는 SPEC §2.3에 따라 기록.
승인 전 계정이면 여기서 중단하고 수동 링크 방식으로 대기 (PRD §3).

## Phase 1. 기반 (config + db + coupang_api 확장)

1. `config.py` 신규: SPEC §4 그대로 구현. env 미설정 시 ValueError로 즉시 종료.
2. `db.py` 신규: SPEC §3 스키마 그대로 생성. `init_db()`, `upsert_product()`,
   `add_price_history_if_changed()`, `was_posted_recently(product_id, days)`,
   `log_post()`, `save_daily_stats()`, `get_daily_stats()` 구현.
3. `coupang_api.py` 확장 (기존 시그니처 변경 금지):
   - `get_clicks_report()`, `get_commission_report()`, `get_cancels_report()` 추가
     (orders와 동일 쿼리 `startDate/endDate/page`, SPEC §2.2)
   - `get_order_report()`에 `subId` 옵션 추가
   - 429/5xx 지수 백오프 재시도 최대 3회, 401은 즉시 예외 (SPEC §2.3)
   - 리포트계는 `page` 순회 수집 헬퍼 추가
4. `test_api.py`에 리포트 스모크 테스트 추가 (어제~오늘 1일 조회, 건수만 출력)

**확인**:
- `python test_api.py` → 골드박스 + 주문/클릭/수익/취소 4종 모두 `rCode=0` 또는 명확한 에러 로그
- `python -c "import db; db.init_db()"` → `data/app.db` 생성, 테이블 4개 존재

## Phase 2. 수집 + 발행 (collector + publisher)

1. `collector.py` 신규: SPEC §8 그대로 구현.
   `collect_goldbox()`, `collect_keyword(keyword)`, `collect_best(category_id)`,
   `apply_filters()` (통과/스킵+사유 분리 반환), 키워드 1시간 캐시.
2. `publisher.py` 신규:
   - `build_card(product, tag)` → SPEC §6.1 포맷 (HTML escape, 고지문구 고정)
   - `publish(product, sub_id, source)` → 딥링크 변환 → 송출 채널 photo/text 발행
     → `posts`에 posted/failed 기록. 딥링크 실패 시 발행 금지 + skip_reason 기록
   - `preview(product)` → 대시보드용 미리보기 텍스트 (발행 전 검수용)
3. 수동 발행 E2E 스크립트로 검증: `python -c "from publisher import ..."` 또는 임시 스크립트로
   테스트 상품 1개를 `subId=test`로 송출 채널에 발행 후 삭제 (또는 테스트 채널 사용)

**확인**:
- 송출 채널에 카드 1개 정상 노출 (이미지/가격/버튼/고지문구)
- `posts`에 `status=posted, sub_id=test` 행 존재
- 동일 상품 재발행 시도 → `skip_reason=7일내발행`로 스킵되고 대시보드 로그용 반환값 존재

## Phase 3. 봇 명령어 (bot.py v1 — 스케줄 없이 명령어만)

1. `bot.py` 신규 (python-telegram-bot v21, long polling):
   - 발신자 = `TELEGRAM_ADMIN_CHAT_ID` 일치 시만 반응 (SPEC §7)
   - `/검색 <키워드>` → 5개 인라인 버튼 → 선택 → 미리보기 → `[발행][취소]`
   - 쿠팡 URL 메시지 → 딥링크 변환 → 미리보기 → `[발행][취소]` (subId=manual)
   - `/상태`, `/발행취소` (대기 승인 1건 취소)
2. 승인 대기 상태는 메모리 dict로 관리 (재시작 유실 허용)

**확인**:
- 대시보드 채팅에서 `/검색 물티슈` → 5개 후보 → 1개 선택 → 발행 → 송출 채널 노출
- 쿠팡 URL 붙여넣기 → 미리보기 → 발행 성공
- 타인(허용 ID 외) 명령은 무시됨
- `/상태`에 오늘 발행수/모드/다음 스케줄 표시 (스케줄 미등록이면 "미등록" 표기 허용)

## Phase 4. 스케줄 + 리포트 (완성)

1. `reporter.py` 신규:
   - `daily_report(date)` → 클릭/주문/수익/취소 API 조회 → 전일 대비 계산
     → SPEC §6.2 포맷 → 대시보드 전송 + `daily_stats` 저장
   - `weekly_report()` → 최근 7일 합산 + TOP5
   - `detect_cancels()` → 취소 발생 시 별도 경고 포맷
2. `bot.py`에 APScheduler 추가 (Asia/Seoul, SPEC §5 시간표):
   - 08:00 / 12:00 / 18:00 / 22:00 수집→필터→(approval이면 대시보드 승인요청 / auto면 즉시 발행)
   - 16:00 일일결산, 월 09:00 주간결산
   - 검색 캐시·딥링크 묶음(≤5개) 규칙 준수
3. `python bot.py` 24시간 실행, 대시보드로 송출로그/에러알림 수신 확인

**확인 (PRD §7과 동일)**:
- [ ] 스케줄 24시간 동안 중복발행 0건
- [ ] 16시 일일결산이 리포트 API 실측값과 일치 (1회 수동 대조)
- [ ] 401/429 강제 유발 테스트 또는 실제 발생 시 대시보드 알림 도착
- [ ] `python bot.py` 7일 무중단 (로그 보관)

## Phase 5. (선택, v1 이후) — 지금 하지 않음

- 승인모드 → 완전자동 전환 (`POST_MODE=auto`)
- 클라우드 이전 (24시간 PC 실행이 부담될 때)
- 가격하락 폭(%) 알림 고도화, 클릭 경유 카운트 (API 일별 한계 보완)
- 카테고리ID 실측표 확정, 키워드 성과 기반 교체

## 작업 규칙 (모든 Phase 공통)

- 기존 `coupang_api.py` 인증 로직과 함수 시그니처는 유지, 확장만 한다
- 새 패키지 추가 금지 (SPEC §0의 4개 + 표준라이브러리 sqlite만)
- Secret Key/토큰을 로그·응답·커밋에 출력 금지
- 송출 카드 고지문구 삭제 금지
- 막히면 `test_api.py` 실측 결과를 대시보드 로그 형식으로 남기고 중단 (추측으로 path 변경 금지)
