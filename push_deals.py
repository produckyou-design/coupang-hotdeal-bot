import hashlib
import html
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# 콘솔 인코딩 대응 (pythonw 실행 시 sys.stdout/stderr가 None일 수 있음)
if sys.stdout is not None and hasattr(sys.stdout, "detach"):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr is not None and hasattr(sys.stderr, "detach"):
    try:
        sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding="utf-8", errors="replace")
    except Exception:
        pass

load_dotenv()

from urllib.parse import parse_qs, unquote, urlparse

from click_tracker import (
    fetch_click_stats,
    format_click_stats_telegram,
    list_keyword_alerts,
    match_alerts_for_deal,
    match_alerts_for_title,
    record_alert_hits,
    register_tracked_link,
    remove_keyword_alert,
)
from coupang_api import (
    CoupangPartnersAPI,
    classify_sub_id,
    format_channel_split_block,
    format_order_source_badge,
    summarize_partner_stats,
)
from flyer_renderer import (
    build_card_chip,
    format_flyer_caption_and_buttons,
    is_half_off_mega,
    is_high_ticket_mega,
    render_hotdeal_flyer,
    shorten_card_title,
)
from price_history import (
    PriceHistoryDB,
    build_option_key,
    classify_deal_category,
    compute_effective_unit_metrics,
    has_ambiguous_option_or_price,
    is_comparable_option_key,
    normalize_observed_shipping_type,
    strip_purchase_limit_phrases,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 파일 로깅 및 콘솔 로깅 동시 지원 (배경 실행 시에도 deal_pusher.log에 실시간 기록)
log_file = os.getenv("DEAL_PUSHER_LOG_FILE", os.path.join(BASE_DIR, "deal_pusher.log"))
handlers: List[logging.Handler] = []
if log_file:
    handlers.append(logging.FileHandler(log_file, encoding="utf-8", delay=True))
if sys.stderr is not None:
    handlers.append(logging.StreamHandler(sys.stderr))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=handlers,
)
logger = logging.getLogger(__name__)

# 설정값
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_PUBLIC_CHANNEL_ID", "@HOTpang911")
ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "7525598874")
REPORT_CHANNEL_ID = os.getenv("TELEGRAM_REPORT_CHANNEL_ID", "-1003981511629")

coupang_api = CoupangPartnersAPI()
SEEN_FILE = os.path.join(BASE_DIR, "seen_deals.json")
TRACKER_FILE = os.path.join(BASE_DIR, "sales_tracker.json")
QUOTA_FILE = os.path.join(BASE_DIR, "post_quota.json")
QUEUE_FILE = os.path.join(BASE_DIR, "flyer_queue.json")
OCR_CACHE_FILE = os.path.join(BASE_DIR, "ocr_cache.json")
PRICE_DB_FILE = os.path.join(BASE_DIR, "price_history.db")
OCR_CACHE_TTL_HOURS = 24
OCR_CACHE_MAX_ENTRIES = 300
_LAST_OCR_HAD_ERROR = False
_PRICE_DB_CACHE: Dict[str, PriceHistoryDB] = {}
LOCK_FILE_PATH = os.path.join(BASE_DIR, "deal_pusher.lock")
_LOCK_HANDLE = None

# 일일/회차별 송출 수량 제한, 전단지 배치 및 24시간 전단지 송출 설정 (KST)
ENABLE_GOLDBOX = False
ENABLE_QUIET_HOURS = False
DAILY_MAX_COMMUNITY_DEALS = 200
GOLDBOX_PER_SLOT = 4
DAILY_MAX_TOTAL_DEALS = 200
MAX_COMMUNITY_PER_RUN = 20
MAX_PER_SOURCE_PER_RUN = 6
MIN_COMMUNITY_INTERVAL_MINUTES = 0
FLYER_BATCH_SIZE = 4
FLYER_MAX_WAIT_MINUTES = 20
FLYER_SOLO_WAIT_MINUTES = 60
FLYER_URGENT_DISCOUNT_RATE = 50
FLYER_URGENT_WAIT_MINUTES = 10
FLYER_ITEM_TTL_HOURS = 6
QUIET_HOURS_START = 0
QUIET_HOURS_END = 7

WAIT_EXPIRE_MINUTES = 120
WAIT_RECHECK_MIN_MINUTES = 8
SOURCE_429_DEFAULT_COOLDOWN_MINUTES = 30
SOURCE_403_COOLDOWN_MINUTES = 120
PRICE_DROP_REENTRY_PCT = 5.0

ENABLE_TRACK_B2_NO_ORIG_FOOD = False  # 정가 미확인(할인율 0%) 식품 단가·반응 단독 송출 비활성화

CATEGORY_MIN_DISCOUNT_MAP = {
    "food": 20,
    "living": 20,
    "it": 20,
    "general": 20,
}
HIGH_TICKET_MIN_ORIG_PRICE = 300_000
IT_HIGH_SAVINGS_MIN_RATE = 12
IT_HIGH_SAVINGS_MIN_WON = 50000
LOW_PRICE_BAIT_MAX_PRICE = 15_000
LOW_PRICE_BAIT_MIN_RATE = 15
LOW_PRICE_BAIT_CATEGORIES = ("food", "living")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"
}

SOURCE_SUB_ID_MAP = {
    "arca": "tgarca",
    "ruliweb": "tgruli",
    "ppomppu": "tgppom",
    "dealbada": "tgdealbada",
}

SOURCE_LABEL_MAP = {
    "arca": "아카라이브",
    "ppomppu": "뽐뿌",
    "ruliweb": "루리웹",
    "dealbada": "딜바다",
}


def get_source_label(source_name: str) -> str:
    """출처 코드를 한국어 커뮤니티 이름으로 변환"""
    return SOURCE_LABEL_MAP.get(str(source_name or "").strip().lower(), "커뮤니티")


_COMMENT_EXPAND_TRAIL_RE = re.compile(r"\s*펼쳐보기\s*[▼▾]?\s*$")
_URL_STRIP_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_DOMAIN_STRIP_RE = re.compile(
    r"\b[a-zA-Z0-9.-]+\.(?:com|co\.kr|kr|live|me|gl|ly)(?:/\S*)?",
    re.IGNORECASE,
)

_REACTION_BOILERPLATE_TOKENS = (
    "등록된 댓글이 없습니다",
    "핫딜을 공유하기 위해서는",
    "글쓴이를 배려하여",
    "로그인 후",
    "업자신고",
    "이 글을 보지 않았다면",
)
_REACTION_PROFANITY_TOKENS = (
    "시발", "씨발", "ㅅㅂ", "ㅆㅂ", "병신", "ㅂㅅ", "미친", "ㅁㅊ",
    "개새", "존나", "ㅈㄴ", "지랄", "꺼져", "새끼", "좆", "닥쳐", "ㅗ",
)
_REACTION_NEGATIVE_OR_NOISE_TOKENS = (
    "비싸", "비싼", "안싸", "안 싼", "안삼", "거름", "거르",
    "품절", "끝났", "종료", "마감", "업자", "바이럴", "광고",
    "쿠파스", "파트너스", "수수료", "링크", "로켓아니", "로켓 아니",
    "별로", "노맛", "최악", "취소", "환불", "사기", "낚시", "정가", "불매",
)
_REACTION_POSITIVE_TOKENS = (
    "싸", "저렴", "가성비", "최저가", "역대", "꿀딜", "대란",
    "혜자", "괜찮", "강추", "무조건", "구매", "주문", "쟁여",
    "쟁임", "득템", "단가", "만원", "개당",
)


def _collect_comment_texts(soup: Any, selector: str, limit: int = 15) -> List[str]:
    """이미 파싱된 게시글 HTML에서 중복 제거한 댓글 텍스트 목록 추출"""
    out: List[str] = []
    seen_txt: Set[str] = set()
    for el in soup.select(selector):
        txt = " ".join(el.get_text(" ", strip=True).split())
        txt = _COMMENT_EXPAND_TRAIL_RE.sub("", txt).strip()
        if not txt or txt in seen_txt:
            continue
        seen_txt.add(txt)
        out.append(txt)
        if len(out) >= limit:
            break
    return out


def _clean_reaction_candidate(text: str) -> str:
    s = _URL_STRIP_RE.sub("", str(text or ""))
    s = _DOMAIN_STRIP_RE.sub("", s)
    s = _COMMENT_EXPAND_TRAIL_RE.sub("", s)
    return " ".join(s.split()).strip(" /·|-")


def _is_safe_reaction_candidate(text: str) -> bool:
    if len(text) < 6:
        return False
    low = text.lower()
    if any(tok in low for tok in _REACTION_BOILERPLATE_TOKENS):
        return False
    if any(tok in low for tok in _REACTION_PROFANITY_TOKENS):
        return False
    if any(tok in low for tok in _REACTION_NEGATIVE_OR_NOISE_TOKENS):
        return False
    return True


def _truncate_reaction_quote(text: str, max_len: int) -> str:
    if max_len > 1 and len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def extract_best_reaction_quote(
    comment_texts: List[str],
    fallback_reaction: str = "",
    max_len: int = 34,
) -> str:
    """안전·긍정 필터를 통과한 가장 설득력 있는 커뮤니티 한줄 반응 추출"""
    for raw in comment_texts or []:
        cleaned = _clean_reaction_candidate(raw)
        if not _is_safe_reaction_candidate(cleaned):
            continue
        if any(kw in cleaned for kw in _REACTION_POSITIVE_TOKENS):
            return _truncate_reaction_quote(cleaned, max_len)

    fb = _clean_reaction_candidate(fallback_reaction)
    if _is_safe_reaction_candidate(fb):
        return _truncate_reaction_quote(fb, max_len)
    return ""

CLOSED_KEYWORDS = ["이미 정상화", "가격 정상화", "종료", "품절", "마감", "솔드아웃", "sold out"]
MIN_HOTDEAL_DISCOUNT_RATE = 20
STRONG_HOTDEAL_DISCOUNT_RATE = 40

PROMO_SKIP_KEYWORDS = [
    "쿠팡이츠", "배민", "배달의민족", "요기요", "선착쿠폰", "쿠폰 예고",
    "출석체크", "래플", "응모", "와우 멤버스 데이", "와멤데",
]

KST = timezone(timedelta(hours=9))

ELECTRONICS_KEYWORDS = [
    "노트북", "모니터", "tv", "티비", "아이폰", "iphone", "갤럭시", "태블릿", "아이패드", "ipad",
    "맥북", "macbook", "컴퓨터", "데스크탑", "그래픽카드", "rtx", "라데온", "ssd", "nvme",
    "ram", "메모리", "마이크로sd", "microsd", "sd카드", "cpu", "메인보드", "파워서플라이",
    "본체", "이어폰", "헤드폰", "버즈", "에어팟", "워치", "닌텐도", "스위치", "플스", "ps5",
    "마우스", "키보드", "충전기", "보조배터리", "손난로", "온풍기", "히터", "선풍기", "에어컨",
    "냉장고", "세탁기", "건조기", "청소기", "로봇청소기", "고데기", "드라이기", "면도기",
    "공기청정기", "제습기", "가습기", "블랙박스", "내비게이션", "프로젝터", "사운드바", "스피커",
    "공유기", "자급제", "요가매트", "파우치", "백팩", "캐리어", "인치", "qled", "oled", "uhd", "qhd",
]


def acquire_process_lock() -> bool:
    """중복 실행(스케줄러 겹침 등) 원천 차단용 OS 파일 락"""
    global _LOCK_HANDLE
    try:
        f = open(LOCK_FILE_PATH, "w", encoding="utf-8")
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.write(str(os.getpid()))
        f.flush()
        _LOCK_HANDLE = f
        return True
    except Exception:
        return False


def esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def is_closed_deal(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in CLOSED_KEYWORDS)


def load_seen() -> Set[str]:
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def save_seen(seen: Set[str]):
    """디스크 기존 기록과 병합한 뒤 임시 파일 교체(os.replace)로 기록 유실/축소 원천 차단"""
    try:
        disk_seen: List[str] = []
        if os.path.exists(SEEN_FILE):
            try:
                with open(SEEN_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    disk_seen = loaded
            except Exception:
                pass
        merged = list(dict.fromkeys(disk_seen + list(seen)))[-1500:]
        tmp_path = SEEN_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, SEEN_FILE)
    except Exception as e:
        logger.error(f"seen_deals 저장 에러: {e}")


def load_post_quota() -> Dict[str, Any]:
    today_str = datetime.now(KST).strftime("%Y%m%d")
    default_q = {
        "date": today_str,
        "community_sent": 0,
        "goldbox_sent": 0,
        "last_community_ts": 0,
    }
    if os.path.exists(QUOTA_FILE):
        try:
            with open(QUOTA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("date") == today_str:
                return {
                    "date": today_str,
                    "community_sent": int(data.get("community_sent", 0) or 0),
                    "goldbox_sent": int(data.get("goldbox_sent", 0) or 0),
                    "last_community_ts": int(data.get("last_community_ts", 0) or 0),
                }
        except Exception:
            pass
    return default_q


def save_post_quota(q: Dict[str, Any]):
    try:
        tmp_path = QUOTA_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(q, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, QUOTA_FILE)
    except Exception as e:
        logger.error(f"post_quota 저장 에러: {e}")


def load_flyer_queue() -> List[Dict[str, Any]]:
    if os.path.exists(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except Exception:
            pass
    return []


def save_flyer_queue(queue: List[Dict[str, Any]]):
    try:
        tmp_path = QUEUE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, QUEUE_FILE)
    except Exception as e:
        logger.error(f"flyer_queue 저장 에러: {e}")


def _is_transient_http_status(status_code: int) -> bool:
    return status_code in (403, 408, 429) or status_code >= 500


def load_ocr_cache() -> Dict[str, Any]:
    """실행 간 OCR 가격 헤더 캐시 로드 (만료 항목 자동 제외)"""
    if not os.path.exists(OCR_CACHE_FILE):
        return {}
    try:
        with open(OCR_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        now_ts = int(time.time())
        cutoff = now_ts - OCR_CACHE_TTL_HOURS * 3600
        cleaned: Dict[str, Any] = {}
        for k, v in data.items():
            if not isinstance(v, dict):
                continue
            upd = int(v.get("updated_at", 0) or 0)
            ocr_txt = v.get("ocr_text")
            fp = v.get("fingerprint")
            if upd >= cutoff and isinstance(ocr_txt, str) and ocr_txt.strip() and isinstance(fp, str) and fp:
                cleaned[str(k)] = v
        return cleaned
    except Exception:
        return {}


def save_ocr_cache(cache: Dict[str, Any]):
    """OCR 캐시를 TTL/최대 개수 상한으로 정리해 원자적으로 저장"""
    try:
        now_ts = int(time.time())
        cutoff = now_ts - OCR_CACHE_TTL_HOURS * 3600
        valid_items = [
            (str(k), v)
            for k, v in cache.items()
            if isinstance(v, dict)
            and int(v.get("updated_at", 0) or 0) >= cutoff
            and isinstance(v.get("ocr_text"), str)
            and v["ocr_text"].strip()
        ]
        valid_items.sort(key=lambda kv: int(kv[1].get("updated_at", 0) or 0), reverse=True)
        trimmed = dict(valid_items[:OCR_CACHE_MAX_ENTRIES])
        cache.clear()
        cache.update(trimmed)

        tmp_path = OCR_CACHE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(trimmed, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, OCR_CACHE_FILE)
    except Exception as e:
        logger.error(f"ocr_cache 저장 에러: {e}")


def _build_price_evidence_fingerprint(
    pricing_title: str,
    body_text: str,
    ocr_imgs: List[str],
    extra_shipping: str = "",
) -> str:
    """추천/댓글 수를 제외한 제목·본문·배송·이미지 URL 기반 가격 근거 지문 생성"""
    payload = {
        "title": re.sub(r"\s+", " ", (pricing_title or "").strip()),
        "body": (body_text or "").strip(),
        "imgs": [str(u).strip() for u in (ocr_imgs or [])[:2] if u],
        "shipping": (extra_shipping or "").strip(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_quiet_hours(now: Optional[datetime] = None) -> bool:
    if not ENABLE_QUIET_HOURS or "--ignore-quiet" in sys.argv:
        return False
    cur = now or datetime.now(KST)
    return QUIET_HOURS_START <= cur.hour < QUIET_HOURS_END


def can_send_community_now(
    quota: Dict[str, Any],
    run_comm_sent: int,
    now_ts: Optional[float] = None,
    now_dt: Optional[datetime] = None,
) -> Tuple[bool, str]:
    if is_quiet_hours(now_dt):
        return False, "quiet_hours"
    if run_comm_sent >= MAX_COMMUNITY_PER_RUN:
        return False, "per_run_cap"
    comm_cnt = int(quota.get("community_sent", 0) or 0)
    gb_cnt = int(quota.get("goldbox_sent", 0) or 0)
    if comm_cnt >= DAILY_MAX_COMMUNITY_DEALS:
        return False, "daily_comm_cap"
    if (comm_cnt + gb_cnt) >= DAILY_MAX_TOTAL_DEALS:
        return False, "daily_total_cap"
    if MIN_COMMUNITY_INTERVAL_MINUTES > 0:
        last_ts = int(quota.get("last_community_ts", 0) or 0)
        cur_ts = time.time() if now_ts is None else now_ts
        if last_ts > 0 and (cur_ts - last_ts) < MIN_COMMUNITY_INTERVAL_MINUTES * 60:
            return False, "cooldown"
    return True, "ok"


def clean_coupang_url(raw_url: str) -> Tuple[str, str]:
    """
    단축링크(link.coupang.com 등) 302 Location 헤더를 추적해 제3자 lptag를 제거하고
    정규 상품 URL(https://www.coupang.com/vp/products/{pid}?...) 및 (clean_url, product_id) 반환.
    """
    url = (raw_url or "").strip()
    if not url:
        return "", ""

    if "unsafelink.com/" in url:
        url = url.split("unsafelink.com/")[-1]

    # link.coupang.com 단축링크는 allow_redirects=False로 Location만 추적 (www.coupang.com 403 우회)
    for _ in range(2):
        if "link.coupang.com" not in url:
            break
        try:
            r = requests.get(url, headers=HEADERS, allow_redirects=False, timeout=6)
            loc = r.headers.get("Location", "")
            if not loc:
                break
            url = loc
        except Exception:
            break

    m_pid = re.search(r"/products/(\d+)", url) or re.search(r"pageKey=(\d+)", url)
    if not m_pid:
        return url, ""

    product_id = m_pid.group(1)
    m_item = re.search(r"itemId=(\d+)", url)
    m_vendor = re.search(r"vendorItemId=(\d+)", url)

    clean_url = f"https://www.coupang.com/vp/products/{product_id}"
    qs: List[str] = []
    if m_item:
        qs.append(f"itemId={m_item.group(1)}")
    if m_vendor:
        qs.append(f"vendorItemId={m_vendor.group(1)}")
    if qs:
        clean_url += "?" + "&".join(qs)

    return clean_url, product_id


def is_product_recently_seen(product_id: str, seen: Set[str]) -> bool:
    """최근 3일 이내 동일 Coupang productId가 이미 송출되었는지 확인 (커뮤니티 간 중복 방지)"""
    if not product_id:
        return False
    now = datetime.now(KST)
    for delta in range(3):
        day_str = (now - timedelta(days=delta)).strftime("%Y%m%d")
        if f"cp_prod_{product_id}_{day_str}" in seen:
            return True
    return False


def mark_product_seen(product_id: str, seen: Set[str]):
    if not product_id:
        return
    day_str = datetime.now(KST).strftime("%Y%m%d")
    seen.add(f"cp_prod_{product_id}_{day_str}")


# ==========================================
# OCR 전처리 및 쿠팡 가격 헤더존 정밀 추출
# ==========================================
def _preprocess_image_for_price_ocr(img):
    """
    쿠팡 캡처 이미지의 2대 OCR 난점 해결:
    1) 빨간 할인율 배지(흰 글씨 '13%', '28%', '29%') 고대비 반전
    2) 회색 정상가 중앙을 가로지르는 1~2px 가로 취소선(strikethrough)만 선택 복원 ('0'->'e' 오인 방지)
    3) 3배 LANCZOS 업스케일링
    """
    from PIL import Image
    rgb_img = img.convert("RGB")
    w, h = rgb_img.size
    try:
        import numpy as np
        rgb = np.array(rgb_img, dtype=np.int16)

        # 1. 빨간 할인율 배지 영역 고대비 변환
        red_px = ((rgb[:, :, 0] - rgb[:, :, 1]) > 85) & (rgb[:, :, 0] > 140)
        cs = np.pad(red_px.astype(np.int32), ((1, 0), (1, 0))).cumsum(axis=0).cumsum(axis=1)
        ry, rx = 6, 10
        y1 = np.clip(np.arange(h) - ry, 0, h)
        y2 = np.clip(np.arange(h) + ry + 1, 0, h)
        x1 = np.clip(np.arange(w) - rx, 0, w)
        x2 = np.clip(np.arange(w) + rx + 1, 0, w)
        box_red = (
            cs[y2[:, None], x2[None, :]]
            - cs[y1[:, None], x2[None, :]]
            - cs[y2[:, None], x1[None, :]]
            + cs[y1[:, None], x1[None, :]]
        )
        area = (y2 - y1)[:, None] * (x2 - x1)[None, :]
        in_red_badge = box_red > (area * 0.25)

        out = rgb.copy()
        badge_val = 255 - rgb[:, :, 1]
        badge_val = np.where(badge_val > 140, 250, np.where(badge_val < 80, 15, badge_val))
        for c in range(3):
            out[:, :, c] = np.where(in_red_badge, badge_val, out[:, :, c])

        # 2. 가로 취소선 선택 제거 (위아래 배경인 구간의 가로선만 지워 세로 획 보존)
        gray = out.mean(axis=2)
        for thickness in (1, 2):
            dy = thickness + 1
            for y in range(dy, h - dy):
                row = gray[y, :]
                above = gray[y - dy, :]
                below = gray[y + dy, :]
                for is_fg, ab_is_bg in (
                    (row < 218, (above > 225) & (below > 225)),
                    (row > 65, (above < 48) & (below < 48)),
                ):
                    idx = 0
                    while idx < w:
                        if is_fg[idx]:
                            start = idx
                            while idx < w and is_fg[idx]:
                                idx += 1
                            if (idx - start) >= 22 and ab_is_bg[start:idx].mean() >= 0.35:
                                erase_mask = ab_is_bg[start:idx]
                                for yy in range(y, min(h, y + thickness)):
                                    for c in range(3):
                                        seg = out[yy, start:idx, c]
                                        bg_rep = (out[y - dy, start:idx, c] + out[y + dy, start:idx, c]) // 2
                                        out[yy, start:idx, c] = np.where(erase_mask, bg_rep, seg)
                        else:
                            idx += 1

        healed = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))
        return healed.resize((w * 3, h * 3), Image.Resampling.LANCZOS)
    except Exception:
        return rgb_img.resize((w * 3, h * 3), Image.Resampling.LANCZOS)


def _ocr_single_image_lines(pil_img) -> List[str]:
    """단일 PIL 이미지에서 Y좌표(위->아래) 정렬된 텍스트 라인 리스트 추출"""
    global _LAST_OCR_HAD_ERROR
    # 1) Windows 환경: winocr (단어 바운딩박스 기준 정확한 상->하 정렬)
    try:
        import winocr
        import asyncio

        async def _winocr():
            return await winocr.recognize_pil(pil_img, lang="ko")

        res_obj = asyncio.run(_winocr())
        if res_obj is not None:
            if not getattr(res_obj, "lines", None):
                return []
            items = []
            for line in res_obj.lines:
                txt = (line.text or "").strip()
                if not txt:
                    continue
                if getattr(line, "words", None):
                    y0 = min(w.bounding_rect.y for w in line.words)
                    x0 = min(w.bounding_rect.x for w in line.words)
                else:
                    y0, x0 = 0.0, 0.0
                items.append((int(y0 // 8), int(x0), txt))
            items.sort(key=lambda t: (t[0], t[1]))
            return [t[2] for t in items]
    except Exception:
        pass

    # 2) Linux/GitHub Actions 환경: pytesseract
    try:
        import pytesseract
        text = pytesseract.image_to_string(pil_img, lang="kor+eng")
        if text:
            return [ln.strip() for ln in text.splitlines() if ln.strip()]
        return []
    except Exception:
        _LAST_OCR_HAD_ERROR = True

    return []


# 가격 블록 아래쪽(옵션/보험/적립/결제버튼) 시작을 알리는 경계 패턴
_OCR_STOP_BOUNDARY_RE = re.compile(
    r"(?:"
    r"색상\s*:|"
    r"저장용량\s*[:\s]|"
    r"운영체제\s*:|"
    r"스탠드/벽걸이|"
    r"사이즈\s*:|"
    r"선택안함|"
    r"applecare|ap하ecare|애플케어|"
    r"쿠팡안심케어|쿠광안심게어|안심케어|무상보증연장|"
    r"카드할인\s|카드\s*혜택|카드\s*하택|"
    r"장바구니\s*담기|바로구매"
    r")",
    re.IGNORECASE,
)


def _truncate_ocr_to_price_header(lines: List[str]) -> List[str]:
    """
    쿠팡 상세 캡처에서 상단 상품명~가격 영역만 남기고,
    옵션(+370,000원 등), AppleCare+(305,150원), 안심케어(5,000원), 캐시적립(50,410원) 등 하단 영역은 원천 절단.
    """
    kept: List[str] = []
    for ln in lines:
        if _OCR_STOP_BOUNDARY_RE.search(ln):
            break
        kept.append(ln)
    return kept


def extract_ocr_text(img_urls: Optional[Union[str, List[str]]]) -> str:
    """
    첨부된 이미지(최대 2장)에서 원본 패스 + 취소선/배지 복원 패스 듀얼 OCR 수행 후
    가격 헤더 영역 텍스트만 반환.
    """
    global _LAST_OCR_HAD_ERROR
    _LAST_OCR_HAD_ERROR = False
    if not img_urls:
        return ""
    urls = [img_urls] if isinstance(img_urls, str) else list(img_urls)

    from PIL import Image
    collected_blocks: List[str] = []

    for u in urls[:2]:
        if not u or u.endswith(".svg") or "twemoji" in u:
            continue
        try:
            res = requests.get(u, headers=HEADERS, timeout=7)
            if res.status_code != 200:
                _LAST_OCR_HAD_ERROR = True
                continue
            img = Image.open(io.BytesIO(res.content))
            if img.width < 120 or img.height < 80:
                continue

            raw_lines = _truncate_ocr_to_price_header(_ocr_single_image_lines(img))
            enh_img = _preprocess_image_for_price_ocr(img)
            enh_lines = _truncate_ocr_to_price_header(_ocr_single_image_lines(enh_img))

            if raw_lines or enh_lines:
                logger.info("📸 [OCR] 이미지 가격 헤더 영역 텍스트 인식 성공")
                block = "\n".join(raw_lines) + "\n---OCR_ENHANCED---\n" + "\n".join(enh_lines)
                collected_blocks.append(block)
        except Exception as e:
            _LAST_OCR_HAD_ERROR = True
            logger.debug(f"OCR 추출 실패 ({u}): {e}")

    return "\n===NEXT_IMAGE===\n".join(collected_blocks)


def get_or_extract_ocr_text(
    deal_id: str,
    pricing_title: str,
    body_text: str,
    ocr_imgs: List[str],
    ocr_cache: Dict[str, Any],
    extra_shipping: str = "",
) -> Tuple[str, bool]:
    """가격 근거가 동일하면 이전 실행의 OCR 결과를 재사용하고, 실패 시 재시도 가능하도록 캐시 제외"""
    global _LAST_OCR_HAD_ERROR
    if not ocr_imgs:
        return "", False

    fp = _build_price_evidence_fingerprint(pricing_title, body_text, ocr_imgs, extra_shipping=extra_shipping)
    now_ts = int(time.time())
    entry = ocr_cache.get(deal_id)
    if (
        isinstance(entry, dict)
        and entry.get("fingerprint") == fp
        and (now_ts - int(entry.get("updated_at", 0) or 0)) <= OCR_CACHE_TTL_HOURS * 3600
        and isinstance(entry.get("ocr_text"), str)
        and entry["ocr_text"].strip()
    ):
        logger.info(f"♻️ [OCR 캐시 재사용] {deal_id}")
        return entry["ocr_text"], False

    _LAST_OCR_HAD_ERROR = False
    try:
        ocr_text = extract_ocr_text(ocr_imgs)
    except Exception as e:
        logger.warning(f"⚠️ [OCR 실패] {deal_id}: {e}")
        if deal_id in ocr_cache:
            ocr_cache.pop(deal_id, None)
            save_ocr_cache(ocr_cache)
        return "", True

    had_err = bool(_LAST_OCR_HAD_ERROR)
    clean_ocr = (ocr_text or "").strip()
    if clean_ocr and not had_err:
        ocr_cache[deal_id] = {
            "fingerprint": fp,
            "ocr_text": ocr_text,
            "updated_at": now_ts,
        }
        save_ocr_cache(ocr_cache)
        return ocr_text, False

    if deal_id in ocr_cache:
        ocr_cache.pop(deal_id, None)
        save_ocr_cache(ocr_cache)
    return ocr_text or "", had_err


# ==========================================
# 상품명 정제 및 가성비 단가 계산
# ==========================================
def clean_display_title(raw_title: str) -> str:
    """커뮤니티 말머리/가격/배송비 꼬리표를 제거하고 깔끔한 실제 상품명만 남김"""
    t = raw_title.strip()
    t = re.sub(r"^뽐뿌::\s*", "", t)
    t = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", t)
    # 끝에 붙은 댓글 수 제거: [12] 또는 (2)
    t = re.sub(r"\s*(?:\[\d+\]|\(\d+\))\s*$", "", t)
    # 끝에 붙은 (가격/배송) 괄호 제거: (23,100원/무료), (89,890/무료), (13,600원) (무배)
    for _ in range(3):
        t = re.sub(
            r"\s*\([^\)]*?(?:[\d,]{4,}\s*원?|무료|무배|와우|로켓|배송)[^\)]*?\)\s*$",
            "",
            t,
            flags=re.IGNORECASE,
        )
    # 슬래시 구분 가격 꼬리표 제거: / 9,370원(로켓 무료배송) / 11% 세일, /17,220원 와우 무료배송
    t = re.sub(r"\s*/\s*[\d,]+\s*원.*$", "", t)
    # 끝에 단독으로 붙은 배송비·가격 꼬리표 제거: 17,900원 배송비 3,000원 / 66030원
    t = re.sub(r"\s*(?:유료배송|배송비|택배비|배송료)\s*[:：\+\-]?\s*\d[\d,]*\s*원\s*$", "", t)
    t = re.sub(r"\s+\d[\d,]*\s*원\s*(?:배송비|택배비|배송료)\s*$", "", t)
    t = re.sub(r"\s+\d[\d,]{3,}\s*원(?:\s*(?:와우|무료|무배|로켓)\S*)*\s*$", "", t)
    t = re.sub(r"\s+", " ", t).strip(" /,-")
    return t if len(t) >= 2 else raw_title.strip()


def clean_spec_title(t: str) -> str:
    """골드박스 보조용 SKU 검색 결과 정제"""
    bad_words = [
        "로그인", "회원가입", "게시판", "조회수", "등록일", "포럼", "자유게시판",
        "시간 전", "일 전", "오늘의집", "쿠팡상품번호", "이미 정상화",
    ]
    if any(bad in t for bad in bad_words):
        return ""
    t = re.sub(r"^.*?›\s*", "", t)
    t = re.sub(r"\[[^\]]*\]", "", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"\([^\)]*?(?:무료|무배|와우|역대가|원|\d+)[^\)]*?\)", "", t)
    t = re.sub(r"(?:최저가|가격\s*비교|가격변동|와우할인).*$", "", t)
    t = re.sub(r"[\d,]+원.*", "", t)
    t = re.sub(r"[|/·•].*$", "", t)
    t = re.sub(r"\s*새\s*창\s*열림\s*", "", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.-")
    if len(t) > 50:
        return ""
    return t


def enrich_product_spec(p_name: str, p_url: str, img_url: Optional[str] = None) -> str:
    """골드박스 API 전용 축약 상품명 규격 보강 (전자제품 및 이미 규격 있는 상품은 원본 유지)"""
    if not p_url:
        return p_name
    if any(k in p_name.lower() for k in ELECTRONICS_KEYWORDS):
        return p_name

    has_count = re.search(r"(\d+)\s*(개입|개|봉|팩|캔|병|포|박스|롤|통|입|매|세트|정|캡슐)(?![a-zA-Z])", p_name)
    has_weight = re.search(r"(\d+(?:\.\d+)?)\s*(ml|l|리터|g|kg|그램|킬로)(?![a-zA-Z])", p_name, re.IGNORECASE)
    if has_count and has_weight:
        return p_name

    m_v = re.search(r"vendorItemId=(\d+)", p_url)
    m_i = re.search(r"itemId=(\d+)", p_url)
    search_ids = [x for x in [m_v.group(1) if m_v else "", m_i.group(1) if m_i else ""] if x]

    keywords = [w for w in p_name.split() if len(w) >= 2][:2]
    if len(keywords) < 2:
        return p_name

    for target_id in search_ids[:2]:
        try:
            u = "https://search.naver.com/search.naver?query=" + requests.utils.quote(f"쿠팡 {target_id}")
            r = requests.get(u, headers=HEADERS, timeout=3)
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup.select("a"):
                txt = el.text.strip()
                if all(k in txt for k in keywords):
                    c_txt = clean_spec_title(txt)
                    if not c_txt or len(c_txt) <= len(p_name) or len(c_txt) > len(p_name) + 20:
                        continue
                    if c_txt.count(",") >= 3:
                        continue
                    c_cnt = bool(re.search(r"(\d+)\s*(개입|개|봉|팩|캔|병|포|박스|롤|통|입|매|정)(?![a-zA-Z])", c_txt))
                    c_wt = bool(re.search(r"(\d+(?:\.\d+)?)\s*(ml|l|g|kg)(?![a-zA-Z])", c_txt, re.IGNORECASE))
                    if c_cnt and c_wt:
                        logger.info(f"✨ 골드박스 규격 보강: {p_name} ➡️ {c_txt}")
                        return c_txt
        except Exception:
            pass

    return p_name


def compute_unit_pricing(name: str, price: int) -> str:
    """식품/생필품 전용 개당 단가 및 100g/100ml당 단가 계산 (전자기기·가전·잡화 철저 배제)"""
    if price <= 0:
        return ""

    low_name = name.lower()
    if any(k in low_name for k in ELECTRONICS_KEYWORDS):
        return ""
    # 용량 표기가 저장장치(GB, TB)나 배터리(mAh)인 경우 배제
    if re.search(r"\d+\s*(?:gb|tb|mah|wh|w|hz)\b", low_name):
        return ""

    clean_name = strip_purchase_limit_phrases(name)
    counts = re.findall(
        r"(?<!\d)(?<!\d,)(\d{1,3}(?:,\d{3})+|\d+)\s*(개입|개|봉|팩|캔|병|포|박스|롤|통|입|매|세트|정|캡슐|알)(?![a-zA-Z])",
        clean_name,
        re.IGNORECASE,
    )
    weights = re.findall(
        r"(?<!\d)(?<!\d,)((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(kg|l|리터|킬로|g|ml|그램)(?![a-zA-Z])",
        clean_name,
        re.IGNORECASE,
    )

    count_val = 0
    count_unit = ""
    if len(counts) >= 2:
        c1, u1 = int(counts[0][0].replace(",", "")), counts[0][1]
        c2, u2 = int(counts[-1][0].replace(",", "")), counts[-1][1]
        # 예: '5개입 1팩' -> 5개, '24개입 2개' -> 48개, '60정 2개' -> 120정, '20병 2박스' -> 40병
        if c2 == 1 and c1 > 1:
            count_val = c1
            count_unit = "개" if u1 == "개입" else u1
        elif c1 > 1 and c2 > 1 and u1 in ("개입", "입", "매", "정", "캡슐", "알", "롤", "포", "병", "캔", "봉", "개"):
            count_val = c1 * c2
            count_unit = "개" if u1 in ("개입", "입") else u1
        else:
            count_val = c2
            count_unit = "개" if u2 == "개입" else u2
    elif len(counts) == 1:
        count_val = int(counts[0][0].replace(",", ""))
        count_unit = "개" if counts[0][1] == "개입" else counts[0][1]

    total_amount = 0.0
    amount_unit = "g"
    if weights:
        w_val = float(weights[0][0].replace(",", ""))
        w_u = weights[0][1].lower()
        if w_u in ("kg", "킬로"):
            single_amount = w_val * 1000
            amount_unit = "g"
        elif w_u in ("l", "리터"):
            single_amount = w_val * 1000
            amount_unit = "ml"
        elif w_u == "ml":
            single_amount = w_val
            amount_unit = "ml"
        else:
            single_amount = w_val
            amount_unit = "g"

        # '60정' 같은 정제류가 아닌 일반 중량/용량일 때 수량 곱산
        if count_val > 1 and len(weights) == 1 and count_unit not in ("정", "캡슐", "알"):
            total_amount = single_amount * count_val
        else:
            total_amount = single_amount

    parts = []
    if count_val > 1:
        u_p = round(price / count_val)
        parts.append(f"<b>{count_unit}당 약 {u_p:,}원</b>")

    if total_amount >= 50:
        unit_100 = round(price / (total_amount / 100))
        parts.append(f"100{amount_unit}당 약 {unit_100:,}원")

    if parts:
        return "• <b>단가계산</b>: " + " (".join(parts) + (")" if len(parts) > 1 else "") + " 꼴!\n"
    return ""


# ==========================================
# 핵심 가격 분석 엔진 (100% 명시된 팩트만 추출, 역산/추측 절대 금지)
# ==========================================
_BLACKLIST_LINE_RE = re.compile(
    r"(?:"
    r"무이자|할부|\d+\s*개월|"
    r"^\s*월\s*[\d,]|"
    r"적립|석맆|적맇|쿠팡캐시|쿠팣개시|개시적립|개시석맆|최대적립|"
    r"안심케어|안심게어|무상보증|보증연장|보증기간|만원\s*한도|"
    r"applecare|ap하ecare|애플케어|"
    r"쿠폰\s*적용|구폰\s*적용|두든\s*적용|"
    r"원\s*(?:이상|미만)|배송비"
    r")",
    re.IGNORECASE,
)


def _strip_inline_non_prices(text: str) -> str:
    """한 줄 내부에 섞여 있는 단위가격, 할부금, 옵션추가금(+/-), 적립금, 상품평 수를 제거"""
    s = text
    # 괄호형 단위가격: (10g당 128원), (1정당 230원), (100ml당 830원)
    s = re.sub(r"\([^\)]*?당\s*[\d,\.\s]+원[^\)]*?\)", " ", s)
    s = re.sub(
        r"\d+\s*(?:g|kg|ml|l|정|개|매|포|입|캡슐|봉|팩|캔|병|롤)\s*당\s*[\d,\.\s]+원",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    # 월 할부금 표기: 월266,333원~, 월 50,000원, 266,333원~
    s = re.sub(r"월\s*[\d,\.\s]+원\s*~?", " ", s)
    s = re.sub(r"[\d,\.\s]+원\s*~", " ", s)
    # +/- 부호 수식 금액 (옵션 추가금 +370,000원, 쿠폰 차감액 -48,950원)
    s = re.sub(r"[+\-]\s*\d[\d,\.\s]*원", " ", s)
    # 적립금 문구
    s = re.sub(r"(?:최대\s*)?\d[\d,\.\s]*원\s*(?:쿠팡캐시|캐시|최대적립|적립|개시적립)", " ", s)
    # 상품평/구매자수
    s = re.sub(r"[\d,]+\s*(?:개\s*상품평|명\s*이상)", " ", s)
    return s


_SHIPPING_THRESHOLD_SPAN_RE = re.compile(
    r"(?:(?:로켓(?:와우|배송|직구|프레시)?|와우|일반)?\s*(?:무료)?배송\s*)?"
    r"(?:\d[\d,\.\s]*원|\d+(?:\.\d+)?\s*만\s*원)\s*(?:이상|미만)"
    r"(?:\s*(?:구매|주문)?\s*(?:시\s*)?(?:무료배송|무배|배송비\s*무료|무료|할인|적용)?)?",
    re.IGNORECASE,
)
_SHIPPING_FEE_FORWARD_RE = re.compile(
    r"(?:유료배송|배송비|택배비|배송료|운송비)[ \t]*(?:은|는)?[ \t]*(?:별도[ \t]*)?[:：\+\-\(]?[ \t\r\n]*\d[\d,\. \t]*원\)?",
    re.IGNORECASE,
)
_SHIPPING_FEE_PAREN_REVERSE_RE = re.compile(
    r"[\(\[][ \t]*\d[\d,\. \t]*원[ \t]*(?:배송비|택배비|배송료|운송비)(?:[ \t]*(?:별도|착불))?[ \t]*[\)\]]",
    re.IGNORECASE,
)
_SHIPPING_FEE_REVERSE_RE = re.compile(
    r"\d[\d,\. \t]*원[ \t]*(?:배송비(?![ \t]*(?:무료|0[ \t]*원|없음))|택배비|배송료|운송비)(?:[ \t]*(?:별도|착불))?",
    re.IGNORECASE,
)


def _strip_pair_ship_fee(m: re.Match) -> str:
    open_b, left_s, _sep, right_s, close_b = m.groups()
    if re.search(r"판매가|할인가|특가|정가|정상가|쿠팡가|와우가|실구매가", right_s):
        return m.group(0)
    l_nums = re.findall(r"\d[\d,]*", left_s)
    r_nums = re.findall(r"\d[\d,]*", right_s)
    if not l_nums or not r_nums:
        return m.group(0)
    lv = int(l_nums[-1].replace(",", ""))
    rv = int(r_nums[0].replace(",", ""))
    if 1000 <= rv <= 10000 and rv < lv:
        return f"{open_b}{left_s.strip()}{close_b}"
    return m.group(0)


def _strip_shipping_and_threshold_phrases(text: str) -> str:
    """배송비·무료배송 기준 금액('29,800원 이상 무료배송', '배송비 3,000원', '(3,000원 배송비 별도)', '(12,000/3,000)') 순차 제거"""
    s = _SHIPPING_THRESHOLD_SPAN_RE.sub(" ", text or "")
    s = _SHIPPING_FEE_FORWARD_RE.sub(" ", s)
    s = _SHIPPING_FEE_PAREN_REVERSE_RE.sub(" ", s)
    s = re.sub(r"(?:유료배송|배송비|택배비|배송료|운송비)[ \t]*(?:은|는)?[ \t]*(?:무료|0[ \t]*원|없음|별도|착불|포함|제외|있음)", " ", s)
    s = _SHIPPING_FEE_REVERSE_RE.sub(" ", s)
    s = re.sub(
        r"([\(\[])([^\)\]/\r\n]+)(\s*/\s*)([^\)\]/\r\n]*\d[\d,]*\s*원?[^\)\]/\r\n]*)([\)\]])",
        _strip_pair_ship_fee,
        s,
    )
    return s


_NEGATED_FREE_SHIPPING_RE = re.compile(
    r"(?:(?:로켓|와우)\s*)?(?:무료배송|무배|배송비\s*무료|무료)\s*(?:아님|아니[며고라]?|불가|제외|미적용|없음|안\s*됨|안됨|X|x|×)",
    re.IGNORECASE,
)
_NEGATED_ROCKET_RE = re.compile(
    r"로켓\s*(?:와우|배송|프레시|직구)?\s*(?:아님|아니[며고라]?|불가|제외|미적용|없음|안\s*됨|안됨|X|x|×)",
    re.IGNORECASE,
)
_WOW_FREE_SPAN_RE = re.compile(
    r"(?:로켓\s*)?와우\s*(?:멤버십\s*)?(?:회원\s*)?(?:만|전용|한정|기준|시)?\s*"
    r"(?:(?:[\d,]+\s*원|\d+(?:\.\d+)?\s*만\s*원)\s*이상(?:\s*(?:구매|주문|결제)?\s*(?:시|일\s*때)?\s*)?)?"
    r"(?:무료배송|무배|배송비\s*무료|무료)",
    re.IGNORECASE,
)
_MEMBER_FREE_SPAN_RE = re.compile(
    r"(?:유료\s*회원|멤버십\s*회원?|회원\s*(?:만|전용|한정))\s*"
    r"(?:(?:[\d,]+\s*원|\d+(?:\.\d+)?\s*만\s*원)\s*이상(?:\s*(?:구매|주문|결제)?\s*(?:시|일\s*때)?\s*)?)?"
    r"(?:무료배송|무배|배송비\s*무료|무료)",
    re.IGNORECASE,
)
_RESTRICTED_FREE_RE = re.compile(
    r"([가-힣a-zA-Z0-9·]+(?:/[가-힣a-zA-Z0-9·]+)?(?:\s*(?:전용|한정|제외|일부)|만))"
    r"(?:\s+|(?=무료배송|무배|배송비))"
    r"(?:(?:[\d,]+\s*원|\d+(?:\.\d+)?\s*만\s*원)\s*이상(?:\s*(?:구매|주문|결제)?\s*(?:시|일\s*때)?\s*)?)?"
    r"(?:무료배송|무배|배송비\s*무료)",
    re.IGNORECASE,
)
_QTY_FREE_RE = re.compile(
    r"(\d+\s*(?:개|팩|박스|병|캔|세트|통|봉|포)\s*이상)\s*(?:구매\s*(?:시\s*)?)?(?:무료배송|무배|배송비\s*무료)",
    re.IGNORECASE,
)


def parse_shipping_details(
    title: str,
    body: str = "",
    ocr_text: str = "",
    extra_shipping: str = "",
) -> Dict[str, Any]:
    """원문(제목·본문·OCR·아카라이브 배송란)에서 근거가 확인된 배송비/배송유형 및 복합 조건까지 정밀 추출"""
    parts = [p for p in (extra_shipping, title, body, ocr_text) if p]
    combined = "\n".join(parts)
    if not combined.strip():
        return {"shipping_fee": 0, "shipping_type": "", "shipping_condition": "", "shipping_label": ""}

    # 부정 표현 먼저 감지·제거해 '무료배송 아님' / '로켓배송 아님' 오탐 방지
    has_negated_free = bool(_NEGATED_FREE_SHIPPING_RE.search(combined))
    clean_for_pos = _NEGATED_FREE_SHIPPING_RE.sub(" ", combined)
    clean_for_rocket = _NEGATED_ROCKET_RE.sub(" ", clean_for_pos)

    has_rocket = bool(re.search(r"로켓\s*(?:무료배송|무배|배송|와우|프레시|직구)|로켓배송|로켓와우", clean_for_rocket))
    has_wow_free = bool(_WOW_FREE_SPAN_RE.search(clean_for_pos))
    after_wow = _WOW_FREE_SPAN_RE.sub(" ", clean_for_pos)
    has_member_free = bool(_MEMBER_FREE_SPAN_RE.search(after_wow))
    after_member = _MEMBER_FREE_SPAN_RE.sub(" ", after_wow)

    restricted_cond = ""
    m_rest = _RESTRICTED_FREE_RE.search(after_member)
    if m_rest:
        cand_rest = re.sub(r"\s+", " ", m_rest.group(1)).strip()
        # 상품명 일반 수식어/부정어 오탐 제외
        if not any(bad in cand_rest for bad in ("오늘만", "이번만", "하루만")):
            restricted_cond = cand_rest

    qty_cond = ""
    m_qty = _QTY_FREE_RE.search(clean_for_pos)
    if m_qty:
        qty_cond = re.sub(r"\s+", " ", m_qty.group(1)).strip()

    # 무료배송 조건 금액 추출 (예: 29,800원 이상 무료배송 / 3만원 이상 무료배송)
    threshold_cond = ""
    m_thr = re.search(
        r"([\d,]+)\s*원\s*이상(?:\s*(?:구매|주문|결제)?\s*(?:시|일\s*때)?\s*)?(?:무료배송|무배|배송비\s*무료|무료)",
        clean_for_pos,
    )
    if m_thr:
        try:
            thr_val = int(m_thr.group(1).replace(",", ""))
            if thr_val >= 1000:
                threshold_cond = f"{thr_val:,}원 이상 무료배송"
        except Exception:
            pass
    if not threshold_cond:
        m_man = re.search(
            r"(\d+(?:\.\d+)?)\s*만\s*원\s*이상(?:\s*(?:구매|주문|결제)?\s*(?:시|일\s*때)?\s*)?(?:무료배송|무배|배송비\s*무료|무료)",
            clean_for_pos,
        )
        if m_man:
            try:
                thr_val = int(round(float(m_man.group(1)) * 10000))
                if thr_val >= 10000:
                    threshold_cond = f"{thr_val:,}원 이상 무료배송"
            except Exception:
                pass

    # 무료배송 기준 금액('29,800원 이상 무료배송' 등) 및 '배송비 무료/0원'을 먼저 걷어낸 뒤에만 유료 배송비 추출
    combined_no_thr = _SHIPPING_THRESHOLD_SPAN_RE.sub(" ", combined)
    combined_no_thr = re.sub(r"(?:유료배송|배송비|택배비|배송료|운송비)[ \t]*(?:은|는)?[ \t]*(?:무료|0[ \t]*원|없음)", " ", combined_no_thr)
    extra_no_thr = _SHIPPING_THRESHOLD_SPAN_RE.sub(" ", extra_shipping or "")

    shipping_fee = 0
    for pat in (
        r"(?:유료배송|배송비|택배비|배송료|운송비)[ \t]*(?:은|는)?[ \t]*(?:별도[ \t]*)?[:：\+\-\(]?[ \t\r\n]*([\d,]+)[ \t]*원",
        r"[\(\[][ \t]*([\d,]+)[ \t]*원[ \t]*(?:배송비|택배비|배송료|운송비)",
    ):
        for fm in re.finditer(pat, combined_no_thr):
            val = int(fm.group(1).replace(",", ""))
            if 500 <= val <= 50000:
                shipping_fee = val
                break
        if shipping_fee:
            break

    if shipping_fee == 0:
        combined_rev_scan = re.sub(
            r"(?:유료배송|배송비|택배비|배송료|운송비)[ \t]*(?:은|는)?[ \t]*(?:무료|0[ \t]*원|없음|별도|착불|포함|제외|있음)",
            " ",
            combined_no_thr,
        )
        for fm in re.finditer(r"([\d,]+)[ \t]*원[ \t]*(?:배송비|택배비|배송료|운송비)", combined_rev_scan):
            val = int(fm.group(1).replace(",", ""))
            if 500 <= val <= 50000:
                shipping_fee = val
                break

    if shipping_fee == 0 and extra_no_thr:
        m_ex = re.search(r"([\d,]+)\s*원", extra_no_thr)
        if m_ex:
            val = int(m_ex.group(1).replace(",", ""))
            if 500 <= val <= 50000:
                shipping_fee = val

    if shipping_fee == 0:
        for pm in re.finditer(r"[\(\[][^\)\]/\r\n]*?(\d[\d,]*)\s*원?\s*/\s*(\d[\d,]*)\s*원?[^\)\]]*[\)\]]", combined_no_thr):
            right_ctx = pm.group(0).split("/", 1)[1]
            if re.search(r"판매가|할인가|특가|정가|정상가|쿠팡가|와우가|실구매가", right_ctx):
                continue
            lv = int(pm.group(1).replace(",", ""))
            rv = int(pm.group(2).replace(",", ""))
            if 1000 <= rv <= 10000 and rv < lv:
                shipping_fee = rv
                break

    has_paid_text = bool(re.search(r"배송비\s*별도|착불|유료배송", combined))

    # 부정/조건부/회원/지역제한/수량/배송비 문구를 모두 걷어낸 뒤에만 순수 무료배송 여부 판정
    uncond_scan = _NEGATED_FREE_SHIPPING_RE.sub(" ", combined)
    uncond_scan = _SHIPPING_THRESHOLD_SPAN_RE.sub(" ", uncond_scan)
    uncond_scan = _WOW_FREE_SPAN_RE.sub(" ", uncond_scan)
    uncond_scan = _MEMBER_FREE_SPAN_RE.sub(" ", uncond_scan)
    uncond_scan = _RESTRICTED_FREE_RE.sub(" ", uncond_scan)
    uncond_scan = _QTY_FREE_RE.sub(" ", uncond_scan)
    uncond_scan = _SHIPPING_FEE_FORWARD_RE.sub(" ", uncond_scan)
    uncond_scan = _SHIPPING_FEE_PAREN_REVERSE_RE.sub(" ", uncond_scan)
    uncond_scan = _SHIPPING_FEE_REVERSE_RE.sub(" ", uncond_scan)
    extra_ship_norm = re.sub(r"\s+", " ", extra_shipping or "").strip()
    has_unconditional_free = (shipping_fee == 0 and not has_paid_text and not has_negated_free) and (
        bool(re.search(r"로켓\s*무료배송|로켓\s*무배|로켓무배|무료배송|무배|배송비\s*무료|배송비\s*0\s*원|/\s*무료(?:\s*[\)\]]|$)|\(\s*무료\s*\)", uncond_scan))
        or extra_ship_norm in ("무료", "0원", "무배")
    )

    # 복합 무료배송 조건 조합 (예: 와우 회원 29,800원 이상 무료배송 / 수도권만 무료배송)
    qualifiers: List[str] = []
    if has_wow_free:
        qualifiers.append("와우 회원")
    elif has_member_free:
        qualifiers.append("회원")
    if restricted_cond:
        qualifiers.append(restricted_cond)
    if qty_cond:
        qualifiers.append(qty_cond)

    free_cond_str = ""
    if threshold_cond and qualifiers:
        free_cond_str = f"{' '.join(qualifiers)} {threshold_cond}"
    elif threshold_cond:
        free_cond_str = threshold_cond
    elif qualifiers:
        if qualifiers == ["와우 회원"] and has_rocket:
            free_cond_str = "로켓와우 무료배송"
        elif qualifiers == ["와우 회원"]:
            free_cond_str = "와우 무료배송"
        else:
            free_cond_str = f"{' '.join(qualifiers)} 무료배송"

    shipping_type = ""
    shipping_condition = ""
    shipping_label = ""
    if shipping_fee > 0 and free_cond_str:
        shipping_type = "conditional"
        shipping_condition = free_cond_str
        short_cond = free_cond_str.replace("무료배송", "무료")
        shipping_label = f"배송비 {shipping_fee:,}원 ({short_cond})"
    elif free_cond_str:
        shipping_type = "conditional"
        shipping_condition = free_cond_str
        shipping_label = (
            f"로켓배송 · {free_cond_str}"
            if (has_rocket and "로켓" not in free_cond_str)
            else free_cond_str
        )
    elif shipping_fee > 0:
        shipping_type = "paid"
        shipping_label = f"로켓배송 · 배송비 {shipping_fee:,}원" if has_rocket else f"배송비 {shipping_fee:,}원"
    elif has_paid_text:
        shipping_type = "paid"
        shipping_label = "착불" if "착불" in combined else "배송비 별도"
    elif has_unconditional_free:
        shipping_type = "rocket_free" if has_rocket else "free"
        shipping_label = "로켓 무료배송" if has_rocket else "무료배송"
    elif has_rocket:
        shipping_type = "rocket"
        shipping_label = "로켓배송"

    return {
        "shipping_fee": shipping_fee,
        "shipping_type": shipping_type,
        "shipping_condition": shipping_condition,
        "shipping_label": shipping_label,
    }


def _extract_clean_won_numbers(text: str, blacklisted_nums: Set[int]) -> List[int]:
    """정상 표기된 원화 금액 추출 (OCR 콤마 뒤 공백 '23, 100원' 포함)"""
    s = _strip_inline_non_prices(_strip_shipping_and_threshold_phrases(text))
    nums: List[int] = []
    for m in re.finditer(r"(?<![+\-\d월])(\d{1,3}(?:\s*,\s*\d{3})+|\d{4,8})\s*원", s):
        raw = re.sub(r"[\s,]", "", m.group(1))
        if raw.isdigit():
            val = int(raw)
            if val >= 1000 and val not in blacklisted_nums:
                nums.append(val)
    return nums


def _extract_strikethrough_candidates(text: str, blacklisted_nums: Set[int]) -> List[int]:
    """
    취소선이 그어진 정상가 후보 추출 (예: '899;eee원' -> 899000, '60;000원' -> 60000).
    ※ 이 후보들은 단독 사용되지 않으며, 반드시 명시된 할인율(%) 또는 쿠폰차액과 수학적으로 일치할 때만 채택됨!
    """
    s = _strip_inline_non_prices(_strip_shipping_and_threshold_phrases(text))
    cands: List[int] = []
    for m in re.finditer(r"(?<![+\-\d월])(\d{1,3}(?:[\s,;]+\d{3}|\s*[;,]\s*[0eEoO]{3})+)\s*원", s):
        cleaned = re.sub(r"[\s,;]", "", m.group(1)).lower().replace("e", "0").replace("o", "0")
        if cleaned.isdigit():
            val = int(cleaned)
            if val >= 1000 and val not in blacklisted_nums:
                cands.append(val)
    return cands


def _coupang_rate_matches(orig_p: int, sale_p: int, written_rate: int) -> bool:
    """쿠팡 공식 할인율(버림 floor) 또는 반올림(round)과 표기된 %가 일치하는지 검증"""
    if orig_p <= sale_p or sale_p <= 0 or not (3 <= written_rate <= 88):
        return False
    floor_r = (orig_p - sale_p) * 100 // orig_p
    round_r = round((orig_p - sale_p) / orig_p * 100)
    return written_rate in (floor_r, round_r) or abs(floor_r - written_rate) <= 1


def parse_pricing_details(title: str, body: str, ocr_text: str = "", extra_shipping: str = "") -> Dict[str, Any]:
    """
    제목, 본문, 이미지 OCR 헤더에서 명시적으로 적힌 정상가·할인가·할인율 및 배송 근거만 정밀 추출.
    [절대 원칙]
    1. 정가 역산(sale_price / (1 - r)) 절대 금지
    2. 임의 쿠폰 차감(sale_price - coupon) 절대 금지
    3. 할부금(월 XXX원~), 적립금(쿠팡캐시), 보험료(AppleCare+/안심케어), 옵션추가금(+XXX원), 배송비·기준금액 원천 배제
    """
    ship_info = parse_shipping_details(title, body, ocr_text, extra_shipping=extra_shipping)
    blacklisted_prices: Set[int] = set()

    # OCR 패스 분리 및 유효 라인 필터링
    ocr_lines_all: List[str] = []
    ocr_lines_raw: List[str] = []
    ocr_lines_enh: List[str] = []
    for img_block in ocr_text.split("===NEXT_IMAGE==="):
        if not img_block.strip():
            continue
        parts = img_block.split("---OCR_ENHANCED---")
        r_lns = [ln.strip() for ln in parts[0].splitlines() if ln.strip()]
        e_lns = [ln.strip() for ln in parts[1].splitlines() if ln.strip()] if len(parts) > 1 else []
        ocr_lines_raw.extend(r_lns)
        ocr_lines_enh.extend(e_lns)
        ocr_lines_all.extend(r_lns)
        ocr_lines_all.extend(e_lns)

    # ==========================================
    # 1단계: 판매가(할인가, sale_price) 확정
    # ==========================================
    sale_price = 0

    # 1-A) 제목에서 가격 최우선 추출 (작성자가 직접 명시한 핫딜가)
    title_clean = _strip_inline_non_prices(_strip_shipping_and_threshold_phrases(title))
    # 제목에 화살표(10,570원 -> 9,370원)가 있으면 뒤쪽 가격이 할인가
    m_title_arrow = re.search(r"([\d,]+)\s*원?\s*(?:에서|->|→|~>)\s*([\d,]+)\s*원", title_clean)
    if m_title_arrow:
        p_to = int(m_title_arrow.group(2).replace(",", ""))
        if p_to >= 1000:
            sale_price = p_to

    if sale_price == 0:
        m_title_lbl = re.search(
            r"(?<!일반)(?:최종가|최종특가|실구매가|와우할인가|와우가|쿠팡가|할인가|판매가|특가|혜택가|가격)\s*[:：\s]*([\d,]+)\s*원",
            title_clean,
        )
        if m_title_lbl:
            val = int(m_title_lbl.group(1).replace(",", ""))
            if val >= 1000 and val not in blacklisted_prices:
                sale_price = val

    if sale_price == 0:
        t_sale_scan = re.sub(
            r"(?:정가|정상가|기존가|소비자가|출고가|시중가|일반판매가)\s*[:：\s]*[\d,]+\s*원",
            " ",
            title_clean,
        )
        t_sale_scan = re.sub(r"[\d,]+\s*원\s*(?:할인|절약|쿠폰|적립)", " ", t_sale_scan)
        t_won = [
            int(p.replace(",", ""))
            for p in re.findall(r"(?<![+\-\d월])([\d,]+)\s*원", t_sale_scan)
            if int(p.replace(",", "")) >= 1000 and int(p.replace(",", "")) not in blacklisted_prices
        ]
        if t_won:
            sale_price = t_won[-1]

    # '원' 글자 없이 (89,890/무료), (14,630/와우무배) 형태로 적힌 제목 가격 지원
    if sale_price == 0:
        t_paren = re.findall(
            r"(?:[\(\[/]\s*|\s)(\d{1,3}(?:,\d{3})+|\d{4,7})\s*(?:/|\)|무료|무배|와우|로켓)",
            title_clean,
        )
        for p in reversed(t_paren):
            val = int(p.replace(",", ""))
            if val >= 1000 and val not in blacklisted_prices:
                sale_price = val
                break

    # 1-B) 본문(body)에서 명시적 판매가 추출
    body_clean = _strip_shipping_and_threshold_phrases(_strip_inline_non_prices(body))
    m_body_arrow = re.search(r"([\d,]+)\s*원?\s*(?:에서|->|→|~>)\s*([\d,]+)\s*원", body_clean)
    if sale_price == 0 and m_body_arrow:
        p_to = int(m_body_arrow.group(2).replace(",", ""))
        if p_to >= 1000 and p_to not in blacklisted_prices:
            sale_price = p_to

    if sale_price == 0:
        for pat in (
            r"(?<!일반)(?:최종가|최종특가|실구매가|와우할인가|와우가|쿠팡가|할인가|판매가|특가|혜택가|가격)\s*[:：\s]*([\d,]+)\s*원",
            r"([\d,]+)\s*원에\s*(?:판매|구매|구입)",
        ):
            m_lbl = re.search(pat, body_clean)
            if m_lbl:
                val = int(m_lbl.group(1).replace(",", ""))
                if val >= 1000 and val not in blacklisted_prices:
                    sale_price = val
                    break

    if sale_price == 0:
        # 정가/할인액/배송비 문구를 제외한 본문 일반 금액
        for line in body_clean.splitlines():
            if _BLACKLIST_LINE_RE.search(line):
                continue
            line_for_sale = re.sub(
                r"(?:정가|정상가|기존가|소비자가|출고가|시중가|일반판매가)\s*[:：\s]*[\d,]+\s*원",
                " ",
                line,
            )
            if re.search(r"(?:쿠폰할인|원\s*할인|체감)", line_for_sale):
                continue
            b_nums = _extract_clean_won_numbers(line_for_sale, blacklisted_prices)
            if b_nums:
                sale_price = b_nums[0]
                break

    # 1-C) 제목·본문에 가격이 없을 때만 OCR 가격 헤더(Raw 패스 우선)에서 추출
    if sale_price == 0:
        # '일반판매가' 또는 '일반할인가'가 나오기 전 상단 대표 가격(와우할인가/즉시할인가) 우선 탐색
        seen_wow_price = 0
        fallback_ocr_prices: List[int] = []
        skip_next_as_normal_label = False
        in_secondary_normal_disc = False

        for ln in ocr_lines_raw + ocr_lines_enh:
            if _BLACKLIST_LINE_RE.search(ln):
                continue
            # '일반판매가' 라벨 다음 줄은 정상가이므로 판매가 후보에서 제외
            if re.search(r"일[반\*\w]?\s*[판킨핀]매가", ln):
                skip_next_as_normal_label = True
                continue
            if re.search(r"일[반\*\w]?\s*할인가", ln):
                in_secondary_normal_disc = True
                continue
            # 쿠폰 할인폭 안내줄(예: '86,000원 웰컴백 쿠폰할인 · 2시간 남음 2,070,740원')은 판매가 줄이 아님
            if re.search(r"쿠폰할인.*남음|구폰할인.*남음|큰할인.*남음", ln) or re.search(r"^\s*[↓十전\(,4]\s*[\d,]+\s*원\s*.*할인", ln):
                continue

            nums = _extract_clean_won_numbers(ln, blacklisted_prices)
            if skip_next_as_normal_label:
                skip_next_as_normal_label = False
                continue
            if not nums:
                continue

            # 한 줄에 '13% 1,984,740원 2,290,000원' 처럼 할인가와 정가가 같이 적힌 경우 작은 쪽이 할인가
            cand = min(nums) if len(nums) >= 2 else nums[0]
            if not in_secondary_normal_disc and seen_wow_price == 0:
                seen_wow_price = cand
                break
            fallback_ocr_prices.append(cand)

        if seen_wow_price >= 1000:
            sale_price = seen_wow_price
        elif fallback_ocr_prices:
            sale_price = fallback_ocr_prices[0]

    # ==========================================
    # 2단계: 정상가(orig_price) 및 할인율(discount_rate) 팩트 검증 추출
    # (오직 실제 적혀 있고 수학적으로 일치하는 경우만 인정, 역산 절대 금지!)
    # ==========================================
    orig_price = 0
    discount_rate = 0

    if sale_price >= 1000:
        # [증명 1] 제목 또는 본문의 명시적 가격 화살표 (예: 10,570원 -> 9,370원)
        for m_arr in (m_title_arrow, m_body_arrow):
            if m_arr:
                p1 = int(m_arr.group(1).replace(",", ""))
                p2 = int(m_arr.group(2).replace(",", ""))
                if p1 > sale_price and abs(p2 - sale_price) <= 100 and p1 not in blacklisted_prices:
                    fl_r = (p1 - sale_price) * 100 // p1
                    if 3 <= fl_r <= 85:
                        orig_price = p1
                        discount_rate = fl_r
                        break

        # [증명 2] 제목 또는 본문의 명시적 정가 키워드 (예: '정가 4,180원에서 22% 할인')
        if orig_price == 0:
            m_orig_txt = re.search(
                r"(?:정가|정상가|기존가|소비자가|출고가)\s*[:\s]*([\d,]+)\s*원",
                f"{title_clean}\n{body_clean}",
            )
            if m_orig_txt:
                p_o = int(m_orig_txt.group(1).replace(",", ""))
                if p_o > sale_price and p_o not in blacklisted_prices:
                    fl_r = (p_o - sale_price) * 100 // p_o
                    if 3 <= fl_r <= 85:
                        orig_price = p_o
                        # 본문/제목에 적힌 %가 일치하면 그 숫자를 그대로 사용
                        m_txt_r = re.search(r"(?<!\d)(\d{1,2})\s*%", f"{title_clean}\n{body_clean}")
                        if m_txt_r and _coupang_rate_matches(p_o, sale_price, int(m_txt_r.group(1))):
                            discount_rate = int(m_txt_r.group(1))
                        else:
                            discount_rate = fl_r

        # OCR에서 명시된 할인율(%) 후보들과 정상가(P_orig > sale_price) 후보 수집
        explicit_rates: List[int] = []
        # 1) 제목/본문 및 OCR의 표준 % 표기 (판매됨/진행/기준/적립 제외)
        for src_txt in [title_clean, body_clean] + ocr_lines_all:
            if re.search(r"적립|석맆|캐시", src_txt):
                continue
            for rm in re.finditer(r"(?<!\d)(\d{1,2})\s*(?:%|％|0/0)(?!\s*(?:판매|진행|기준|적립|남음))", src_txt):
                rv = int(rm.group(1))
                if 3 <= rv <= 85 and rv not in explicit_rates:
                    explicit_rates.append(rv)

        # 2) 3배 업스케일 OCR에서 '%' 기호가 '96'으로 인식된 배지 후보 (반드시 정가 후보와 수학 일치 시에만 작동)
        ocr96_rates: List[int] = []
        for ln in ocr_lines_enh:
            if _BLACKLIST_LINE_RE.search(ln):
                continue
            for rm in re.finditer(r"(?<!\d)(\d{1,2})96(?=\s|$|\d)", ln):
                rv = int(rm.group(1))
                if 5 <= rv <= 85 and rv not in explicit_rates and rv not in ocr96_rates:
                    ocr96_rates.append(rv)

        # OCR 헤더의 정상가 후보(P_orig > sale_price) 수집
        ocr_orig_candidates: List[int] = []
        labeled_normal_prices: List[int] = []
        coupon_delta_pairs: List[Tuple[int, int]] = []  # (delta, explicit_orig)

        expect_normal_next = False
        for ln in ocr_lines_all:
            if _BLACKLIST_LINE_RE.search(ln):
                expect_normal_next = False
                continue

            # '일반판매가' 명시 라벨 감지 ('일반할인가'와 엄격 구분!)
            if re.search(r"일[반\*\w]?\s*[판킨핀]매가", ln):
                expect_normal_next = True
                inline_n = _extract_clean_won_numbers(ln, blacklisted_prices)
                for v in inline_n:
                    if v > sale_price and v not in labeled_normal_prices:
                        labeled_normal_prices.append(v)
                continue

            # 쿠폰 할인폭 + 기준가 동시 표기 줄 감지 (예: '316,000원 와우쿠폰할인 · 20시간 남음 1,115,000원')
            m_cdelta = re.search(r"([\d,]+)\s*원\s*.*?(?:쿠폰할인|구폰할인|큰할인)", ln)
            if m_cdelta:
                delta_v = int(m_cdelta.group(1).replace(",", ""))
                all_in_ln = _extract_clean_won_numbers(ln, blacklisted_prices) + _extract_strikethrough_candidates(ln, blacklisted_prices)
                for v in all_in_ln:
                    if v != delta_v and v > sale_price:
                        coupon_delta_pairs.append((delta_v, v))
                        if v not in ocr_orig_candidates:
                            ocr_orig_candidates.append(v)
                expect_normal_next = False
                continue

            line_nums = _extract_clean_won_numbers(ln, blacklisted_prices)
            strike_nums = _extract_strikethrough_candidates(ln, blacklisted_prices)

            if expect_normal_next and line_nums:
                for v in line_nums:
                    if v > sale_price and v not in labeled_normal_prices:
                        labeled_normal_prices.append(v)
                expect_normal_next = False

            for v in line_nums + strike_nums:
                if v > sale_price and v not in ocr_orig_candidates:
                    ocr_orig_candidates.append(v)

        # [증명 3] 명시적 할인율(%) 배지 + 명시적 정상가(P_orig) 수학적 일치 검증 (가장 정확)
        if orig_price == 0 and ocr_orig_candidates:
            # 가장 큰 정상가(최초 정가, 예: 2,290,000원 vs 중간 쿠폰기준가 2,070,740원)부터 표준 %와 대조
            for r_cand in explicit_rates:
                for p_cand in sorted(ocr_orig_candidates, reverse=True):
                    if _coupang_rate_matches(p_cand, sale_price, r_cand):
                        orig_price = p_cand
                        discount_rate = r_cand
                        break
                if orig_price > 0:
                    break

        if orig_price == 0 and ocr_orig_candidates and ocr96_rates:
            for r_cand in ocr96_rates:
                for p_cand in sorted(ocr_orig_candidates, reverse=True):
                    if _coupang_rate_matches(p_cand, sale_price, r_cand):
                        orig_price = p_cand
                        discount_rate = r_cand
                        break
                if orig_price > 0:
                    break

        # [증명 4] 명시적 '일반판매가' 라벨 가격 (예: 레노버 일반판매가 899,000원)
        if orig_price == 0 and labeled_normal_prices:
            p_norm = max(labeled_normal_prices)
            fl_r = (p_norm - sale_price) * 100 // p_norm
            if 3 <= fl_r <= 85:
                orig_price = p_norm
                discount_rate = fl_r

        # [증명 5] 명시적 쿠폰할인액(D) + 명시적 정상가(P_orig)가 1원 단위까지 정확히 일치 (sale_price + D == P_orig)
        if orig_price == 0 and coupon_delta_pairs:
            for delta_v, p_orig_v in coupon_delta_pairs:
                if abs((sale_price + delta_v) - p_orig_v) <= 10:
                    fl_r = (p_orig_v - sale_price) * 100 // p_orig_v
                    if 3 <= fl_r <= 85:
                        orig_price = p_orig_v
                        discount_rate = fl_r
                        break

    # 최종 일관성 방어선: 정가와 할인가가 모두 검증되지 않았으면 둘 다 0으로 초기화 (절대 숫자 날조 금지)
    if orig_price > sale_price >= 1000:
        floor_rate = (orig_price - sale_price) * 100 // orig_price
        if not _coupang_rate_matches(orig_price, sale_price, discount_rate):
            discount_rate = floor_rate
        if not (3 <= discount_rate <= 85):
            orig_price = 0
            discount_rate = 0
    else:
        orig_price = 0
        discount_rate = 0

    final_price = sale_price

    # 단가 계산 (깔끔히 정제된 상품명 기준)
    unit_info = ""
    if final_price >= 1000:
        raw_unit = compute_unit_pricing(clean_display_title(title), final_price)
        if raw_unit:
            unit_info = raw_unit.replace("• <b>단가계산</b>: ", "").replace("\n", "").strip()

    # 커뮤니티 반응 (URL/쿠파스 링크, 테이블 헤더, 단순 감상평 원천 배제)
    community_reactions: List[str] = []
    for line in body.splitlines():
        # 줄 내부에 포함된 모든 URL 및 도메인 주소 완전 제거
        ln = re.sub(r"https?://\S+", "", line)
        ln = re.sub(r"www\.\S+", "", ln)
        ln = re.sub(r"\b[a-zA-Z0-9.-]+\.(?:com|co\.kr|kr|live|me|gl|ly)(?:/\S*)?", "", ln, flags=re.IGNORECASE)
        ln = re.sub(r"(?:구매\s*링크|상품\s*링크|링크)\s*[:：\-]?\s*$", "", ln).strip()
        if not ln or len(ln) < 5:
            continue
        if any(
            bad in ln.lower()
            for bad in [
                "맛있", "맛나", "좋네", "샀습", "추천합", "감사", "양도 많고",
                "구매링크", "상품링크", "링크", "쇼핑몰", "쿠파스", "파트너스", "수수료", "일정액",
                "coupang", "unsafelink", "http",
            ]
        ):
            continue
        if any(kw in ln for kw in ["최저가", "역대", "마트", "당근", "가성비", "단가", "품절", "싼듯", "저렴"]):
            community_reactions.append(ln)

    reaction_text = ""
    if community_reactions:
        combined = " / ".join(community_reactions[:2])
        if len(combined) > 70:
            combined = combined[:67] + "..."
        reaction_text = combined

    return {
        "orig_price": orig_price,
        "sale_price": sale_price,
        "final_price": final_price,
        "discount_rate": discount_rate,
        "unit_info": unit_info,
        "reaction_text": reaction_text,
        "shipping_fee": int(ship_info.get("shipping_fee", 0) or 0),
        "shipping_type": str(ship_info.get("shipping_type") or ""),
        "shipping_condition": str(ship_info.get("shipping_condition") or ""),
        "shipping_label": str(ship_info.get("shipping_label") or ""),
    }


def send_telegram(
    chat_id: str,
    text: str,
    photo_url: Optional[str] = None,
    button_text: Optional[str] = None,
    button_url: Optional[str] = None,
    button_style: Optional[str] = None,
    inline_keyboard: Optional[List[List[Dict[str, Any]]]] = None,
    disable_notification: bool = False,
    return_detail: bool = False,
) -> Any:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "parse_mode": "HTML",
    }
    if disable_notification:
        payload["disable_notification"] = True
    if inline_keyboard:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
    elif button_text and button_url:
        btn: Dict[str, Any] = {"text": button_text, "url": button_url}
        if button_style:
            btn["style"] = button_style
        payload["reply_markup"] = {
            "inline_keyboard": [[btn]]
        }

    # 1. 사진이 있는 경우 sendPhoto 시도
    if photo_url:
        try:
            p_payload = dict(payload)
            p_payload["photo"] = photo_url
            p_payload["caption"] = text
            res = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", json=p_payload, timeout=10
            ).json()
            if res.get("ok"):
                return (True, 0, "") if return_detail else True
            err_code = int(res.get("error_code", 0) or 0)
            err_desc = str(res.get("description") or "")
            low_desc = err_desc.lower()
            if err_code == 403 or "blocked" in low_desc or "deactivated" in low_desc or "chat not found" in low_desc:
                return (False, err_code, err_desc) if return_detail else False
        except Exception as e:
            logger.warning(f"사진 전송 실패, 텍스트 전송으로 자동 전환 ({photo_url[:50]}...): {e}")

    # 2. 사진 없거나 사진 전송 실패 시 일반 텍스트 메시지로 자동 안전 전송
    try:
        t_payload = dict(payload)
        t_payload["text"] = text
        t_payload["link_preview_options"] = {"is_disabled": True}
        res = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=t_payload, timeout=10
        ).json()
        ok = bool(res.get("ok", False))
        if return_detail:
            return ok, int(res.get("error_code", 0) or 0), str(res.get("description") or "")
        return ok
    except Exception as e:
        logger.error(f"텔레그램 메시지 전송 최종 실패: {e}")
        return (False, 0, str(e)) if return_detail else False


def send_telegram_photo_bytes(
    chat_id: str,
    png_bytes: bytes,
    caption: str,
    inline_keyboard: Optional[List[List[Dict[str, Any]]]] = None,
) -> bool:
    """메모리 내 렌더링된 PNG 전단지 이미지를 멀티파트 sendPhoto로 전송"""
    data: Dict[str, Any] = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if inline_keyboard:
        data["reply_markup"] = json.dumps({"inline_keyboard": inline_keyboard}, ensure_ascii=False)
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data=data,
            files={"photo": ("hotdeal_flyer.png", png_bytes, "image/png")},
            timeout=15,
        ).json()
        if res.get("ok"):
            return True
        logger.warning(f"전단지 sendPhoto 응답 실패: {res.get('description') or res}")
    except Exception as e:
        logger.error(f"전단지 sendPhoto 전송 예외: {e}")
    return False


# ==========================================
# 1. 루리웹 핫딜 수집기
# ==========================================
def collect_ruliweb_deals(max_pages: int = 3) -> List[Dict[str, Any]]:
    """루리웹 핫딜 게시판 일반 스캔 및 '쿠팡' 검색 결과 듀얼 수집"""
    deals = []
    urls = [
        "https://bbs.ruliweb.com/market/board/1020?search_type=subject&search_key=%EC%BF%A0%ED%8C%A1",
    ]
    for page in range(1, max_pages + 1):
        urls.append(f"https://bbs.ruliweb.com/market/board/1020?page={page}")

    for url in urls:
        try:
            res = requests.get(url, headers=HEADERS, timeout=8)
            if res.status_code != 200:
                _record_source_http_block("ruliweb", res)
                if res.status_code in (403, 429):
                    break
                continue

            soup = BeautifulSoup(res.text, "html.parser")
            for row in soup.select("tr.table_body"):
                if row.select_one("span.strike, del"):
                    continue
                s = row.select_one("a.subject_link, a.deco")
                if not s:
                    continue
                raw_t = " ".join(s.text.split())
                title = re.sub(r"\s*\(\d+\)\s*$", "", raw_t).strip()
                if "쿠팡" not in title and "coupang" not in title.lower():
                    continue
                if is_closed_deal(title):
                    continue

                link = s.get("href", "")
                m = re.search(r"/read/(\d+)", link)
                pid = f"ruliweb_{m.group(1)}" if m else link

                votes = 0
                hits = 0
                comments = 0
                rec_el = row.select_one("td.recomd")
                if rec_el:
                    m_v = re.search(r"-?\d+", rec_el.get_text(strip=True))
                    if m_v:
                        votes = int(m_v.group(0))
                hit_el = row.select_one("td.hit")
                if hit_el:
                    m_h = re.search(r"\d+", hit_el.get_text(strip=True).replace(",", ""))
                    if m_h:
                        hits = int(m_h.group(0))
                nr_el = row.select_one("span.num_reply, a.num_reply")
                if nr_el:
                    m_c = re.search(r"\d+", nr_el.get_text(strip=True))
                    if m_c:
                        comments = int(m_c.group(0))
                elif re.search(r"\((\d+)\)\s*$", raw_t):
                    comments = int(re.search(r"\((\d+)\)\s*$", raw_t).group(1))

                if not any(d["id"] == pid for d in deals):
                    deals.append({
                        "id": pid, "title": title, "link": link,
                        "votes": votes, "hits": hits, "comments": comments, "downvotes": 0,
                    })
        except Exception as e:
            logger.error(f"루리웹 수집 오류 ({url}): {e}")

    return deals


def parse_ruliweb_detail(url: str) -> Optional[Dict[str, Any]]:
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        if res.status_code != 200:
            _record_source_http_block("ruliweb", res)
            if _is_transient_http_status(res.status_code):
                logger.warning(f"⏳ 루리웹 상세 일시 오류 (HTTP {res.status_code}): {url}")
                return {"transient_error": True, "error_reason": f"http_{res.status_code}"}
            return None
        soup = BeautifulSoup(res.text, "html.parser")

        subj = soup.select_one("span.subject_inner_text")
        if subj and is_closed_deal(subj.text):
            return None

        cp_url = None
        src = soup.select_one("div.source_url a")
        if src and "coupang.com" in src.get("href", ""):
            cp_url = src["href"]
            m = re.search(r"ol=([^&]+)", cp_url)
            if m:
                import urllib.parse
                cp_url = urllib.parse.unquote(m.group(1))

        if not cp_url:
            all_cp = re.findall(r"https?://(?:www\.|m\.)?coupang\.com/vp/products/\d+[^\s\"\'<>]*", res.text)
            if all_cp:
                cp_url = all_cp[0]

        if not cp_url:
            return None

        body_text = ""
        img_urls: List[str] = []
        vc = soup.select_one("div.view_content")
        if vc:
            body_text = vc.get_text("\n", strip=True)
            for img in vc.select("img"):
                iu = img.get("src")
                if iu and not iu.endswith(".svg"):
                    if iu.startswith("//"):
                        iu = "https:" + iu
                    if iu not in img_urls:
                        img_urls.append(iu)

        return {
            "coupang_url": cp_url,
            "body_text": body_text,
            "img_url": img_urls[0] if img_urls else None,
            "ocr_img_urls": img_urls[:2],
            "comment_texts": _collect_comment_texts(
                soup, ".comment_view .text_wrapper .text, .comment_element .text", limit=15
            ),
        }
    except requests.Timeout as e:
        logger.warning(f"⏳ 루리웹 상세 타임아웃 ({url}): {e}")
        return {"transient_error": True, "error_reason": "timeout"}
    except requests.RequestException as e:
        logger.warning(f"⏳ 루리웹 상세 네트워크 일시 오류 ({url}): {e}")
        return {"transient_error": True, "error_reason": "request_error"}
    except Exception as e:
        logger.error(f"루리웹 상세 파싱 오류 ({url}): {e}")
        return None


# ==========================================
# 2. 뽐뿌 핫딜 수집기
# ==========================================
def collect_ppomppu_deals(max_pages: int = 4) -> List[Dict[str, Any]]:
    """뽐뿌 핫딜 게시판 일반 스캔 및 '쿠팡' 검색 결과 듀얼 수집"""
    deals = []
    import urllib.parse
    kw = urllib.parse.quote("쿠팡", encoding="euc-kr")
    urls = [
        f"https://www.ppomppu.co.kr/zboard/zboard.php?id=ppomppu&keyword={kw}&page=1",
    ]
    for page in range(1, max_pages + 1):
        urls.append(f"https://www.ppomppu.co.kr/zboard/zboard.php?id=ppomppu&page={page}")

    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=6)
            if r.status_code != 200:
                _record_source_http_block("ppomppu", r)
                if r.status_code in (403, 429):
                    break
                continue
            r.encoding = "euc-kr"
            soup = BeautifulSoup(r.text, "html.parser")
            rows = soup.select("tr.baseList")
            if rows:
                for tr in rows:
                    a = tr.select_one("a.baseList-title")
                    if not a or not a.text.strip():
                        continue
                    href = a.get("href", "")
                    if "view.php?id=ppomppu&" not in href:
                        continue
                    if tr.select_one("span.baseList-end, img[src*='end'], strike, del, span[style*='line-through']") or a.select_one("strike, del, span[style*='line-through']"):
                        continue
                    title = " ".join(a.text.split())
                    if is_closed_deal(title):
                        continue
                    if "쿠팡" not in title and "coupang" not in title.lower():
                        continue
                    m = re.search(r"no=(\d+)", href)
                    if not m:
                        continue
                    deal_id = f"ppomppu_{m.group(1)}"
                    full_url = f"https://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&no={m.group(1)}"

                    votes = 0
                    downvotes = 0
                    rec_el = tr.select_one("td.baseList-rec")
                    if rec_el:
                        rec_nums = re.findall(r"\d+", rec_el.get_text(" ", strip=True))
                        if len(rec_nums) >= 2:
                            votes = int(rec_nums[0])
                            downvotes = int(rec_nums[1])
                        elif len(rec_nums) == 1:
                            votes = int(rec_nums[0])
                    comments = 0
                    c_el = tr.select_one("span.baseList-c")
                    if c_el:
                        m_c = re.search(r"\d+", c_el.get_text(strip=True))
                        if m_c:
                            comments = int(m_c.group(0))

                    if not any(d["id"] == deal_id for d in deals):
                        deals.append({
                            "id": deal_id, "title": title, "link": full_url,
                            "votes": votes, "downvotes": downvotes, "comments": comments, "hits": 0,
                        })
            else:
                for a in soup.select("a"):
                    href = a.get("href", "")
                    if "view.php?id=ppomppu&" in href and a.text.strip():
                        if a.select_one("strike, del, span[style*='line-through']"):
                            continue
                        title = " ".join(a.text.split())
                        if is_closed_deal(title):
                            continue
                        if "쿠팡" in title or "coupang" in title.lower():
                            m = re.search(r"no=(\d+)", href)
                            if not m:
                                continue
                            deal_id = f"ppomppu_{m.group(1)}"
                            full_url = f"https://www.ppomppu.co.kr/zboard/view.php?id=ppomppu&no={m.group(1)}"
                            if not any(d["id"] == deal_id for d in deals):
                                deals.append({"id": deal_id, "title": title, "link": full_url, "votes": 0, "downvotes": 0, "comments": 0, "hits": 0})
            time.sleep(0.2)
        except Exception as e:
            logger.error(f"뽐뿌 수집 오류 ({url}): {e}")
    return deals


def parse_ppomppu_detail(url: str) -> Optional[Dict[str, Any]]:
    try:
        time.sleep(0.25)
        res = requests.get(url, headers=HEADERS, timeout=7)
        if res.status_code != 200:
            _record_source_http_block("ppomppu", res)
            if _is_transient_http_status(res.status_code):
                logger.warning(f"⏳ 뽐뿌 상세 일시 오류 (HTTP {res.status_code}): {url}")
                return {"transient_error": True, "error_reason": f"http_{res.status_code}"}
            return None
        res.encoding = "euc-kr"
        soup = BeautifulSoup(res.text, "html.parser")

        # 상단 제목 종료 여부 확인
        top_box = soup.select_one("div.sub-top-text-box")
        if top_box and is_closed_deal(top_box.text[:200]):
            return None

        all_cp = re.findall(r"https?://(?:www\.|m\.)?coupang\.com/vp/products/\d+[^\s\"\'<>]*", res.text)
        if not all_cp:
            all_cp = re.findall(r"https?://link\.coupang\.com/[a-zA-Z0-9/_\-\?=&]+", res.text)

        if not all_cp:
            return None
        cp_url = all_cp[0]

        if "link.coupang.com" in cp_url:
            clean_cp, _ = clean_coupang_url(cp_url)
            if "coupang.com" in clean_cp:
                cp_url = clean_cp

        body_text = ""
        content_el = soup.select_one("td.board-contents, td.han")
        if content_el:
            body_text = content_el.get_text("\n", strip=True)

        # 본문 첨부 캡처 이미지(가격표 포함) 우선 수집 + 썸네일 보조
        content_imgs: List[str] = []
        if content_el:
            for img in content_el.select("img"):
                src = img.get("src")
                if src and not src.endswith(".svg") and "icon" not in src.lower():
                    iu = ("https:" + src) if src.startswith("//") else src
                    if iu.startswith("http") and iu not in content_imgs:
                        content_imgs.append(iu)

        thumb_candidates = re.findall(
            r"https?://[^\s\"\'<>]*(?:coupangcdn|thumbnail)[^\s\"\'<>]*\.(?:jpg|jpeg|png|webp)",
            res.text,
        )
        photo_url = content_imgs[0] if content_imgs else (thumb_candidates[0] if thumb_candidates else None)

        return {
            "coupang_url": cp_url,
            "body_text": body_text,
            "img_url": photo_url,
            "ocr_img_urls": content_imgs[:2],
            "comment_texts": [],
        }
    except requests.Timeout as e:
        logger.warning(f"⏳ 뽐뿌 상세 타임아웃 ({url}): {e}")
        return {"transient_error": True, "error_reason": "timeout"}
    except requests.RequestException as e:
        logger.warning(f"⏳ 뽐뿌 상세 네트워크 일시 오류 ({url}): {e}")
        return {"transient_error": True, "error_reason": "request_error"}
    except Exception as e:
        logger.error(f"뽐뿌 상세 파싱 오류 ({url}): {e}")
        return None


# ==========================================
# 3. 아카라이브 핫딜 수집기 (메인 게시판 스토어 태그 + 검색 듀얼 수집)
# ==========================================
def collect_arcalive_deals() -> List[Dict[str, Any]]:
    """아카라이브 핫딜 채널 메인 게시판(쿠팡 스토어 태그) 및 검색 결과 듀얼 수집"""
    deals = []
    import urllib.parse
    kw = urllib.parse.quote("쿠팡")

    # 1) 메인 게시판 1~4페이지 스캔 (제목에 '쿠팡' 없어도 스토어 태그가 쿠팡인 딜 전량 포착)
    blocked = False
    for page in (1, 2, 3, 4):
        url = f"https://arca.live/b/hotdeal?p={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=8)
            if r.status_code != 200:
                _record_source_http_block("arca", r)
                if r.status_code in (403, 429):
                    blocked = True
                    break
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for row in soup.select("div.vrow.hybrid"):
                if "notice" in (row.get("class") or []) or "deal-close" in (row.get("class") or []):
                    continue
                if row.select_one(".deal-close, .close, del, s"):
                    continue
                store_el = row.select_one(".deal-store")
                store_txt = store_el.get_text(strip=True) if store_el else ""
                a = row.select_one("a.title, a.hybrid-title")
                if not a:
                    continue
                raw_t = " ".join(a.get_text(" ", strip=True).split())
                if "쿠팡" not in store_txt and "쿠팡" not in raw_t and "coupang" not in raw_t.lower():
                    continue
                m = re.search(r"/b/hotdeal/(\d+)", a.get("href", ""))
                if not m:
                    continue

                comments = 0
                m_c = re.search(r"\[(\d+)\]\s*$", raw_t)
                if m_c:
                    comments = int(m_c.group(1))
                title = re.sub(r"\s*\[\d+\]$", "", raw_t).strip()
                if not title or is_closed_deal(title):
                    continue

                votes = 0
                rate_el = row.select_one(".col-rate")
                if rate_el:
                    m_v = re.search(r"-?\d+", rate_el.get_text(strip=True))
                    if m_v:
                        votes = int(m_v.group(0))

                deal_id = f"arca_{m.group(1)}"
                full_url = f"https://arca.live/b/hotdeal/{m.group(1)}"
                if not any(d["id"] == deal_id for d in deals):
                    deals.append({
                        "id": deal_id, "title": title, "link": full_url,
                        "votes": votes, "downvotes": 0, "comments": comments, "hits": 0,
                    })
        except Exception as e:
            logger.error(f"아카라이브 메인 수집 오류 (p={page}): {e}")

    # 2) 키워드 검색 보조 수집
    if blocked:
        return deals
    search_url = f"https://arca.live/b/hotdeal?target=all&keyword={kw}"
    try:
        r = requests.get(search_url, headers=HEADERS, timeout=8)
        if r.status_code != 200:
            _record_source_http_block("arca", r)
        elif r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a.title, div.vrow-top a.hybrid-title, a.vrow"):
                if "notice" in (a.get("class") or []):
                    continue
                href = a.get("href", "")
                m = re.search(r"/b/hotdeal/(\d+)", href)
                if not m:
                    continue
                if a.select_one(".deal-close, del, s"):
                    continue
                raw_t = " ".join(a.text.split())
                comments = 0
                m_c = re.search(r"\[(\d+)\]\s*$", raw_t)
                if m_c:
                    comments = int(m_c.group(1))
                title = re.sub(r"\s*\[\d+\]$", "", raw_t).strip()
                if not title or is_closed_deal(title):
                    continue
                deal_id = f"arca_{m.group(1)}"
                full_url = f"https://arca.live/b/hotdeal/{m.group(1)}"
                if not any(d["id"] == deal_id for d in deals):
                    deals.append({
                        "id": deal_id, "title": title, "link": full_url,
                        "votes": 0, "downvotes": 0, "comments": comments, "hits": 0,
                    })
    except Exception as e:
        logger.error(f"아카라이브 검색 수집 오류: {e}")

    return deals


def parse_arcalive_detail(url: str) -> Optional[Dict[str, Any]]:
    """아카라이브 핫딜 본문 파싱 (종료 딜 필터링 + 전용 테이블 정밀 추출)"""
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        if res.status_code != 200:
            _record_source_http_block("arca", res)
            if _is_transient_http_status(res.status_code):
                logger.warning(f"⏳ 아카라이브 상세 일시 오류 (HTTP {res.status_code}): {url}")
                return {"transient_error": True, "error_reason": f"http_{res.status_code}"}
            return None
        soup = BeautifulSoup(res.text, "html.parser")

        title_row = soup.select_one("div.title-row")
        if title_row:
            if title_row.select_one(".deal-close, del, s") or is_closed_deal(title_row.text):
                return None

        cp_url = None
        extracted_title = ""
        extracted_price = 0
        extracted_shipping = ""

        for tr in soup.select("table.article-options tr, table.article-info tr"):
            th = tr.select_one("th, td:first-child")
            td = tr.select_one("td:last-child")
            if th and td:
                lbl = th.text.strip()
                val = td.text.strip()
                if "링크" in lbl:
                    a = td.select_one("a")
                    if a and "coupang.com" in a.get("href", ""):
                        h = a["href"]
                        if "unsafelink.com/" in h:
                            h = h.split("unsafelink.com/")[-1]
                        cp_url = h
                elif "상품명" in lbl and val:
                    extracted_title = val
                elif "배송" in lbl and val:
                    extracted_shipping = val
                elif "가격" in lbl and val:
                    clean_val = _strip_shipping_and_threshold_phrases(val)
                    m = re.search(r"([\d,]+)\s*원?", clean_val)
                    if m:
                        p_val = int(m.group(1).replace(",", ""))
                        if p_val >= 1000:
                            extracted_price = p_val

        if is_closed_deal(extracted_title):
            return None

        if extracted_price == 0 and any(kw in extracted_title for kw in ["쿠폰 예고", "선착쿠폰", "이벤트", "출석체크"]):
            return None

        if not cp_url:
            for a in soup.select("table.article-info a, div.article-body a, a"):
                href = a.get("href", "")
                if "coupang.com" in href:
                    if "unsafelink.com/" in href:
                        href = href.split("unsafelink.com/")[-1]
                    cp_url = href
                    break

        if not cp_url:
            all_cp = re.findall(r"https?://(?:www\.|m\.)?coupang\.com/vp/products/\d+[^\s\"\'<>]*", res.text)
            if all_cp:
                cp_url = all_cp[0]

        if not cp_url:
            return None

        img_urls: List[str] = []
        body_text = ""
        body_el = soup.select_one("div.article-body")
        if body_el:
            for img in body_el.select("img"):
                iu = img.get("src")
                if iu and not iu.endswith(".svg") and "twemoji" not in iu:
                    if iu.startswith("//"):
                        iu = "https:" + iu
                    if iu not in img_urls:
                        img_urls.append(iu)
            # 테이블 텍스트 중복 제거 후 순수 본문만 추출
            for tbl in body_el.select("table"):
                tbl.decompose()
            body_text = body_el.get_text("\n", strip=True)

        return {
            "coupang_url": cp_url,
            "body_text": body_text,
            "img_url": img_urls[0] if img_urls else None,
            "ocr_img_urls": img_urls[:2],
            "extracted_title": extracted_title,
            "extracted_price": extracted_price,
            "extracted_shipping": extracted_shipping,
            "comment_texts": _collect_comment_texts(
                soup, ".comment-item .message .text", limit=15
            ),
        }
    except requests.Timeout as e:
        logger.warning(f"⏳ 아카라이브 상세 타임아웃 ({url}): {e}")
        return {"transient_error": True, "error_reason": "timeout"}
    except requests.RequestException as e:
        logger.warning(f"⏳ 아카라이브 상세 네트워크 일시 오류 ({url}): {e}")
        return {"transient_error": True, "error_reason": "request_error"}
    except Exception as e:
        logger.error(f"아카라이브 상세 파싱 오류 ({url}): {e}")
        return None


# ==========================================
# 3-B. 딜바다 국내핫딜 수집기 및 출처 쿨다운 헬퍼
# ==========================================
def get_price_db() -> PriceHistoryDB:
    """현재 설정된 PRICE_DB_FILE 경로에 대한 SQLite DB 인스턴스 반환"""
    db_path = os.path.abspath(PRICE_DB_FILE)
    db = _PRICE_DB_CACHE.get(db_path)
    if db is None:
        db = PriceHistoryDB(db_path)
        _PRICE_DB_CACHE[db_path] = db
    return db


def _resolve_sub_id(source_name: str, track: str = "A") -> str:
    base = SOURCE_SUB_ID_MAP.get(source_name, f"tg{re.sub(r'[^a-zA-Z0-9]', '', source_name)}")
    return f"{base}np" if str(track).upper() == "B" else base


def _parse_retry_after_minutes(res: Any, default_minutes: int = SOURCE_429_DEFAULT_COOLDOWN_MINUTES) -> int:
    try:
        raw = str((getattr(res, "headers", None) or {}).get("Retry-After") or "").strip()
        if raw.isdigit():
            secs = max(60, int(raw))
            return max(1, (secs + 59) // 60)
    except Exception:
        pass
    return default_minutes


def _record_source_http_block(source_name: str, res: Any) -> None:
    status = int(getattr(res, "status_code", 0) or 0)
    if status == 429:
        mins = _parse_retry_after_minutes(res)
        get_price_db().set_source_cooldown(source_name, 429, mins, reason="http_429")
        logger.warning(f"⏸️ [{source_name}] HTTP 429 수신 — {mins}분 쿨다운 설정")
    elif status == 403:
        get_price_db().set_source_cooldown(source_name, 403, SOURCE_403_COOLDOWN_MINUTES, reason="http_403")
        logger.warning(f"🛑 [{source_name}] HTTP 403 차단 감지 — {SOURCE_403_COOLDOWN_MINUTES}분 쿨다운 설정")


def _extract_coupang_url_from_href_or_text(raw: str) -> Optional[str]:
    s = (raw or "").strip()
    if not s:
        return None
    if "unsafelink.com/" in s:
        s = s.split("unsafelink.com/")[-1]
    if "coupang.com" in s.lower() and not ("tu=" in s and "linkprice" in s.lower()):
        m = re.search(r"https?://(?:www\.|m\.|link\.)?coupang\.com[^\s\"'<>]*", s, re.IGNORECASE)
        if m:
            return m.group(0)
    try:
        qs = parse_qs(urlparse(s).query or "")
        for key in ("tu", "url", "target"):
            for val in qs.get(key, []):
                decoded = unquote(val)
                if "coupang.com" in decoded.lower():
                    return decoded
    except Exception:
        pass
    return None


def collect_dealbada_deals() -> List[Dict[str, Any]]:
    """딜바다 국내핫딜 게시판 1~2페이지 쿠팡 핫딜 수집 (빈 썸네일 앵커 우회)"""
    deals: List[Dict[str, Any]] = []
    for page in (1, 2):
        url = f"https://www.dealbada.com/bbs/board.php?bo_table=deal_domestic&page={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                _record_source_http_block("dealbada", r)
                if r.status_code in (403, 429):
                    break
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for row in soup.select("table tbody tr"):
                row_cls = row.get("class") or []
                if "bo_notice" in row_cls or "notice" in row_cls:
                    continue
                if row.select_one(".deal_end, del, s, strike"):
                    continue

                subj_a = row.select_one("td.td_subject a[href*='wr_id=']")
                if not subj_a:
                    for cand_a in row.select("a[href*='bo_table=deal_domestic'][href*='wr_id=']"):
                        if cand_a.get_text(strip=True):
                            subj_a = cand_a
                            break
                if not subj_a:
                    continue
                href = str(subj_a.get("href") or "").strip()
                m_id = re.search(r"wr_id=(\d+)", href)
                if not m_id:
                    continue

                row_txt = " ".join(row.get_text(" ", strip=True).split())
                raw_t = " ".join(subj_a.get_text(" ", strip=True).split())
                if not raw_t:
                    continue
                if "쿠팡" not in raw_t and "쿠팡" not in row_txt and "coupang" not in row_txt.lower():
                    continue

                comments = 0
                cmt_el = row.select_one(".cnt_cmt")
                if cmt_el:
                    m_c = re.search(r"\d+", cmt_el.get_text(strip=True))
                    if m_c:
                        comments = int(m_c.group(0))

                title = re.sub(r"\s*\[\d+\]\s*$", "", raw_t).strip()
                if not title or is_closed_deal(title):
                    continue

                votes = 0
                good_el = row.select_one("td.td_num:last-of-type, .bo_v_good")
                if good_el:
                    m_v = re.search(r"\d+", good_el.get_text(strip=True))
                    if m_v:
                        votes = int(m_v.group(0))

                wr_id = m_id.group(1)
                deal_id = f"dealbada_{wr_id}"
                full_url = f"https://www.dealbada.com/bbs/board.php?bo_table=deal_domestic&wr_id={wr_id}"
                if not any(d["id"] == deal_id for d in deals):
                    deals.append({
                        "id": deal_id,
                        "title": title,
                        "link": full_url,
                        "votes": votes,
                        "downvotes": 0,
                        "comments": comments,
                        "hits": 0,
                    })
        except Exception as e:
            logger.error(f"딜바다 수집 오류 (page={page}): {e}")
    return deals


def parse_dealbada_detail(url: str) -> Optional[Dict[str, Any]]:
    """딜바다 국내핫딜 본문 파싱 (#bo_v_link 직접 쿠팡 URL + linkprice tu 디코딩)"""
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        if res.status_code != 200:
            _record_source_http_block("dealbada", res)
            if _is_transient_http_status(res.status_code):
                logger.warning(f"⏳ 딜바다 상세 일시 오류 (HTTP {res.status_code}): {url}")
                return {"transient_error": True, "error_reason": f"http_{res.status_code}"}
            return None

        soup = BeautifulSoup(res.text, "html.parser")
        title_el = soup.select_one("#bo_v_title, .bo_v_tit")
        extracted_title = " ".join(title_el.get_text(" ", strip=True).split()) if title_el else ""
        if title_el and (title_el.select_one(".deal_end, del, s, strike") or is_closed_deal(extracted_title)):
            return None

        cp_url: Optional[str] = None
        for a in soup.select("#bo_v_link a, #bo_v_atc a, #bo_v_con a, a"):
            cand = _extract_coupang_url_from_href_or_text(a.get("href", ""))
            if not cand:
                cand = _extract_coupang_url_from_href_or_text(a.get_text(" ", strip=True))
            if cand:
                cp_url = cand
                break

        if not cp_url:
            m_raw = re.search(r"https?://(?:www\.|m\.|link\.)?coupang\.com/(?:vp/products/\d+|re/[^\s\"'<>]+|go/[^\s\"'<>]+)[^\s\"'<>]*", res.text)
            if m_raw:
                cp_url = m_raw.group(0)

        if not cp_url:
            return None

        body_el = soup.select_one("#bo_v_con") or soup.select_one("#bo_v_atc")
        img_urls: List[str] = []
        body_text = ""
        if body_el:
            for img in body_el.select("img"):
                iu = str(img.get("src") or "").strip()
                if not iu or iu.endswith(".svg") or "emoticon" in iu.lower() or "twemoji" in iu.lower():
                    continue
                if iu.startswith("//"):
                    iu = "https:" + iu
                elif iu.startswith("/"):
                    iu = "https://www.dealbada.com" + iu
                if iu not in img_urls:
                    img_urls.append(iu)
            body_text = body_el.get_text("\n", strip=True)

        return {
            "coupang_url": cp_url,
            "body_text": body_text,
            "img_url": img_urls[0] if img_urls else None,
            "ocr_img_urls": img_urls[:2],
            "extracted_title": extracted_title,
            "comment_texts": _collect_comment_texts(
                soup, "#bo_vc article p", limit=15
            ),
        }
    except requests.Timeout as e:
        logger.warning(f"⏳ 딜바다 상세 타임아웃 ({url}): {e}")
        return {"transient_error": True, "error_reason": "timeout"}
    except requests.RequestException as e:
        logger.warning(f"⏳ 딜바다 상세 네트워크 일시 오류 ({url}): {e}")
        return {"transient_error": True, "error_reason": "request_error"}
    except Exception as e:
        logger.error(f"딜바다 상세 파싱 오류 ({url}): {e}")
        return None


# ==========================================
# 4. 핫딜 송출 엔진
# ==========================================
def get_discount_badge(disc_rate: int) -> Tuple[str, str]:
    """할인율 구간별 강조 이모지 및 문구 반환"""
    if disc_rate >= 60:
        return "🚨", "폭탄할인"
    if disc_rate >= 50:
        return "💥", "반값특가"
    if disc_rate >= STRONG_HOTDEAL_DISCOUNT_RATE:
        return "⚡", "파격할인"
    if disc_rate >= 30:
        return "🔥", "특가할인"
    if disc_rate >= MIN_HOTDEAL_DISCOUNT_RATE:
        return "🏷️", "알뜰할인"
    return "🏷️", "할인"


def format_deal_message(clean_title: str, p_info: Dict[str, Any], deal_url: str = "") -> str:
    """검증된 가격 정보만으로 텔레그램 HTML 메시지 생성"""
    orig_p = p_info["orig_price"]
    best_price = p_info["final_price"]
    disc_rate = p_info["discount_rate"]

    if orig_p > best_price >= 1000:
        if not _coupang_rate_matches(orig_p, best_price, disc_rate):
            disc_rate = (orig_p - best_price) * 100 // orig_p
        if not (3 <= disc_rate <= 85):
            orig_p = 0
            disc_rate = 0
    else:
        orig_p = 0
        disc_rate = 0

    header_line = "🔥 <b>[실시간 검증 특가]</b>"
    if MIN_HOTDEAL_DISCOUNT_RATE <= disc_rate <= 85 and orig_p > best_price > 0:
        badge_emoji, badge_label = get_discount_badge(disc_rate)
        header_line = f"{badge_emoji} <b>[{disc_rate}% {badge_label}] 정상가 <s>{orig_p:,}원</s> ➡️ 특가 {best_price:,}원!</b>"

    price_lines = []
    if orig_p > best_price > 0:
        save_amt = orig_p - best_price
        price_lines.append(f"• <b>정상가격</b>: <s>{orig_p:,}원</s> (<b>{save_amt:,}원 절약!</b>)")

    if best_price > 0:
        if 3 <= disc_rate <= 85 and orig_p > best_price:
            badge_emoji, _ = get_discount_badge(disc_rate)
            disc_text = f" ({badge_emoji} <b>{disc_rate}% 할인</b>)"
        else:
            disc_text = ""
        price_lines.append(f"• <b>최종특가</b>: <b>{best_price:,}원</b>{disc_text}")
    else:
        price_lines.append("• <b>최종특가</b>: <b>한정수량 최저가</b>")

    if p_info["unit_info"]:
        price_lines.append(f"• <b>단가계산</b>: <b>{p_info['unit_info']}</b>")

    price_block = "\n".join(price_lines)
    link_block = (
        f'🔗 <b><a href="{html.escape(deal_url, quote=True)}">👉 쿠팡 특가 바로가기 (클릭)</a></b>\n\n'
        if deal_url
        else ""
    )
    reaction_block = (
        f"💬 <b>커뮤니티 반응</b>: <code>{esc(p_info['reaction_text'])}</code>\n\n"
        if p_info["reaction_text"]
        else ""
    )

    ship_lbl = str(p_info.get("shipping_label") or "").strip()
    shipping_block = f"🚚 <b>{esc(ship_lbl)}</b>\n" if ship_lbl else ""

    return (
        f"{header_line}\n\n"
        f"📌 <b>{esc(clean_title)}</b>\n\n"
        f"💵 <b>특가 정보</b>\n"
        f"{price_block}\n\n"
        f"{link_block}"
        f"{reaction_block}"
        f"{shipping_block}"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>핫딜 특성상 가격 변동 및 조기 품절될 수 있습니다.</i>\n"
        f"※ 이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
    )


_AMBIGUOUS_TRACK_B_RE = re.compile(
    r"\d[\d,]*\s*원\s*(?:부터|~)|최대\s*할인|체감가|적립\s*(?:포함|후)\s*[\d,]+\s*원"
)


def _has_verified_reaction(
    d: Dict[str, Any],
    source_name: str,
    base_title: str,
    body_text: str,
) -> bool:
    votes = int(d.get("votes", 0) or 0)
    downvotes = int(d.get("downvotes", 0) or 0)
    comments = int(d.get("comments", 0) or 0)
    hits = int(d.get("hits", 0) or 0)

    if source_name == "arca" and (votes >= 1 or comments >= 3):
        return True
    if source_name == "ruliweb" and (votes >= 1 or comments >= 2 or hits >= 700):
        return True
    if source_name == "ppomppu" and (votes >= 1 or comments >= 3):
        return True
    if source_name == "dealbada" and (votes >= 2 or comments >= 2):
        return True

    combined = f"{base_title}\n{body_text}"
    if downvotes < 2 and any(kw in combined for kw in ("역대가", "역대", "대란", "꿀딜")):
        return True
    return False


def _has_track_b_food_reaction(
    d: Dict[str, Any],
    source_name: str,
    base_title: str,
    body_text: str,
) -> bool:
    votes = int(d.get("votes", 0) or 0)
    downvotes = int(d.get("downvotes", 0) or 0)
    comments = int(d.get("comments", 0) or 0)
    if votes < 0:
        return False
    if downvotes >= 2 and not (votes >= 8 and votes >= downvotes * 4):
        return False
    if downvotes >= 1 and votes < 5:
        return False
    if votes >= 5:
        return True
    if downvotes == 0 and votes >= 3 and comments >= 10:
        return True
    if source_name == "dealbada" and downvotes == 0 and votes >= 4 and comments >= 3:
        return True
    return False


def evaluate_deal_quality(
    d: Dict[str, Any],
    source_name: str,
    p_info: Dict[str, Any],
    base_title: str,
    body_text: str,
    option_key: str = "",
    price_db: Optional[PriceHistoryDB] = None,
) -> Dict[str, Any]:
    """
    투트랙 + 카테고리별 품질 게이트 평가.
    - Track A: 명시적 정상가 검증 + 기본 전 카테고리 최소 20% 할인
      (15,000원 이하 식품/생필품 무료·와우배송 미끼 상품은 15%+ 허용, 정가 30만원 이상 고가 상품은 12%+ & 5만원+ 절약 예외 허용)
    - Track B1: 정상가 미확인 딜 중 30일 DB 관측(5일+ 중앙값) 대비 20%+ 하락 검증
    - Track B2: ENABLE_TRACK_B2_NO_ORIG_FOOD 활성화 시에만 동작 (기본 False)
    반환 dict: {passed, reason, track, category, evidence_badge, evidence_note, baseline_price, observed_disc_rate, effective_unit_display}
    """
    category = classify_deal_category(base_title, body_text)
    ship_fee = int(p_info.get("shipping_fee", 0) or 0)
    final_p = int(p_info.get("final_price", 0) or 0)
    orig_p = int(p_info.get("orig_price", 0) or 0)
    disc_rate = int(p_info.get("discount_rate", 0) or 0)
    ship_type = str(p_info.get("shipping_type") or "")
    total_p = final_p + ship_fee if ship_fee > 0 else final_p

    is_ambig, _ = has_ambiguous_option_or_price(base_title, body_text, sale_price=final_p)
    obs_ship_type = normalize_observed_shipping_type(
        ship_type,
        ship_fee,
        shipping_condition=str(p_info.get("shipping_condition") or ""),
    )
    unit_metrics = compute_effective_unit_metrics(base_title, total_p, body_text=body_text)
    effective_unit_display = str(unit_metrics.get("display_unit") or "")

    base_res: Dict[str, Any] = {
        "passed": False,
        "reason": "low_discount",
        "track": "",
        "category": category,
        "evidence_badge": "",
        "evidence_note": "",
        "baseline_price": 0,
        "observed_disc_rate": 0,
        "effective_unit_display": effective_unit_display,
        "total_price": total_p,
        "unit_metrics": unit_metrics,
        "is_ambiguous": is_ambig,
        "obs_ship_type": obs_ship_type,
    }

    low_title = base_title.lower()
    if any(kw in low_title for kw in PROMO_SKIP_KEYWORDS):
        base_res["reason"] = "permanent"
        return base_res

    votes = int(d.get("votes", 0) or 0)
    downvotes = int(d.get("downvotes", 0) or 0)
    if votes < 0 or (downvotes >= 4 and votes == 0) or (downvotes >= 6 and downvotes >= votes * 2):
        base_res["reason"] = "permanent"
        return base_res

    # Track A: 명시적 정상가 + 판매가 검증 구간
    if orig_p > final_p >= 1000:
        min_rate = CATEGORY_MIN_DISCOUNT_MAP.get(category, MIN_HOTDEAL_DISCOUNT_RATE)
        save_amt = orig_p - final_p
        high_ticket_save = (
            orig_p >= HIGH_TICKET_MIN_ORIG_PRICE
            and save_amt >= IT_HIGH_SAVINGS_MIN_WON
            and disc_rate >= IT_HIGH_SAVINGS_MIN_RATE
        )
        low_price_bait = (
            category in LOW_PRICE_BAIT_CATEGORIES
            and final_p <= LOW_PRICE_BAIT_MAX_PRICE
            and obs_ship_type in ("free", "wow_free")
            and disc_rate >= LOW_PRICE_BAIT_MIN_RATE
        )

        if disc_rate < min_rate and not high_ticket_save and not low_price_bait:
            base_res["reason"] = "low_discount"
            return base_res

        if disc_rate >= STRONG_HOTDEAL_DISCOUNT_RATE and (downvotes < 3 or votes >= 2):
            base_res.update({"passed": True, "reason": "ok", "track": "A"})
            return base_res
        if (orig_p >= 1_000_000 or final_p >= 1_000_000) and disc_rate >= MIN_HOTDEAL_DISCOUNT_RATE and (downvotes < 3 or votes >= 2):
            base_res.update({"passed": True, "reason": "ok", "track": "A"})
            return base_res
        if high_ticket_save and (downvotes < 3 or votes >= 2):
            base_res.update({"passed": True, "reason": "ok", "track": "A"})
            return base_res
        if _has_verified_reaction(d, source_name, base_title, body_text):
            base_res.update({"passed": True, "reason": "ok", "track": "A"})
            return base_res

        base_res["reason"] = "wait"
        return base_res

    # Track B: 정상가 미확인 딜 (판매가 >= 1,000원 필수)
    if final_p < 1000:
        base_res["reason"] = "low_discount"
        return base_res

    if is_ambig or _AMBIGUOUS_TRACK_B_RE.search(base_title):
        base_res["reason"] = "wait"
        return base_res

    # Track B1: 동일 확정 옵션/구성 + 확정 배송 조건 30일(5일 이상 관측) 중앙값 대비 할인 검증
    if price_db is not None and is_comparable_option_key(option_key) and obs_ship_type in ("free", "paid", "wow_free"):
        baseline = price_db.get_observed_baseline(
            option_key,
            shipping_type=obs_ship_type,
            days=30,
            min_days=5,
        )
        if baseline and baseline["median_price"] > total_p:
            med_p = int(baseline["median_price"])
            obs_disc = (med_p - total_p) * 100 // med_p
            req_obs_disc = CATEGORY_MIN_DISCOUNT_MAP.get(category, MIN_HOTDEAL_DISCOUNT_RATE)
            if obs_disc >= req_obs_disc:
                base_res.update({
                    "passed": True,
                    "reason": "ok",
                    "track": "B",
                    "evidence_badge": f"30일 관측가 {obs_disc}%↓",
                    "evidence_note": f"30일 기준 {med_p:,}원 ({baseline['distinct_days']}일 관측)",
                    "baseline_price": med_p,
                    "observed_disc_rate": obs_disc,
                })
                return base_res

    # Track B2: 식품/반복소비재 단일 구성 단가 + 확정 배송 + 반응 게이트 (기본 비활성화)
    if ENABLE_TRACK_B2_NO_ORIG_FOOD and category == "food" and effective_unit_display:
        if obs_ship_type not in ("free", "paid", "wow_free"):
            base_res["reason"] = "wait"
            return base_res
        if _has_track_b_food_reaction(d, source_name, base_title, body_text):
            ship_note = f"배송포함 {effective_unit_display}" if ship_fee > 0 else effective_unit_display
            src_kr = get_source_label(source_name)
            votes_n = int(d.get("votes", 0) or 0)
            comments_n = int(d.get("comments", 0) or 0)
            stat_parts: List[str] = []
            if votes_n > 0:
                stat_parts.append(f"추천 {votes_n}")
            if comments_n > 0:
                stat_parts.append(f"댓글 {comments_n}")
            stat_str = " · ".join(stat_parts) if stat_parts else "반응 확인"
            base_res.update({
                "passed": True,
                "reason": "ok",
                "track": "B",
                "evidence_badge": f"{src_kr} 인기",
                "evidence_note": f"{ship_note} · {src_kr} {stat_str}",
            })
            return base_res
        base_res["reason"] = "wait"
        return base_res

    base_res["reason"] = "low_discount"
    return base_res


def is_quality_hotdeal(
    d: Dict[str, Any],
    source_name: str,
    p_info: Dict[str, Any],
    base_title: str,
    body_text: str,
) -> Tuple[bool, str]:
    """기존 2-튜플 시그니처 호환 래퍼"""
    res = evaluate_deal_quality(d, source_name, p_info, base_title, body_text)
    return bool(res["passed"]), str(res["reason"])


def _drop_ocr_cache_entry(ocr_cache: Dict[str, Any], deal_id: str) -> None:
    if deal_id in ocr_cache:
        ocr_cache.pop(deal_id, None)
        save_ocr_cache(ocr_cache)


def _can_reenter_recent_product(
    product_id: str,
    option_key: str,
    total_price: int,
    price_db: PriceHistoryDB,
) -> Tuple[bool, float]:
    """최근 3일 내 동일 product_id라도 같은 옵션 5%+ 추가 하락 또는 별도 옵션이면 재진입 허용"""
    last_pub = price_db.get_last_published(option_key)
    if last_pub is not None:
        return price_db.should_reenter_price_drop(
            option_key,
            total_price,
            drop_pct=PRICE_DROP_REENTRY_PCT,
            window_days=3,
        )
    if (":vid:" in option_key or ":iid:" in option_key) and product_id:
        with price_db._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM published_prices WHERE product_id = ? LIMIT 1",
                (str(product_id),),
            ).fetchone()
        if row is not None:
            return True, 0.0
    return False, 0.0


def _format_mega_log_tag(item: Dict[str, Any]) -> str:
    disc = int(item.get("discount_rate", 0) or 0)
    if is_half_off_mega(item):
        if disc >= 70:
            return "70%+ 대폭 할인"
        if disc >= 60:
            return "반값 이상"
        return "반값 할인"
    return "100만+ 고가"


def _safe_cb_data(prefix: str, value: str, max_bytes: int = 64) -> str:
    """Telegram callback_data 64 UTF-8 바이트 제한 안전 절삭."""
    raw = str(value or "").strip()
    prefix_bytes = len(prefix.encode("utf-8"))
    budget = max(1, max_bytes - prefix_bytes)
    out = []
    used = 0
    for ch in raw:
        ch_b = len(ch.encode("utf-8"))
        if used + ch_b > budget:
            break
        out.append(ch)
        used += ch_b
    return prefix + "".join(out)


def notify_keyword_subscribers(
    item: Dict[str, Any],
    alerts: Optional[List[Dict[str, Any]]] = None,
    notified_deal_ids: Optional[Set[str]] = None,
) -> int:
    """등록된 구독자 맞춤 키워드(동의어·가격상한·할인율·제외어 반영) 매칭 시 리치 DM 즉시 발송."""
    if alerts is None or not alerts:
        return 0
    clean_title = str(item.get("clean_title") or "").strip()
    deeplink = str(item.get("deeplink") or "").strip()
    if not clean_title or not deeplink:
        return 0

    deal_id = str(item.get("deal_id") or "").strip()
    if notified_deal_ids is not None and deal_id and deal_id in notified_deal_ids:
        return 0

    sale_p = int(item.get("sale_price", 0) or 0)
    orig_p = int(item.get("orig_price", 0) or 0)
    disc_r = int(item.get("discount_rate", 0) or 0)

    matched = match_alerts_for_deal(clean_title, alerts, sale_price=sale_p, discount_rate=disc_r)
    if not matched:
        return 0

    ship_lbl = str(item.get("shipping_label") or "").strip()
    unit_info = str(item.get("unit_info") or "").strip()
    reaction_txt = str(item.get("reaction_text") or "").strip()
    if len(reaction_txt) > 70:
        reaction_txt = reaction_txt[:69].rstrip() + "…"
    raw_img = str(item.get("img_url") or "").strip()
    img_url = raw_img if raw_img.startswith(("http://", "https://")) else None
    src_kr = get_source_label(str(item.get("source") or ""))

    price_str = f"<b>{sale_p:,}원</b>" if sale_p >= 1000 else "<b>가격 확인</b>"
    orig_part = f"<s>{orig_p:,}원</s> → " if (orig_p > sale_p >= 1000 and disc_r >= 3) else ""
    rate_part = f"<b>{disc_r}%↓</b> " if disc_r > 0 else ""
    unit_part = f" · {esc(unit_info)}" if unit_info else ""
    ship_part = f" · {esc(ship_lbl)}" if ship_lbl else ""
    src_part = f" ({esc(src_kr)})" if src_kr else ""
    btn_text = f"🛒 {sale_p:,}원 바로가기 ↗" if sale_p >= 1000 else "🛒 쿠팡 바로가기 ↗"

    now_hour = datetime.now(KST).hour
    is_night = now_hour >= 23 or now_hour < 8

    sent_cnt = 0
    sent_hits: List[Tuple[str, str]] = []
    dead_chat_ids: Set[str] = set()

    for idx, (chat_id, info) in enumerate(matched.items()):
        kws = list(info.get("keywords") or [])
        kw_norms = list(info.get("keyword_norms") or [])
        alert_ids = list(info.get("alert_ids") or [])
        quiet_night = bool(info.get("quiet_night", True))

        kw_str = ", ".join(kws[:3])
        first_kw = kws[0] if kws else "키워드"
        first_kw_short = (first_kw[:9] + "…") if len(first_kw) > 10 else first_kw
        if alert_ids:
            mute_cb = f"alert:muteid:{alert_ids[0]}"
        else:
            mute_cb = _safe_cb_data("alert:mute:", first_kw)

        react_block = f'\n💬 <i>"{esc(reaction_txt)}"</i>' if reaction_txt else ""
        dm_text = (
            f"⚡ <b>[초고속 키워드 알림: {esc(kw_str)}]</b>\n\n"
            f"📌 <b>{esc(clean_title)}</b>\n"
            f"💵 {rate_part}{orig_part}{price_str}{unit_part}{ship_part}{src_part}"
            f"{react_block}\n\n"
            f"<i>※ 쿠팡 파트너스 활동으로 일정액의 수수료를 제공받습니다.</i>"
        )
        inline_kb = [
            [{"text": btn_text, "url": deeplink, "style": "success"}],
            [
                {"text": f"🔕 '{first_kw_short}' 끄기", "callback_data": mute_cb},
                {"text": "⚙️ 키워드 관리", "callback_data": "alert:menu"},
            ],
        ]
        disable_notif = bool(is_night and quiet_night)

        try:
            res = send_telegram(
                chat_id=chat_id,
                text=dm_text,
                photo_url=img_url,
                inline_keyboard=inline_kb,
                disable_notification=disable_notif,
                return_detail=True,
            )
            if isinstance(res, tuple):
                ok, err_code, err_desc = bool(res[0]), int(res[1] or 0), str(res[2] or "")
            else:
                ok, err_code, err_desc = bool(res), 0, ""

            if not ok and err_code == 429:
                m_wait = re.search(r"retry after (\d+)", err_desc, re.I)
                wait_s = min(3.0, max(0.5, float(m_wait.group(1)) if m_wait else 1.0))
                time.sleep(wait_s)
                res2 = send_telegram(
                    chat_id=chat_id,
                    text=dm_text,
                    photo_url=img_url,
                    inline_keyboard=inline_kb,
                    disable_notification=disable_notif,
                    return_detail=True,
                )
                if isinstance(res2, tuple):
                    ok, err_code, err_desc = bool(res2[0]), int(res2[1] or 0), str(res2[2] or "")
                else:
                    ok, err_code, err_desc = bool(res2), 0, ""

            if ok:
                sent_cnt += 1
                for kn in kw_norms:
                    sent_hits.append((chat_id, kn))
            else:
                low_err = err_desc.lower()
                if (
                    err_code == 403
                    or "blocked" in low_err
                    or "deactivated" in low_err
                    or "chat not found" in low_err
                ):
                    dead_chat_ids.add(chat_id)
        except Exception as e:
            logger.warning(f"키워드 알림 DM 실패 (chat={chat_id}): {e}")

        if idx + 1 < len(matched):
            time.sleep(0.05)

    if dead_chat_ids:
        for dead_cid in dead_chat_ids:
            try:
                remove_keyword_alert(dead_cid, remove_all=True, purge_prefs=True)
            except Exception:
                pass
        alerts[:] = [r for r in alerts if str(r.get("chat_id") or "").strip() not in dead_chat_ids]
        logger.info(f"🧹 [키워드 알림] 봇 차단/탈퇴 구독자 {len(dead_chat_ids)}명 자동 정리 완료")

    if sent_hits:
        try:
            record_alert_hits(sent_hits)
        except Exception:
            pass

    if notified_deal_ids is not None and deal_id:
        notified_deal_ids.add(deal_id)

    if sent_cnt > 0:
        logger.info(f"🔔 [키워드 알림] {clean_title[:30]} -> {sent_cnt}명 DM 발송")
    return sent_cnt


def process_deal(
    d: Dict[str, Any],
    detail_parser,
    source_name: str,
    seen: Set[str],
    flyer_queue: Optional[List[Dict[str, Any]]] = None,
    quota: Optional[Dict[str, Any]] = None,
    run_stats: Optional[Dict[str, int]] = None,
    ocr_cache: Optional[Dict[str, Any]] = None,
    alerts_cache: Optional[List[Dict[str, Any]]] = None,
    notified_deal_ids: Optional[Set[str]] = None,
) -> bool:
    deal_id = d["id"]
    if deal_id in seen:
        return False

    price_db = get_price_db()
    if price_db.get_source_cooldown(source_name):
        return False
    wait_key = f"{source_name}:{deal_id}"
    skip_wait, skip_reason = price_db.should_skip_wait_recheck(wait_key, WAIT_RECHECK_MIN_MINUTES)
    if skip_wait:
        if skip_reason == "expired":
            logger.info(f"⏰ [대기 만료 제외] 120분 초과로 재확인 종료: {d.get('title', '')[:40]}")
            seen.add(deal_id)
            save_seen(seen)
            if ocr_cache is not None:
                _drop_ocr_cache_entry(ocr_cache, deal_id)
            price_db.delete_pending_wait(wait_key)
        return False

    if ocr_cache is None:
        ocr_cache = load_ocr_cache()

    try:
        detail = detail_parser(d["link"])
    except requests.RequestException as e:
        logger.warning(f"⏳ [{source_name}] 상세 조회 일시 오류 (다음 회차 재시도): {deal_id} ({e})")
        return False

    if isinstance(detail, dict) and detail.get("transient_error"):
        logger.warning(
            f"⏳ [{source_name}] 상세 조회 일시 실패 ({detail.get('error_reason', 'unknown')}) — 다음 회차 재시도: {deal_id}"
        )
        return False

    if not detail or not detail.get("coupang_url"):
        seen.add(deal_id)
        save_seen(seen)
        _drop_ocr_cache_entry(ocr_cache, deal_id)
        price_db.delete_pending_wait(wait_key)
        return False

    raw_url = detail["coupang_url"]
    clean_url, product_id = clean_coupang_url(raw_url)
    body_text = detail.get("body_text", "")
    img_url = detail.get("img_url")
    ocr_imgs = detail.get("ocr_img_urls") or ([img_url] if img_url else [])
    extra_shipping = str(detail.get("extracted_shipping") or "")

    base_title = detail.get("extracted_title") or d["title"]
    if is_closed_deal(base_title) or any(kw in base_title for kw in PROMO_SKIP_KEYWORDS):
        seen.add(deal_id)
        save_seen(seen)
        _drop_ocr_cache_entry(ocr_cache, deal_id)
        price_db.delete_pending_wait(wait_key)
        return False

    option_key = build_option_key(product_id=product_id, url=clean_url, title=base_title, body_text=body_text)
    pricing_title = base_title
    if detail.get("extracted_price") and detail["extracted_price"] >= 1000:
        if f"{detail['extracted_price']:,}" not in pricing_title and str(detail["extracted_price"]) not in pricing_title:
            pricing_title = f"{pricing_title} ({detail['extracted_price']:,}원)"

    clean_title = clean_display_title(base_title)

    # 최근 3일 동일 상품 중복 확인 (단, 동일 옵션 5%+ 추가 하락 또는 별도 옵션은 재진입 판단)
    recently_seen = is_product_recently_seen(product_id, seen)
    quick_p = parse_pricing_details(pricing_title, body_text, "", extra_shipping=extra_shipping)
    quick_total = int(quick_p["final_price"]) + int(quick_p.get("shipping_fee", 0) or 0)
    if recently_seen and quick_total >= 1000:
        can_reenter, drop_pct = _can_reenter_recent_product(product_id, option_key, quick_total, price_db)
        if not can_reenter:
            logger.info(f"⏭️ [중복상품 스킵] 최근 3일 내 동일 productId({product_id}) 이미 송출됨: {d['title'][:40]}")
            seen.add(deal_id)
            save_seen(seen)
            _drop_ocr_cache_entry(ocr_cache, deal_id)
            price_db.delete_pending_wait(wait_key)
            return False
        if drop_pct > 0:
            logger.info(f"📉 [가격하락 재진입 후보] {option_key} 추가 {drop_pct}%↓: {clean_title[:35]}")

    # 이미지 OCR 가격 헤더 추출(캐시 재사용) 및 팩트 기반 가격·배송 분석
    ocr_text, ocr_failed = get_or_extract_ocr_text(
        deal_id,
        pricing_title,
        body_text,
        ocr_imgs,
        ocr_cache,
        extra_shipping=extra_shipping,
    )
    p_info = parse_pricing_details(pricing_title, body_text, ocr_text, extra_shipping=extra_shipping)
    total_price = int(p_info["final_price"]) + int(p_info.get("shipping_fee", 0) or 0)

    if recently_seen:
        if p_info["sale_price"] < 1000:
            if ocr_failed and ocr_imgs:
                return False
            seen.add(deal_id)
            save_seen(seen)
            _drop_ocr_cache_entry(ocr_cache, deal_id)
            price_db.delete_pending_wait(wait_key)
            return False
        can_reenter, drop_pct = _can_reenter_recent_product(product_id, option_key, total_price, price_db)
        if not can_reenter:
            logger.info(f"⏭️ [중복상품 스킵] 최근 3일 내 동일 productId({product_id}) 이미 송출됨: {d['title'][:40]}")
            seen.add(deal_id)
            save_seen(seen)
            _drop_ocr_cache_entry(ocr_cache, deal_id)
            price_db.delete_pending_wait(wait_key)
            return False
        if drop_pct > 0:
            logger.info(f"📉 [가격하락 재진입 확정] {option_key} 추가 {drop_pct}%↓ ({total_price:,}원)")

    if p_info["sale_price"] < 1000:
        if ocr_failed and ocr_imgs:
            logger.warning(f"⏳ [OCR 일시 실패] 이미지 판독 미완료로 다음 회차 재시도: {base_title}")
            return False
        logger.info(f"⏭️ [스킵] 유효 가격 식별 불가: {base_title}")
        seen.add(deal_id)
        save_seen(seen)
        _drop_ocr_cache_entry(ocr_cache, deal_id)
        price_db.delete_pending_wait(wait_key)
        return False

    # 유효 가격 관측 이력 적재 (확정 옵션/구성 + 확정 배송 조건 'free'|'paid'만 1일 최저가 기록)
    eval_res = evaluate_deal_quality(
        d,
        source_name,
        p_info,
        base_title,
        body_text,
        option_key=option_key,
        price_db=price_db,
    )
    if (
        eval_res["reason"] != "permanent"
        and not eval_res.get("is_ambiguous")
        and is_comparable_option_key(option_key)
        and eval_res.get("obs_ship_type") in ("free", "paid", "wow_free")
    ):
        um = eval_res.get("unit_metrics") or {}
        price_db.record_observation(
            option_key=option_key,
            product_id=product_id,
            total_price=total_price,
            sale_price=int(p_info["final_price"]),
            shipping_fee=int(p_info.get("shipping_fee", 0) or 0),
            orig_price=int(p_info["orig_price"]),
            shipping_type=str(eval_res["obs_ship_type"]),
            category=str(eval_res["category"]),
            source=source_name,
            metric_type=str(um.get("metric_type") or ""),
            metric_value=um.get("metric_value"),
        )

    ok_q = bool(eval_res["passed"])
    reason = str(eval_res["reason"])
    if not ok_q:
        if reason == "permanent":
            logger.info(f"🚫 [영구제외] 비핫딜/비추천 과다: {clean_title}")
            seen.add(deal_id)
            save_seen(seen)
            _drop_ocr_cache_entry(ocr_cache, deal_id)
            price_db.delete_pending_wait(wait_key)
        elif reason == "low_discount":
            min_cat_rate = CATEGORY_MIN_DISCOUNT_MAP.get(eval_res["category"], MIN_HOTDEAL_DISCOUNT_RATE)
            if ocr_failed and ocr_imgs and (
                p_info["orig_price"] <= p_info["final_price"]
                or p_info["discount_rate"] < min_cat_rate
            ):
                logger.warning(f"⏳ [OCR 일시 실패] 정상가/할인 근거 미확보로 다음 회차 재시도: {clean_title}")
                return False
            logger.info(
                f"⏭️ [기준 미달 스킵] cat={eval_res['category']}, 할인={p_info['discount_rate']}% (<{min_cat_rate}%): {clean_title}"
            )
            seen.add(deal_id)
            save_seen(seen)
            _drop_ocr_cache_entry(ocr_cache, deal_id)
            price_db.delete_pending_wait(wait_key)
        else:
            price_db.upsert_pending_wait(
                wait_key=wait_key,
                source=source_name,
                post_id=deal_id,
                option_key=option_key,
                url=d["link"],
                title=clean_title,
                category=str(eval_res["category"]),
                reason="wait",
                max_wait_minutes=WAIT_EXPIRE_MINUTES,
            )
            logger.info(
                f"⏳ [검증대기/{eval_res['category']}] 기준 미달(추천={d.get('votes',0)}, 댓글={d.get('comments',0)}, 할인={p_info['discount_rate']}%): {clean_title}"
            )
        return False

    price_db.delete_pending_wait(wait_key)
    track = str(eval_res["track"] or "A")

    # 파트너스 딥링크 생성 (제3자 lptag 제거된 정규 URL 사용, Track B는 *np subId 분리)
    try:
        sub_id = _resolve_sub_id(source_name, track=track)
        res = coupang_api.create_deeplink([clean_url], sub_id=sub_id)
        items = res.get("data", [])
        if not items:
            seen.add(deal_id)
            save_seen(seen)
            _drop_ocr_cache_entry(ocr_cache, deal_id)
            return False
        deeplink = items[0].get("shortenUrl") or items[0].get("landingUrl")
    except Exception as e:
        logger.error(f"딥링크 생성 실패: {e}")
        return False

    deeplink = register_tracked_link(
        deeplink,
        deal_id=deal_id,
        product_id=product_id,
        source=source_name,
        track=track,
        title=clean_title,
        sale_price=int(p_info["final_price"]),
        discount_rate=int(p_info["discount_rate"]),
    )

    display_unit = str(p_info.get("unit_info") or "")
    eff_unit = str(eval_res.get("effective_unit_display") or "")
    if eval_res.get("is_ambiguous"):
        display_unit = ""
    elif eff_unit:
        display_unit = f"{eff_unit} (배송포함)" if int(p_info.get("shipping_fee", 0) or 0) > 0 else eff_unit

    reaction_quote = extract_best_reaction_quote(
        detail.get("comment_texts") or [],
        fallback_reaction=str(p_info.get("reaction_text") or ""),
    )

    # 전단지 큐 모드: 검증 완료 즉시 큐에 적재하고 중복 방지를 위해 seen 기록
    if flyer_queue is not None:
        item = {
            "deal_id": deal_id,
            "product_id": product_id,
            "option_key": option_key,
            "source": source_name,
            "track": track,
            "category": eval_res["category"],
            "evidence_badge": eval_res["evidence_badge"],
            "evidence_note": eval_res["evidence_note"],
            "clean_title": clean_title,
            "short_title": shorten_card_title(clean_title),
            "orig_price": int(p_info["orig_price"]),
            "sale_price": int(p_info["final_price"]),
            "discount_rate": int(p_info["discount_rate"]),
            "unit_info": display_unit,
            "votes": int(d.get("votes", 0) or 0),
            "comments": int(d.get("comments", 0) or 0),
            "reaction_text": reaction_quote,
            "shipping_fee": int(p_info.get("shipping_fee", 0) or 0),
            "shipping_type": str(p_info.get("shipping_type") or ""),
            "shipping_condition": str(p_info.get("shipping_condition") or ""),
            "shipping_label": str(p_info.get("shipping_label") or ""),
            "chip": build_card_chip(
                clean_title,
                int(p_info["orig_price"]),
                int(p_info["final_price"]),
                display_unit,
                shipping_label=str(p_info.get("shipping_label") or ""),
            ),
            "deeplink": deeplink,
            "img_url": img_url,
            "queued_at": int(time.time()),
        }
        is_vip_or_half = is_half_off_mega(item) or is_high_ticket_mega(item)
        if is_vip_or_half and not is_quiet_hours():
            try:
                png_bytes = render_hotdeal_flyer([item], coupang_api=coupang_api)
                caption, inline_kb = format_flyer_caption_and_buttons([item])
                if send_telegram_photo_bytes(CHANNEL_ID, png_bytes, caption, inline_kb):
                    seen.add(deal_id)
                    mark_product_seen(product_id, seen)
                    save_seen(seen)
                    _drop_ocr_cache_entry(ocr_cache, deal_id)
                    price_db.record_published(option_key, product_id, total_price, int(p_info["final_price"]), track=track)
                    if quota is not None:
                        quota["community_sent"] = int(quota.get("community_sent", 0) or 0) + 1
                        quota["last_community_ts"] = int(time.time())
                        save_post_quota(quota)
                    if run_stats is not None:
                        run_stats["sent"] = run_stats.get("sent", 0) + 1
                    tag = _format_mega_log_tag(item)
                    logger.info(
                        f"🚨 [{source_name}] {tag} 단독 리치전단지 즉시 송출 완료: {clean_title} "
                        f"(정가={item['orig_price']:,}원, 특가={item['sale_price']:,}원, 할인율={item['discount_rate']}%, 배송={item['shipping_label'] or '미확인'})"
                    )
                    notify_keyword_subscribers(
                        item,
                        alerts=alerts_cache,
                        notified_deal_ids=notified_deal_ids,
                    )
                    time.sleep(2)
                    return True
            except Exception as e:
                logger.warning(f"대형 할인 단독 전단지 즉시 송출 실패 (큐 폴백): {e}")

        flyer_queue.append(item)
        seen.add(deal_id)
        mark_product_seen(product_id, seen)
        save_seen(seen)
        save_flyer_queue(flyer_queue)
        _drop_ocr_cache_entry(ocr_cache, deal_id)
        price_db.record_published(option_key, product_id, total_price, int(p_info["final_price"]), track=track)
        if run_stats is not None:
            run_stats["queued"] = run_stats.get("queued", 0) + 1
        logger.info(
            f"📥 [{source_name}/Track{track}] 전단지 큐 적재 ({len(flyer_queue)}/{FLYER_BATCH_SIZE}): {clean_title} "
            f"(정가={item['orig_price']:,}원, 특가={item['sale_price']:,}원, 할인율={item['discount_rate']}%, 배송={item['shipping_label'] or '미확인'})"
        )
        notify_keyword_subscribers(
            item,
            alerts=alerts_cache,
            notified_deal_ids=notified_deal_ids,
        )
        return True

    p_info["reaction_text"] = reaction_quote
    msg = format_deal_message(clean_title, p_info, deal_url=deeplink)
    btn_price = p_info["final_price"]
    btn_text = f"🛒 쿠팡에서 {btn_price:,}원에 구매하기 ↗" if btn_price >= 1000 else "🛒 쿠팡에서 보기 ↗"

    ok = send_telegram(
        chat_id=CHANNEL_ID,
        text=msg,
        photo_url=img_url,
        button_text=btn_text,
        button_url=deeplink,
        button_style="success",
    )

    if ok:
        seen.add(deal_id)
        mark_product_seen(product_id, seen)
        save_seen(seen)
        _drop_ocr_cache_entry(ocr_cache, deal_id)
        price_db.record_published(option_key, product_id, total_price, int(p_info["final_price"]), track=track)
        logger.info(
            f"✅ [{source_name}/Track{track}] 송출 완료: {clean_title} "
            f"(정가={p_info['orig_price']:,}원, 특가={p_info['final_price']:,}원, 할인율={p_info['discount_rate']}%)"
        )
        notify_keyword_subscribers(
            {
                "deal_id": deal_id,
                "clean_title": clean_title,
                "orig_price": int(p_info["orig_price"]),
                "sale_price": int(p_info["final_price"]),
                "discount_rate": int(p_info["discount_rate"]),
                "unit_info": display_unit,
                "shipping_label": str(p_info.get("shipping_label") or ""),
                "reaction_text": reaction_quote,
                "img_url": img_url,
                "source": source_name,
                "deeplink": deeplink,
            },
            alerts=alerts_cache,
            notified_deal_ids=notified_deal_ids,
        )
        time.sleep(2)
        return True

    return False


def _prune_flyer_queue(queue: List[Dict[str, Any]], cur_ts: float) -> bool:
    """만료(TTL 초과) 또는 중복 product_id 항목 정리. 변경 시 True 반환"""
    cutoff = cur_ts - FLYER_ITEM_TTL_HOURS * 3600
    kept: List[Dict[str, Any]] = []
    seen_pids: Set[str] = set()
    seen_dids: Set[str] = set()
    changed = False
    for it in queue:
        q_ts = int(it.get("queued_at", cur_ts) or cur_ts)
        did = str(it.get("deal_id") or "")
        pid = str(it.get("product_id") or "")
        if q_ts < cutoff:
            changed = True
            continue
        if (did and did in seen_dids) or (pid and pid in seen_pids):
            changed = True
            continue
        if did:
            seen_dids.add(did)
        if pid:
            seen_pids.add(pid)
        kept.append(it)
    if changed:
        queue[:] = kept
    return changed


def should_flush_partial_queue(
    queue: List[Dict[str, Any]],
    now_dt: Optional[datetime] = None,
    now_ts: Optional[float] = None,
) -> Tuple[bool, str]:
    """4건 미만 부분 큐를 시간/긴급도/야간 진입 직전 조건에 따라 송출할지 판별"""
    if not queue:
        return False, "empty"
    cur_dt = now_dt or datetime.now(KST)
    cur_ts = time.time() if now_ts is None else now_ts

    if len(queue) >= FLYER_BATCH_SIZE:
        return True, "full_batch"
    if ENABLE_QUIET_HOURS and cur_dt.hour == 23 and cur_dt.minute >= 30:
        return True, "pre_quiet_flush"

    oldest_ts = min(int(it.get("queued_at", cur_ts) or cur_ts) for it in queue)
    age_sec = max(0.0, cur_ts - oldest_ts)

    if any(is_half_off_mega(it) or is_high_ticket_mega(it) for it in queue):
        if age_sec >= FLYER_URGENT_WAIT_MINUTES * 60:
            return True, "urgent_timeout"

    if len(queue) >= 2 and age_sec >= FLYER_MAX_WAIT_MINUTES * 60:
        return True, "multi_wait_timeout"

    if len(queue) == 1 and age_sec >= FLYER_SOLO_WAIT_MINUTES * 60:
        return True, "solo_wait_timeout"

    return False, "waiting"


def flush_flyer_queue(
    queue: List[Dict[str, Any]],
    quota: Dict[str, Any],
    now_dt: Optional[datetime] = None,
    now_ts: Optional[float] = None,
) -> int:
    """전단지 큐를 확인해 4건 묶음 또는 대기 초과분을 v5 빅타이포 전단지로 송출하고 송출 상품 수 반환"""
    cur_dt = now_dt or datetime.now(KST)
    cur_ts = time.time() if now_ts is None else now_ts

    if _prune_flyer_queue(queue, cur_ts):
        save_flyer_queue(queue)

    if not queue:
        return 0
    if is_quiet_hours(cur_dt):
        logger.info(f"🌙 [야간 대기] 전단지 큐 {len(queue)}건 오전 {QUIET_HOURS_END}시 송출 예정")
        return 0

    sent_deals = 0
    while queue:
        comm_cnt = int(quota.get("community_sent", 0) or 0)
        gb_cnt = int(quota.get("goldbox_sent", 0) or 0)
        budget = min(DAILY_MAX_COMMUNITY_DEALS - comm_cnt, DAILY_MAX_TOTAL_DEALS - comm_cnt - gb_cnt)
        if budget <= 0:
            logger.info("⏸️ [송출 제한] 일일 상한 도달로 전단지 송출 중단")
            break

        should_flush, flush_reason = should_flush_partial_queue(queue, now_dt=cur_dt, now_ts=cur_ts)
        if not should_flush:
            logger.info(f"📦 [전단지 대기] 현재 {len(queue)}/{FLYER_BATCH_SIZE}건 대기 중 (사유={flush_reason})")
            break

        take_n = min(FLYER_BATCH_SIZE, len(queue), budget)
        ordered = sorted(
            queue,
            key=lambda x: (-int(x.get("discount_rate", 0) or 0), int(x.get("queued_at", 0) or 0)),
        )
        batch = ordered[:take_n]

        try:
            png_bytes = render_hotdeal_flyer(batch, coupang_api=coupang_api)
            caption, inline_kb = format_flyer_caption_and_buttons(batch)
        except Exception as e:
            logger.error(f"전단지 렌더링 오류: {e}")
            break

        ok = send_telegram_photo_bytes(CHANNEL_ID, png_bytes, caption, inline_kb)
        if not ok:
            logger.warning("전단지 텔레그램 전송 실패 — 큐 유지 후 다음 회차 재시도")
            break

        batch_ids = {id(it) for it in batch}
        queue[:] = [it for it in queue if id(it) not in batch_ids]
        save_flyer_queue(queue)

        quota["community_sent"] = comm_cnt + len(batch)
        quota["last_community_ts"] = int(cur_ts)
        save_post_quota(quota)

        sent_deals += len(batch)
        logger.info(
            f"🖼️ [전단지({len(batch)}건) 송출 완료] (사유={flush_reason}, 금일={quota['community_sent']}/{DAILY_MAX_COMMUNITY_DEALS}, 잔여큐={len(queue)})"
        )
        time.sleep(2)

    return sent_deals


# ==========================================
# 5. 시간대 분산 로켓 골드박스 & D+1~D+2 지연 대응 쿠파스 결산
# ==========================================
GOLDBOX_EXCLUDE_CATS = (
    "국내투어", "여행", "티켓", "렌탈", "콘도", "호텔", "리프트", "숙박", "공연", "전시",
)
GOLDBOX_BAIT_TIER0_CATS = (
    "식품", "로켓프레시", "생활용품", "주방용품", "출산/유아동", "반려동물용품", "헬스/건강식품",
)
GOLDBOX_BAIT_TIER1_CATS = (
    "뷰티", "문구/오피스", "자동차용품", "스포츠/레저", "가전디지털",
)


def select_goldbox_bait_items(
    raw_items: List[Dict[str, Any]],
    seen: Set[str],
    max_items: int = 4,
    max_price: int = 30_000,
    fallback_max_price: int = 50_000,
) -> List[Dict[str, Any]]:
    """로켓배송 골드박스 상품 중 클릭 유도력 높은 생필품·식품(3만원 이하 우선) 엄선"""
    candidates: List[Tuple[Tuple[int, int, int, int], Dict[str, Any]]] = []
    seen_pids: Set[str] = set()
    for idx, it in enumerate(raw_items or []):
        if it.get("isRocket") is not True:
            continue
        cat = str(it.get("categoryName") or "")
        pname = str(it.get("productName") or "")
        if any(ex in cat or ex in pname for ex in GOLDBOX_EXCLUDE_CATS):
            continue
        prod_id = str(it.get("productId") or "").strip()
        if not prod_id or prod_id in seen_pids or is_product_recently_seen(prod_id, seen):
            continue
        p_price = int(round(float(it.get("productPrice", 0) or 0)))
        if p_price < 1000 or p_price > fallback_max_price:
            continue

        seen_pids.add(prod_id)
        price_band = 0 if p_price <= max_price else 1
        if any(t0 in cat for t0 in GOLDBOX_BAIT_TIER0_CATS):
            cat_tier = 0
        elif any(t1 in cat for t1 in GOLDBOX_BAIT_TIER1_CATS):
            cat_tier = 1
        else:
            cat_tier = 2
        sub_price_band = 0 if p_price <= 20_000 else 1
        rank_val = int(it.get("rank", idx + 1) or (idx + 1))
        candidates.append(((price_band, cat_tier, sub_price_band, rank_val), it))

    candidates.sort(key=lambda pair: pair[0])
    return [it for _, it in candidates[:max(0, max_items)]]


def run_goldbox_staggered(
    seen: Set[str],
    quota: Dict[str, Any],
    now_dt: Optional[datetime] = None,
) -> int:
    """로켓배송 골드박스 생필품·식품 미끼 4선 전단지를 오전(8~11시)·저녁(18~21시) 2회 분산 송출"""
    now = now_dt or datetime.now(KST)
    if is_quiet_hours(now) and "--force-goldbox" not in sys.argv:
        return 0

    today_str = now.strftime("%Y%m%d")
    if "--force-goldbox" in sys.argv:
        slot_num, slot_label = 1, "오전(강제)"
    elif 8 <= now.hour < 12:
        slot_num, slot_label = 1, "오전"
    elif 18 <= now.hour < 22:
        slot_num, slot_label = 2, "저녁"
    else:
        return 0

    slot_key = f"gb_{today_str}_s{slot_num}"
    legacy_key = f"gb_{today_str}"
    if "--force-goldbox" not in sys.argv:
        if slot_key in seen or (slot_num == 1 and legacy_key in seen):
            return 0

    comm_cnt = int(quota.get("community_sent", 0) or 0)
    gb_cnt = int(quota.get("goldbox_sent", 0) or 0)
    room = DAILY_MAX_TOTAL_DEALS - comm_cnt - gb_cnt
    if room <= 0:
        logger.info("⏸️ [송출 제한] 일일 총 송출 상한 도달로 골드박스 생략")
        return 0

    logger.info(f"🍯 [{slot_label}] 로켓배송 골드박스 {GOLDBOX_PER_SLOT}선 미끼 전단지 준비 중...")
    res = coupang_api.get_goldbox_products(sub_id="tggoldbox")
    raw_items = res.get("data", []) or []

    take_n = min(GOLDBOX_PER_SLOT, room)
    batch = select_goldbox_bait_items(raw_items, seen, max_items=take_n)
    if not batch:
        seen.add(slot_key)
        save_seen(seen)
        return 0

    flyer_items: List[Dict[str, Any]] = []
    for it in batch:
        prod_id = str(it.get("productId") or "").strip()
        raw_name = str(it.get("productName") or "").strip()
        p_price = int(round(float(it.get("productPrice", 0) or 0)))
        if p_price < 1000:
            continue
        p_url = str(it.get("productUrl") or "").strip()
        img_url = it.get("productImage")

        enriched_name = enrich_product_spec(raw_name, p_url, img_url)
        clean_title = clean_display_title(enriched_name) or enriched_name
        raw_unit = compute_unit_pricing(clean_title, p_price)
        unit_info = re.sub(r"<[^>]+>", "", raw_unit).replace("단가계산:", "").replace("•", "").strip() if raw_unit else ""
        unit_short = unit_info.split("(")[0].strip() if unit_info else ""
        shipping_label = "로켓 무료배송" if it.get("isFreeShipping") is True else "로켓배송"

        tracked_url = register_tracked_link(
            p_url,
            deal_id=f"goldbox_{today_str}_{prod_id}",
            product_id=prod_id,
            source="goldbox",
            track="G",
            title=clean_title,
            sale_price=p_price,
            discount_rate=0,
        )
        evidence_note = f"{unit_short} · 오늘 자정 마감" if unit_short else "오늘 자정 마감"

        flyer_items.append({
            "deal_id": f"goldbox_{today_str}_{prod_id}",
            "product_id": prod_id,
            "source": "goldbox",
            "track": "G",
            "slot_label": slot_label,
            "category": classify_deal_category(clean_title, ""),
            "evidence_badge": "골드박스",
            "evidence_note": evidence_note,
            "clean_title": clean_title,
            "short_title": shorten_card_title(clean_title),
            "orig_price": 0,
            "sale_price": p_price,
            "discount_rate": 0,
            "unit_info": unit_info,
            "reaction_text": "",
            "shipping_fee": 0,
            "shipping_type": "free" if it.get("isFreeShipping") is True else "unknown",
            "shipping_condition": "",
            "shipping_label": shipping_label,
            "chip": build_card_chip(clean_title, 0, p_price, unit_info, shipping_label=shipping_label),
            "deeplink": tracked_url,
            "img_url": img_url,
            "votes": 0,
            "comments": 0,
        })

    if not flyer_items:
        seen.add(slot_key)
        save_seen(seen)
        return 0

    try:
        png_bytes = render_hotdeal_flyer(flyer_items, coupang_api=coupang_api)
        caption, inline_kb = format_flyer_caption_and_buttons(flyer_items)
    except Exception as e:
        logger.error(f"골드박스 전단지 렌더링 오류: {e}")
        return 0

    if not send_telegram_photo_bytes(CHANNEL_ID, png_bytes, caption, inline_kb):
        logger.warning("골드박스 전단지 텔레그램 전송 실패 — 다음 회차 재시도")
        return 0

    quota["goldbox_sent"] = gb_cnt + len(flyer_items)
    save_post_quota(quota)
    for item in flyer_items:
        if item.get("product_id"):
            mark_product_seen(item["product_id"], seen)
    seen.add(slot_key)
    save_seen(seen)
    logger.info(f"🍯 [골드박스({len(flyer_items)}건) 전단지 송출 완료] (슬롯={slot_label}, 금일 골드박스={quota['goldbox_sent']}건)")
    time.sleep(2)
    return len(flyer_items)


def load_sales_tracker() -> Dict[str, Any]:
    if os.path.exists(TRACKER_FILE):
        try:
            with open(TRACKER_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_sales_tracker(data: Dict[str, Any]):
    try:
        with open(TRACKER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"sales_tracker 저장 오류: {e}")


def check_and_notify_sales():
    """최근 4일(D-3~D) 주문 집계 감지로 쿠팡 D+1~D+2 지연 주문까지 누락 없이 실시간 알림"""
    now = datetime.now(KST)
    today_str = now.strftime("%Y%m%d")
    start_4d = (now - timedelta(days=3)).strftime("%Y%m%d")
    month_start = now.strftime("%Y%m01")
    month_str = now.strftime("%Y%m")
    target_channel = REPORT_CHANNEL_ID or ADMIN_CHAT_ID

    try:
        res = coupang_api.get_order_report(start_date=start_4d, end_date=today_str)
        items = res.get("data", []) or []

        tracker = load_sales_tracker()
        is_first_migration = "notified_order_keys" not in tracker
        tracker_changed = False

        if tracker.get("month") != month_str:
            tracker["month"] = month_str
            tracker["month_milestone_step"] = 0
            tracker.pop("milestone_retry_pending", None)
            tracker_changed = True

        if is_first_migration:
            seeded_keys: List[str] = []
            for it in items:
                k = f"{it.get('date')}_{it.get('orderId')}_{it.get('productId')}"
                if k not in seeded_keys:
                    seeded_keys.append(k)
            try:
                comm_res = coupang_api.get_commission_report(start_date=month_start, end_date=today_str)
                comm_rows = comm_res.get("data", []) or []
                m_gmv = sum(int(round(float(r.get("gmv", 0) or 0))) for r in comm_rows)
                tracker["month_milestone_step"] = m_gmv // 100000
            except Exception:
                pass
            tracker["notified_order_keys"] = seeded_keys[-500:]
            save_sales_tracker(tracker)
            logger.info(f"📦 sales_tracker 새 스키마 마이그레이션 완료 (최근 4일 기존 주문 {len(seeded_keys)}건 시드)")
            return

        notified_list: List[str] = list(tracker.get("notified_order_keys", []))
        notified_set = set(notified_list)

        new_items: List[Dict[str, Any]] = []
        new_keys: List[str] = []
        for it in items:
            k = f"{it.get('date')}_{it.get('orderId')}_{it.get('productId')}"
            if k not in notified_set:
                new_items.append(it)
                new_keys.append(k)
                notified_set.add(k)

        need_milestone_check = bool(new_items) or bool(tracker.get("milestone_retry_pending"))
        if not need_milestone_check:
            if tracker_changed:
                save_sales_tracker(tracker)
            return

        # 이번 달 누적 확정 실적 조회
        m_orders, m_gmv, m_comm, m_clicks = 0, 0, 0, 0
        comm_ok = False
        try:
            comm_res = coupang_api.get_commission_report(start_date=month_start, end_date=today_str)
            if (
                not isinstance(comm_res, dict)
                or str(comm_res.get("rCode")) != "0"
                or not isinstance(comm_res.get("data"), list)
                or not all(isinstance(r, dict) for r in comm_res["data"])
            ):
                raise RuntimeError(f"커미션 리포트 비정상 응답: {comm_res}")
            comm_rows = comm_res["data"]
            m_clicks = sum(int(r.get("click", 0) or 0) for r in comm_rows)
            m_orders = sum(int(r.get("order", 0) or 0) for r in comm_rows)
            m_gmv = sum(int(round(float(r.get("gmv", 0) or 0))) for r in comm_rows)
            m_comm = sum(int(round(float(r.get("commission", 0) or 0))) for r in comm_rows)
            comm_ok = True
        except Exception as e:
            logger.warning(f"월 누적 커미션 조회 실패: {e}")

        # 1) 새 주문 알림: 전송 성공 시에만 notified_order_keys에 기록
        if new_items:
            new_gmv = sum(int(round(float(it.get("gmv", 0) or 0))) for it in new_items)
            new_comm = sum(int(round(float(it.get("commission", 0) or 0))) for it in new_items)
            item_lines = []
            for it in new_items[:5]:
                d_raw = str(it.get("date") or "")
                d_fmt = f"{d_raw[4:6]}/{d_raw[6:8]}" if len(d_raw) == 8 else d_raw
                pname = str(it.get("productName") or "상품")[:30]
                qty = int(it.get("quantity", 1) or 1)
                g = int(round(float(it.get("gmv", 0) or 0)))
                c = int(round(float(it.get("commission", 0) or 0)))
                src_badge = format_order_source_badge(it.get("subId"))
                item_lines.append(f"• [{d_fmt}·{esc(src_badge)}] {esc(pname)} x{qty} (<b>{g:,}원</b> / 수수료 <b>{c:,}원</b>)")
            if len(new_items) > 5:
                item_lines.append(f"• 외 {len(new_items) - 5}건 추가")
            items_block = "\n".join(item_lines)

            msg = (
                f"🔔 <b>[쿠팡 파트너스 새 주문 집계!]</b> (+{len(new_items)}건)\n\n"
                f"{items_block}\n\n"
                f"• 신규 결제액 / 확정 수익: <b>{new_gmv:,}원</b> / <b>{new_comm:,}원</b>\n"
                f"• {now.month}월 누적: <b>{m_orders:,}건</b> | <b>{m_gmv:,}원</b> | 수익 <b>{m_comm:,}원</b> (클릭 {m_clicks:,}회)\n"
                f"※ <i>쿠팡 API는 D+1~D+2 지연 집계 기준입니다.</i>"
            )
            order_ok = bool(target_channel) and bool(send_telegram(chat_id=target_channel, text=msg))
            if order_ok:
                notified_list.extend(new_keys)
                tracker["notified_order_keys"] = notified_list[-500:]
                tracker_changed = True
                logger.info(f"📢 새 주문 알림 발송: +{len(new_items)}건 ({new_gmv:,}원 / 수수료 {new_comm:,}원)")
            else:
                logger.warning(f"⚠️ 새 주문 알림 전송 실패 ({len(new_items)}건) — 다음 회차 재시도")

        # 2) 매출 마일스톤 알림: 자기 전송 결과에 따라 독립적으로 단계/재시도 플래그 갱신
        if comm_ok:
            cur_step = m_gmv // 100000
            last_step = int(tracker.get("month_milestone_step", 0) or 0)
            if cur_step > last_step and cur_step > 0:
                target_amount = cur_step * 100000
                milestone_msg = (
                    f"🏆 <b>[월 누적 판매액 마일스톤 달성!]</b>\n\n"
                    f"💰 {now.month}월 누적 결제액 <b>{target_amount:,}원</b>을 돌파했습니다!\n"
                    f"• {now.month}월 누적 결제액: <b>{m_gmv:,}원</b>\n"
                    f"• {now.month}월 확정 수익: <b>{m_comm:,}원</b>\n"
                    f"• 총 주문 건수: <b>{m_orders:,}건</b>\n\n"
                    f"📈 다음 목표({(cur_step+1)*10:,}만원)를 향해 순항 중! 🔥"
                )
                milestone_ok = bool(target_channel) and bool(send_telegram(chat_id=target_channel, text=milestone_msg))
                if milestone_ok:
                    logger.info(f"🏆 월 누적 마일스톤 알림 발송: {target_amount:,}원 돌파")
                    tracker["month_milestone_step"] = cur_step
                    tracker.pop("milestone_retry_pending", None)
                    tracker_changed = True
                else:
                    logger.warning(f"⚠️ 월 누적 마일스톤 알림 전송 실패 ({target_amount:,}원) — 다음 회차 재시도")
                    if not tracker.get("milestone_retry_pending"):
                        tracker["milestone_retry_pending"] = True
                        tracker_changed = True
            elif tracker.pop("milestone_retry_pending", None) is not None:
                tracker_changed = True
        else:
            if not tracker.get("milestone_retry_pending"):
                tracker["milestone_retry_pending"] = True
                tracker_changed = True
            logger.warning("⚠️ 월 누적 실적 조회 실패로 마일스톤 확인을 다음 회차에 재시도합니다.")

        if tracker_changed:
            save_sales_tracker(tracker)

    except Exception as e:
        logger.error(f"판매 모니터링 오류: {e}")


def run_daily_report_if_night(seen: Set[str]) -> int:
    now = datetime.now(KST)
    today_str = now.strftime("%Y%m%d")
    report_key = f"report_{today_str}"

    if "--force-report" not in sys.argv:
        if now.hour != 23:
            return 0
        if report_key in seen:
            return 0

    logger.info("📊 일일·월간 파트너스 결산 리포트 생성 중...")
    month_start = now.strftime("%Y%m01")
    start_4d = (now - timedelta(days=3)).strftime("%Y%m%d")

    try:
        comm_res = coupang_api.get_commission_report(start_date=month_start, end_date=today_str)
        comm_rows = comm_res.get("data", []) or []

        order_res = coupang_api.get_order_report(start_date=start_4d, end_date=today_str)
        order_items = order_res.get("data", []) or []

        stats = summarize_partner_stats(comm_rows, order_items)
        m_tot = stats["total"]
        m_clicks = m_tot["clicks"]
        m_orders = m_tot["orders"]
        m_cancels = m_tot["cancels"]
        m_gmv = m_tot["gmv"]
        m_comm = m_tot["commission"]

        # 2. 최근 확정일 집계 (D+1~D+2 지연 반영된 가장 최신 날짜)
        latest_date = stats["latest_date"]
        if latest_date:
            l_tot = stats["latest_total"]
            l_clicks = l_tot["clicks"]
            l_orders = l_tot["orders"]
            l_cancels = l_tot["cancels"]
            l_gmv = l_tot["gmv"]
            l_comm = l_tot["commission"]
            l_fmt = f"{latest_date[:4]}.{latest_date[4:6]}.{latest_date[6:8]}" if len(latest_date) == 8 else latest_date

            matched_prods = [str(it.get("productName") or "")[:28] for it in order_items if str(it.get("date") or "") == latest_date and it.get("productName")]
            prod_note = f"\n• <b>주문 품목</b>: {esc(', '.join(matched_prods[:3]))}" if matched_prods else ""
            latest_block = (
                f"📌 <b>최근 확정일 ({l_fmt})</b>\n"
                f"• 클릭/주문/취소: <b>{l_clicks:,}회</b> / <b>{l_orders:,}건</b> / {l_cancels:,}건\n"
                f"• 결제 금액: <b>{l_gmv:,}원</b>\n"
                f"• 확정 수익: <b>{l_comm:,}원</b>{prod_note}\n\n"
            )
        else:
            latest_block = "📌 <b>최근 확정일</b>: 집계된 내역 없음\n\n"

        channel_split_block = format_channel_split_block(stats, f"채널별 분리 집계 ({now.month}월 누적)")
        click_stats_block = ""
        try:
            click_stats = fetch_click_stats(days=7, limit=5)
            if click_stats:
                click_stats_block = f"\n\n{format_click_stats_telegram(click_stats, top_n=3)}"
        except Exception as cs_err:
            logger.warning(f"실시간 클릭 통계 조회 생략: {cs_err}")

        report_msg = (
            f"📊 <b>[쿠팡 파트너스 일일·월간 결산]</b>\n"
            f"📅 리포트 일자: {now.strftime('%Y년 %m월 %d일')}\n\n"
            f"{latest_block}"
            f"🗓️ <b>{now.month}월 누적 ({now.month}/1 ~ {now.month}/{now.day})</b>\n"
            f"• 총 클릭 수: <b>{m_clicks:,}회</b>\n"
            f"• 총 주문 / 취소: <b>{m_orders:,}건</b> / {m_cancels:,}건\n"
            f"• 총 결제 금액: <b>{m_gmv:,}원</b>\n"
            f"• <b>총 확정 수익</b>: <b>{m_comm:,}원</b>\n\n"
            f"{channel_split_block}{click_stats_block}\n\n"
            f"※ <i>쿠팡 파트너스 API는 D+1~D+2 지연 집계 기준입니다.</i>"
        )

        target_channel = REPORT_CHANNEL_ID or ADMIN_CHAT_ID
        if send_telegram(chat_id=target_channel, text=report_msg):
            seen.add(report_key)
            save_seen(seen)
            logger.info(f"✅ 일일·월간 결산 리포트 전송 완료 (최근확정={latest_date}, 월수익={m_comm:,}원)")
            return 1
    except Exception as e:
        logger.error(f"결산 리포트 조회 오류: {e}")
    return 0



def _hidden_subp_kwargs():
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        sinfo = subprocess.STARTUPINFO()
        sinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        sinfo.wShowWindow = 0
        kwargs["startupinfo"] = sinfo
    return kwargs

def update_pc_heartbeat():
    """로컬 PC 실행 시 깃허브에 생존신고 타임스탬프 전송 (깃허브 액션 중복 실행 방지용)"""
    if os.getenv("GITHUB_ACTIONS"):
        return
    try:
        import subprocess
        now_ts = str(int(time.time()))
        subprocess.run(
            ["gh", "variable", "set", "PC_HEARTBEAT", "--body", now_ts],
            cwd=BASE_DIR,
            capture_output=True,
            timeout=5,
            **_hidden_subp_kwargs(),
        )
    except Exception:
        pass


def sync_git_state():
    """로컬 실행 시 깃허브 액션이 올렸을 수 있는 최신 seen_deals를 pull 동기화 (충돌 시 즉시 rebase abort로 파일 훼손 방지)"""
    if os.getenv("GITHUB_ACTIONS"):
        return
    try:
        res = subprocess.run(["git", "pull", "--rebase"], cwd=BASE_DIR, capture_output=True, timeout=10, **_hidden_subp_kwargs())
        if res.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], cwd=BASE_DIR, capture_output=True, timeout=5, **_hidden_subp_kwargs())
    except Exception:
        pass


def sync_git_push():
    """로컬에서 신규 딜 적재·송출 완료 시 깃허브로 즉시 push 동기화"""
    if os.getenv("GITHUB_ACTIONS"):
        return
    try:
        files = [f for f in ("seen_deals.json", "sales_tracker.json", "post_quota.json", "flyer_queue.json") if os.path.exists(os.path.join(BASE_DIR, f))]
        if not files:
            return
        subprocess.run(["git", "add"] + files, cwd=BASE_DIR, capture_output=True, timeout=5, **_hidden_subp_kwargs())
        subprocess.run(
            ["git", "commit", "-m", "chore: sync seen deals from local runner [skip ci]"],
            cwd=BASE_DIR,
            capture_output=True,
            timeout=5,
            **_hidden_subp_kwargs(),
        )
        subprocess.run(["git", "push"], cwd=BASE_DIR, capture_output=True, timeout=10, **_hidden_subp_kwargs())
    except Exception:
        pass


def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN이 없습니다.")
        sys.exit(1)

    if not acquire_process_lock():
        logger.info("⏳ 이미 다른 deal_pusher 프로세스가 실행 중이므로 중복 실행을 건너뜁니다.")
        return

    update_pc_heartbeat()
    sync_git_state()

    logger.info("🚀 핫딜 수집 및 전단지 송출 작업 시작 (투트랙 선별 · 딜바다 포함 4개 출처 라운드로빈 · v5 빅타이포 전단지)...")
    seen = load_seen()
    quota = load_post_quota()
    flyer_queue = load_flyer_queue()
    ocr_cache = load_ocr_cache()
    price_db = get_price_db()
    expired_waits = price_db.pop_expired_waits()
    if expired_waits:
        for ew in expired_waits:
            post_id = str(ew.get("post_id") or "")
            if post_id:
                seen.add(post_id)
                _drop_ocr_cache_entry(ocr_cache, post_id)
        save_seen(seen)
        logger.info(f"🧹 [대기열 정리] 만료된 대기 딜 {len(expired_waits)}건 seen 영구 제외 및 정리 완료")
    alerts_cache = list_keyword_alerts() or []
    notified_deal_ids: Set[str] = set()
    run_stats = {"queued": 0, "sent": 0}

    def _remaining_daily_room() -> int:
        c = int(quota.get("community_sent", 0) or 0)
        g = int(quota.get("goldbox_sent", 0) or 0)
        return min(DAILY_MAX_COMMUNITY_DEALS - c, DAILY_MAX_TOTAL_DEALS - c - g)

    if is_quiet_hours():
        logger.info("🌙 [야간 방해금지] 00:00~07:00 구간 — 커뮤니티 수집 및 송출 생략")
    elif _remaining_daily_room() <= len(flyer_queue):
        logger.info(
            f"⏸️ [송출 제한] 금일 커뮤니티 한도 도달 또는 대기큐 포화 "
            f"(송출={quota['community_sent']}/{DAILY_MAX_COMMUNITY_DEALS}, 큐={len(flyer_queue)})"
        )
    else:
        sources = (
            (collect_ruliweb_deals, parse_ruliweb_detail, "ruliweb"),
            (collect_ppomppu_deals, parse_ppomppu_detail, "ppomppu"),
            (collect_arcalive_deals, parse_arcalive_detail, "arca"),
            (collect_dealbada_deals, parse_dealbada_detail, "dealbada"),
        )
        collected_by_source: List[Tuple[str, Any, List[Dict[str, Any]]]] = []
        for collector, parser, source_name in sources:
            cd = price_db.get_source_cooldown(source_name)
            if cd:
                logger.info(
                    f"⏸️ [{source_name}] 쿨다운 중 (HTTP {cd.get('status_code')}, ~{cd.get('cooldown_until')}) — 목록 수집 생략"
                )
                continue
            try:
                rows = collector()
            except Exception as e:
                logger.error(f"[{source_name}] 목록 수집 오류: {e}")
                rows = []
            collected_by_source.append((source_name, parser, rows))

        iterators = {
            src: [row for row in rows if row.get("id") not in seen]
            for src, _, rows in collected_by_source
        }
        parser_map = {src: parser for src, parser, _ in collected_by_source}
        attempts_by_source = {src: 0 for src, _, _ in collected_by_source}
        active_sources = [src for src, _, _ in collected_by_source if iterators.get(src)]

        while active_sources:
            if (run_stats["queued"] + run_stats["sent"]) >= MAX_COMMUNITY_PER_RUN or len(flyer_queue) >= _remaining_daily_room():
                break
            next_active: List[str] = []
            for src in active_sources:
                if (run_stats["queued"] + run_stats["sent"]) >= MAX_COMMUNITY_PER_RUN or len(flyer_queue) >= _remaining_daily_room():
                    break
                if price_db.get_source_cooldown(src):
                    logger.info(f"⏸️ [{src}] 회차 중 쿨다운 진입 — 추가 후보 조회 중단")
                    continue
                if attempts_by_source[src] >= MAX_PER_SOURCE_PER_RUN:
                    continue
                q_rows = iterators[src]
                processed_one = False
                while q_rows and not processed_one:
                    cand = q_rows.pop(0)
                    wait_key = f"{src}:{cand['id']}"
                    skip_wait, skip_reason = price_db.should_skip_wait_recheck(wait_key, WAIT_RECHECK_MIN_MINUTES)
                    if skip_wait:
                        if skip_reason == "expired":
                            seen.add(cand["id"])
                            save_seen(seen)
                            _drop_ocr_cache_entry(ocr_cache, cand["id"])
                            price_db.delete_pending_wait(wait_key)
                        continue
                    attempts_by_source[src] += 1
                    process_deal(
                        cand,
                        parser_map[src],
                        src,
                        seen,
                        flyer_queue=flyer_queue,
                        quota=quota,
                        run_stats=run_stats,
                        ocr_cache=ocr_cache,
                        alerts_cache=alerts_cache,
                        notified_deal_ids=notified_deal_ids,
                    )
                    processed_one = True
                if price_db.get_source_cooldown(src):
                    logger.info(f"⏸️ [{src}] 상세 조회 중 차단/429 감지 — 동일 회차 남은 후보 중단")
                    continue
                if q_rows and attempts_by_source[src] < MAX_PER_SOURCE_PER_RUN:
                    next_active.append(src)
            active_sources = next_active

    queued_this_run = run_stats["queued"]
    total_sent = run_stats["sent"]

    # 전단지 큐 플러시 (4건 모이면 즉시, 미만 시 대기 타임아웃/긴급할인 조건에 따라 송출)
    total_sent += flush_flyer_queue(flyer_queue, quota)

    # 로켓 골드박스 생필품·식품 미끼 4선 전단지 (오전 8~11시 · 저녁 18~21시 2타임)
    if ENABLE_GOLDBOX or "--force-goldbox" in sys.argv:
        total_sent += run_goldbox_staggered(seen, quota)

    # 최근 4일 주문 집계 감지 및 마일스톤 알림
    check_and_notify_sales()

    # 밤 23시 일일·월간 결산 리포트 (하루 1회만 발송)
    total_sent += run_daily_report_if_night(seen)

    if total_sent > 0 or queued_this_run > 0:
        sync_git_push()

    logger.info(f"🎉 모든 작업 완료! (금회 전단지 큐 적재 {queued_this_run}건 / 채널 송출 {total_sent}건)")


if __name__ == "__main__":
    main()
