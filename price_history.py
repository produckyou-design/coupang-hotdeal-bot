"""SQLite 기반 가격 관측 이력, 단위가격 파싱, 카테고리 분류, 대기 큐 및 출처 쿨다운 관리 모듈."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

KST = timezone(timedelta(hours=9))

_IT_STRICT_KEYWORDS = (
    "그래픽카드", "노트북", "모니터", "태블릿", "아이패드", "갤럭시탭", "갤럭시북",
    "맥북", "스마트폰", "자급제", "키보드", "마우스", "공유기", "이어폰", "헤드셋",
    "헤드폰", "스피커", "무선충전", "보조배터리", "충전기", "메모리카드", "외장하드",
    "외장ssd", "마이크로sd", "메인보드", "파워서플라이", "수랭쿨러", "공랭쿨러",
    "로봇청소기", "무선청소기", "물걸레청소기", "공기청정기", "가습기", "제습기",
    "에어프라이어", "전자레인지", "커피머신", "에스프레소", "식기세척기", "음식물처리기",
    "건조기", "세탁기", "냉장고", "김치냉장고", "빔프로젝터", "웹캠", "캡처보드",
    "닌텐도", "플레이스테이션", "엑스박스", "스팀덱", "게이밍", "전자책", "리더기",
    "스마트워치", "갤럭시워치", "애플워치", "에어팟", "버즈",
)
_IT_TOKEN_RE = re.compile(
    r"(?:^|[^a-z0-9가-힣])(?:ssd|hdd|nvme|ram|cpu|gpu|nas|usb|oled|qled|uhd|fhd|qhd|4k|8k|ps5|xbox|pc|tv|144hz|165hz|240hz|360hz|mini\s*led|sdxc)(?:[^a-z0-9가-힣]|$)",
    re.IGNORECASE,
)

_LIVING_KEYWORDS = (
    "휴지", "화장지", "롤휴지", "각티슈", "미용티슈", "물티슈", "키친타월", "핸드타월",
    "냅킨", "세제", "세탁세제", "주방세제", "섬유유연제", "다우니", "퍼실", "테크",
    "피지", "액체세제", "캡슐세제", "표백제", "산소계표백제", "과탄산소다", "베이킹소다",
    "구연산", "샴푸", "린스", "트리트먼트", "바디워시", "바디로션", "비누", "핸드워시",
    "치약", "칫솔", "가글", "치실", "면도기", "면도날", "질레트", "도루코", "쉬크",
    "로션", "수분크림", "선크림", "썬크림", "클렌징", "폼클렌징", "마스크팩", "화장솜",
    "생리대", "라이너", "탐폰", "기저귀", "하기스", "팸퍼스", "페넬로페", "배변패드",
    "고양이모래", "벤토나이트", "두부모래", "탈취제", "방향제", "페브리즈", "디퓨저",
    "락스", "곰팡이제거", "배수구", "변기세정", "청소포", "밀대", "행주", "수세미",
    "지퍼백", "위생백", "랩", "호일", "종이호일", "음식물봉투", "쓰레기봉투",
    "건전지", "알카라인", "멀티탭", "전구", "수건", "타월", "양말", "속옷", "팬티",
    "kf94", "kf80", "마스크", "밴드", "습윤밴드", "워셔액", "제습제", "물먹는하마",
)

_FOOD_KEYWORDS = (
    "생수", "먹는샘물", "삼다수", "백산수", "아이시스", "스파클", "에비앙", "탄산수",
    "트레비", "씨그램", "페리에", "아메리카노", "원두", "드립백", "캡슐커피", "믹스커피",
    "라떼", "콜드브루", "두유", "우유", "멸균우유", "요거트", "요구르트", "그릭요거트",
    "주스", "오렌지주스", "사과주스", "콜라", "사이다", "펩시", "코카콜라", "웰치스",
    "환타", "게토레이", "파워에이드", "포카리", "토레타", "몬스터", "레드불", "핫식스",
    "이온음료", "제로음료", "과자", "스낵", "초콜릿", "초코바", "트윅스", "스니커즈",
    "abc초콜릿", "에이비씨", "빅파이", "꼬북칩", "오감자", "포카칩", "프링글스",
    "젤리", "하리보", "마이구미", "사탕", "캔디", "쿠키", "비스킷", "크래커", "웨하스",
    "시리얼", "그래놀라", "오트밀", "프로틴", "단백질", "쉐이크", "닭가슴살",
    "라면", "컵라면", "신라면", "진라면", "안성탕면", "너구리", "짜파게티", "불닭",
    "우동", "소바", "파스타", "스파게티", "냉면", "쫄면", "비빔면", "쌀국수",
    "즉석밥", "햇반", "오뚜기밥", "현미밥", "백미", "잡곡", "찹쌀", "귀리",
    "김치", "포기김치", "묵은지", "만두", "비비고", "교자", "치킨", "너겟", "텐더",
    "돈까스", "탕수육", "소시지", "후랑크", "햄", "스팸", "리챔", "런천미트", "베이컨",
    "삼겹살", "목살", "항정살", "앞다리", "소고기", "한우", "차돌박이", "갈비", "부채살",
    "돼지고기", "오리고기", "훈제오리", "연어", "고등어", "갈치", "새우", "오징어",
    "참치", "동원참치", "사조참치", "골뱅이", "황도", "백도", "통조림",
    "죽", "스프", "카레", "짜장", "소스", "케첩", "마요네즈", "머스타드", "간장",
    "고추장", "된장", "쌈장", "올리브유", "아보카도오일", "식용유", "카놀라유", "참기름",
    "들기름", "설탕", "알룰로스", "스테비아", "소금", "후추", "아몬드", "호두", "캐슈넛",
    "피스타치오", "견과", "아이스크림", "하겐다즈", "나뚜루", "밀키트", "도시락", "샐러드",
    "사과", "귤", "천혜향", "레드향", "오렌지", "바나나", "포도", "샤인머스캣", "토마토",
    "방울토마토", "고구마", "감자", "양파", "마늘", "도시락김", "조미김", "김자반", "재래김",
    "구운김", "미역", "다시마", "멸치", "어묵", "떡볶이", "가래떡", "떡", "빵", "식빵",
    "모닝빵", "베이글", "카스테라", "케이크", "홍삼", "정관장", "비타민", "유산균",
    "락토핏", "오메가3", "루테인", "밀크씨슬", "마그네슘", "아연", "콜라겐", "영양제",
    "사료", "고양이사료", "강아지사료", "츄르", "개껌", "반려동물간식", "국밥", "설렁탕",
    "육개장", "곰탕", "갈비탕", "부대찌개", "된장찌개", "김치찌개", "핫도그", "피자",
)


def classify_deal_category(title: str, body_text: str = "") -> str:
    """제목과 본문을 기반으로 딜 카테고리를 분류('food' | 'living' | 'it' | 'general')."""
    title_l = (title or "").lower()
    body_l = (body_text or "").lower()
    combined = f"{title_l} {body_l}".strip()
    if not combined:
        return "general"

    # IT 가전/PC 부품은 우선 판별 (단, '커피' 등이 함께 있는 커피머신도 IT/가전으로)
    for kw in _IT_STRICT_KEYWORDS:
        if kw in title_l:
            return "it"
    if _IT_TOKEN_RE.search(title_l):
        return "it"

    # 생활용품은 식품보다 먼저 검사 ('물티슈', '주방세제', '핸드워시' 등의 오분류 방지)
    for kw in _LIVING_KEYWORDS:
        if kw in title_l:
            return "living"

    for kw in _FOOD_KEYWORDS:
        if kw in title_l:
            return "food"

    # 제목으로 판별되지 않은 경우 본문 힌트 보조 확인
    for kw in _IT_STRICT_KEYWORDS:
        if kw in body_l:
            return "it"
    if _IT_TOKEN_RE.search(body_l):
        return "it"
    for kw in _LIVING_KEYWORDS:
        if kw in body_l:
            return "living"
    for kw in _FOOD_KEYWORDS:
        if kw in body_l:
            return "food"

    return "general"


_UNIT_SPEC_WEIGHT_VOL_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(kg|g|l|ml|리터|킬로)\b"
    r"(?:\s*[x×*]\s*(\d+)\s*(?:개|입|봉|팩|병|캔|페트|포|통|박스|세트)?)?",
    re.IGNORECASE,
)
_PACK_MULTI_RE = re.compile(
    r"(?:[x×*]\s*(\d+)|(\d+)\s*(?:개입|개|봉지|봉|팩|입|롤|매|정|캡슐|병|캔|페트|포|통|박스|세트))",
    re.IGNORECASE,
)
_COUNT_UNIT_RE = re.compile(
    r"(\d+)\s*(롤|매|정|캡슐|포|봉|팩|병|캔|개|입)\b",
    re.IGNORECASE,
)


def compute_effective_unit_metrics(title: str, total_price: int) -> Dict[str, Any]:
    """제목의 중량/용량/수량과 총 결제금액(배송비 포함)으로 표준 단위가격을 계산."""
    if not title or not isinstance(total_price, (int, float)) or total_price < 1000:
        return {}

    text = re.sub(r"[\[\]\(\)]", " ", title)

    # 1) 중량/용량 + 배수 탐색 (예: 500ml x 40, 500ml 40개, 72g 6개, 200g 2팩 3박스)
    best_weight_g = 0.0
    best_vol_ml = 0.0

    for m in _UNIT_SPEC_WEIGHT_VOL_RE.finditer(text):
        val = float(m.group(1))
        unit = m.group(2).lower()
        mult = int(m.group(3)) if m.group(3) else 1

        if mult == 1:
            # 바로 뒤 구간(최대 24자)에서 개/봉/팩/박스 등 수량 multiplier 탐색
            tail = text[m.end(): m.end() + 24]
            mults = []
            for pm in _PACK_MULTI_RE.finditer(tail):
                num_s = pm.group(1) or pm.group(2)
                if num_s:
                    n = int(num_s)
                    if 1 <= n <= 500:
                        mults.append(n)
            if mults:
                for n in mults:
                    mult *= n

        if unit in ("kg", "킬로"):
            total_g = val * 1000.0 * mult
            if total_g > best_weight_g:
                best_weight_g = total_g
        elif unit == "g":
            total_g = val * mult
            if total_g > best_weight_g:
                best_weight_g = total_g
        elif unit in ("l", "리터"):
            total_ml = val * 1000.0 * mult
            if total_ml > best_vol_ml:
                best_vol_ml = total_ml
        elif unit == "ml":
            total_ml = val * mult
            if total_ml > best_vol_ml:
                best_vol_ml = total_ml

    if best_vol_ml > 0:
        per_100ml = round(total_price * 100.0 / best_vol_ml, 2)
        if best_vol_ml >= 1000.0:
            per_1l = int(round(total_price * 1000.0 / best_vol_ml))
            display_unit = f"1L당 {per_1l:,}원"
        else:
            display_unit = f"100ml당 {int(round(per_100ml)):,}원"
        return {
            "metric_type": "per_100ml",
            "metric_value": per_100ml,
            "per_100g": None,
            "per_100ml": per_100ml,
            "per_unit": None,
            "unit_count": None,
            "unit_label": "ml",
            "display_unit": display_unit,
        }

    if best_weight_g > 0:
        per_100g = round(total_price * 100.0 / best_weight_g, 2)
        if best_weight_g >= 2000.0:
            per_1kg = int(round(total_price * 1000.0 / best_weight_g))
            display_unit = f"1kg당 {per_1kg:,}원"
        else:
            display_unit = f"100g당 {int(round(per_100g)):,}원"
        return {
            "metric_type": "per_100g",
            "metric_value": per_100g,
            "per_100g": per_100g,
            "per_100ml": None,
            "per_unit": None,
            "unit_count": None,
            "unit_label": "g",
            "display_unit": display_unit,
        }

    # 2) 개수 기반 단위 (예: 30롤, 100매, 60정, 24개)
    counts: List[Tuple[int, str]] = []
    for cm in _COUNT_UNIT_RE.finditer(text):
        cnt = int(cm.group(1))
        lbl = cm.group(2)
        if 2 <= cnt <= 2000:
            counts.append((cnt, lbl))
    if counts:
        total_cnt = 1
        unit_label = counts[0][1]
        for cnt, lbl in counts[:2]:
            total_cnt *= cnt
            unit_label = lbl
        per_unit = round(total_price / total_cnt, 2)
        display_unit = f"1{unit_label}당 {int(round(per_unit)):,}원"
        return {
            "metric_type": f"per_{unit_label}",
            "metric_value": per_unit,
            "per_100g": None,
            "per_100ml": None,
            "per_unit": per_unit,
            "unit_count": total_cnt,
            "unit_label": unit_label,
            "display_unit": display_unit,
        }

    return {}


def build_option_key(
    product_id: str = "",
    item_id: str = "",
    vendor_item_id: str = "",
    title: str = "",
    url: str = "",
) -> str:
    """쿠팡 상품/옵션 식별자(productId + itemId/vendorItemId) 기반 고유 키 생성."""
    pid = str(product_id or "").strip()
    iid = str(item_id or "").strip()
    vid = str(vendor_item_id or "").strip()

    if url:
        try:
            parsed = urlparse(url)
            if not pid:
                m_pid = re.search(r"/products/(\d+)", parsed.path or "")
                if m_pid:
                    pid = m_pid.group(1)
            qs = parse_qs(parsed.query or "")
            if not iid and qs.get("itemId"):
                iid = str(qs["itemId"][0]).strip()
            if not vid and qs.get("vendorItemId"):
                vid = str(qs["vendorItemId"][0]).strip()
        except Exception:
            pass

    if pid:
        if vid:
            return f"pid:{pid}:vid:{vid}"
        if iid:
            return f"pid:{pid}:iid:{iid}"
        return f"pid:{pid}"

    norm = re.sub(r"\s+", "", (title or url or "unknown").lower())
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]
    return f"title:{digest}"


def _now_kst(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(KST)
    if now.tzinfo is None:
        return now.replace(tzinfo=KST)
    return now.astimezone(KST)


class PriceHistoryDB:
    """옵션별 30일 가격 관측, 기발행 최저가, 대기열, 출처 쿨다운 SQLite 저장소."""

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS price_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    option_key TEXT NOT NULL,
                    product_id TEXT NOT NULL DEFAULT '',
                    obs_date TEXT NOT NULL,
                    shipping_type TEXT NOT NULL DEFAULT 'free',
                    total_price INTEGER NOT NULL,
                    sale_price INTEGER NOT NULL,
                    shipping_fee INTEGER NOT NULL DEFAULT 0,
                    orig_price INTEGER NOT NULL DEFAULT 0,
                    category TEXT NOT NULL DEFAULT 'general',
                    source TEXT NOT NULL DEFAULT '',
                    metric_type TEXT NOT NULL DEFAULT '',
                    metric_value REAL,
                    created_at TEXT NOT NULL,
                    UNIQUE(option_key, obs_date, shipping_type)
                );
                CREATE INDEX IF NOT EXISTS idx_obs_option_date
                    ON price_observations(option_key, obs_date DESC);

                CREATE TABLE IF NOT EXISTS published_prices (
                    option_key TEXT PRIMARY KEY,
                    product_id TEXT NOT NULL DEFAULT '',
                    total_price INTEGER NOT NULL,
                    sale_price INTEGER NOT NULL,
                    track TEXT NOT NULL DEFAULT 'A',
                    published_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_wait_deals (
                    wait_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    post_id TEXT NOT NULL,
                    option_key TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    reason TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    recheck_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS source_cooldowns (
                    source TEXT PRIMARY KEY,
                    status_code INTEGER NOT NULL,
                    cooldown_until TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                """
            )

    def record_observation(
        self,
        option_key: str,
        product_id: str,
        total_price: int,
        sale_price: int,
        shipping_fee: int = 0,
        orig_price: int = 0,
        shipping_type: str = "free",
        category: str = "general",
        source: str = "",
        metric_type: str = "",
        metric_value: Optional[float] = None,
        obs_date: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        if not option_key or total_price <= 0 or sale_price <= 0:
            return
        dt = _now_kst(now)
        day_str = obs_date or dt.strftime("%Y-%m-%d")
        stype = (shipping_type or "free").strip().lower()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO price_observations (
                    option_key, product_id, obs_date, shipping_type,
                    total_price, sale_price, shipping_fee, orig_price,
                    category, source, metric_type, metric_value, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(option_key, obs_date, shipping_type) DO UPDATE SET
                    total_price = MIN(price_observations.total_price, excluded.total_price),
                    sale_price = CASE
                        WHEN excluded.total_price < price_observations.total_price THEN excluded.sale_price
                        ELSE price_observations.sale_price
                    END,
                    shipping_fee = CASE
                        WHEN excluded.total_price < price_observations.total_price THEN excluded.shipping_fee
                        ELSE price_observations.shipping_fee
                    END,
                    orig_price = MAX(price_observations.orig_price, excluded.orig_price),
                    metric_type = CASE
                        WHEN excluded.metric_type != '' THEN excluded.metric_type
                        ELSE price_observations.metric_type
                    END,
                    metric_value = CASE
                        WHEN excluded.total_price < price_observations.total_price AND excluded.metric_value IS NOT NULL THEN excluded.metric_value
                        ELSE COALESCE(price_observations.metric_value, excluded.metric_value)
                    END
                """,
                (
                    option_key,
                    str(product_id or ""),
                    day_str,
                    stype,
                    int(total_price),
                    int(sale_price),
                    int(shipping_fee or 0),
                    int(orig_price or 0),
                    category or "general",
                    source or "",
                    metric_type or "",
                    metric_value,
                    dt.isoformat(),
                ),
            )

    def get_observed_baseline(
        self,
        option_key: str,
        shipping_type: Optional[str] = None,
        days: int = 30,
        min_days: int = 5,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        if not option_key:
            return None
        dt = _now_kst(now)
        since_date = (dt - timedelta(days=days)).strftime("%Y-%m-%d")

        query = """
            SELECT obs_date, MIN(total_price) AS day_low
            FROM price_observations
            WHERE option_key = ? AND obs_date >= ?
        """
        params: List[Any] = [option_key, since_date]
        if shipping_type:
            query += " AND shipping_type = ?"
            params.append(shipping_type.strip().lower())
        query += " GROUP BY obs_date ORDER BY obs_date DESC"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        distinct_days = len(rows)
        if distinct_days < min_days:
            return None

        prices = [int(r["day_low"]) for r in rows if int(r["day_low"]) > 0]
        if len(prices) < min_days:
            return None

        med = int(round(statistics.median(prices)))
        return {
            "median_price": med,
            "distinct_days": len(prices),
            "min_price": min(prices),
            "max_price": max(prices),
        }

    def record_published(
        self,
        option_key: str,
        product_id: str,
        total_price: int,
        sale_price: int,
        track: str = "A",
        now: Optional[datetime] = None,
    ) -> None:
        if not option_key or total_price <= 0:
            return
        dt = _now_kst(now)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO published_prices (option_key, product_id, total_price, sale_price, track, published_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(option_key) DO UPDATE SET
                    product_id = excluded.product_id,
                    total_price = excluded.total_price,
                    sale_price = excluded.sale_price,
                    track = excluded.track,
                    published_at = excluded.published_at
                """,
                (option_key, str(product_id or ""), int(total_price), int(sale_price), track, dt.isoformat()),
            )

    def get_last_published(self, option_key: str) -> Optional[Dict[str, Any]]:
        if not option_key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM published_prices WHERE option_key = ?",
                (option_key,),
            ).fetchone()
        return dict(row) if row else None

    def should_reenter_price_drop(
        self,
        option_key: str,
        current_total_price: int,
        drop_pct: float = 5.0,
        window_days: int = 3,
        now: Optional[datetime] = None,
    ) -> Tuple[bool, float]:
        if not option_key or current_total_price <= 0:
            return False, 0.0
        row = self.get_last_published(option_key)
        if not row:
            return False, 0.0
        prev_total = int(row.get("total_price", 0) or 0)
        if prev_total <= 0 or current_total_price >= prev_total:
            return False, 0.0
        try:
            pub_dt = datetime.fromisoformat(row["published_at"])
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=KST)
        except Exception:
            return False, 0.0

        dt = _now_kst(now)
        if (dt - pub_dt).total_seconds() > window_days * 86400:
            return False, 0.0

        actual_drop = (prev_total - current_total_price) * 100.0 / prev_total
        if actual_drop >= drop_pct:
            return True, round(actual_drop, 1)
        return False, round(actual_drop, 1)

    def get_pending_wait(self, wait_key: str) -> Optional[Dict[str, Any]]:
        if not wait_key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_wait_deals WHERE wait_key = ?",
                (wait_key,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_pending_wait(
        self,
        wait_key: str,
        source: str,
        post_id: str,
        option_key: str,
        url: str,
        title: str,
        category: str,
        reason: str,
        max_wait_minutes: int = 120,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        dt = _now_kst(now)
        expires_dt = dt + timedelta(minutes=max_wait_minutes)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM pending_wait_deals WHERE wait_key = ?",
                (wait_key,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE pending_wait_deals
                    SET option_key = ?, title = ?, category = ?, reason = ?,
                        last_checked_at = ?, recheck_count = recheck_count + 1
                    WHERE wait_key = ?
                    """,
                    (option_key, title, category, reason, dt.isoformat(), wait_key),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO pending_wait_deals (
                        wait_key, source, post_id, option_key, url, title,
                        category, reason, first_seen_at, last_checked_at, expires_at, recheck_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        wait_key, source, str(post_id), option_key, url, title,
                        category, reason, dt.isoformat(), dt.isoformat(), expires_dt.isoformat(),
                    ),
                )
            row = conn.execute(
                "SELECT * FROM pending_wait_deals WHERE wait_key = ?",
                (wait_key,),
            ).fetchone()
        return dict(row) if row else {}

    def should_skip_wait_recheck(
        self,
        wait_key: str,
        recheck_min_minutes: int = 8,
        now: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        row = self.get_pending_wait(wait_key)
        if not row:
            return False, ""
        dt = _now_kst(now)
        try:
            exp_dt = datetime.fromisoformat(row["expires_at"])
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=KST)
            if dt >= exp_dt:
                return True, "expired"
        except Exception:
            pass
        try:
            last_dt = datetime.fromisoformat(row["last_checked_at"])
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=KST)
            elapsed = (dt - last_dt).total_seconds()
            if elapsed < recheck_min_minutes * 60:
                return True, "cooldown"
        except Exception:
            pass
        return False, ""

    def delete_pending_wait(self, wait_key: str) -> None:
        if not wait_key:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_wait_deals WHERE wait_key = ?", (wait_key,))

    def cleanup_expired_waits(self, now: Optional[datetime] = None) -> int:
        dt = _now_kst(now)
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM pending_wait_deals WHERE expires_at <= ?",
                (dt.isoformat(),),
            )
            return int(cur.rowcount or 0)

    def set_source_cooldown(
        self,
        source: str,
        status_code: int,
        cooldown_minutes: int,
        reason: str = "",
        now: Optional[datetime] = None,
    ) -> None:
        if not source:
            return
        dt = _now_kst(now)
        until = dt + timedelta(minutes=cooldown_minutes)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO source_cooldowns (source, status_code, cooldown_until, reason, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    status_code = excluded.status_code,
                    cooldown_until = excluded.cooldown_until,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (source, int(status_code), until.isoformat(), reason or "", dt.isoformat()),
            )

    def get_source_cooldown(
        self,
        source: str,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        if not source:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM source_cooldowns WHERE source = ?",
                (source,),
            ).fetchone()
        if not row:
            return None
        dt = _now_kst(now)
        try:
            until_dt = datetime.fromisoformat(row["cooldown_until"])
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=KST)
            if dt < until_dt:
                return dict(row)
        except Exception:
            pass
        return None

    def clear_source_cooldown(self, source: str) -> None:
        if not source:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM source_cooldowns WHERE source = ?", (source,))
