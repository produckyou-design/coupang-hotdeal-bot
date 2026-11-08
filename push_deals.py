import html
import io
import json
import logging
import os
import re
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

from coupang_api import CoupangPartnersAPI

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 파일 로깅 및 콘솔 로깅 동시 지원 (배경 실행 시에도 deal_pusher.log에 실시간 기록)
log_file = os.path.join(BASE_DIR, "deal_pusher.log")
handlers: List[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
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
LOCK_FILE_PATH = os.path.join(BASE_DIR, "deal_pusher.lock")
_LOCK_HANDLE = None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"
}

CLOSED_KEYWORDS = ["이미 정상화", "가격 정상화", "종료", "품절", "마감", "솔드아웃", "sold out"]

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
    try:
        recent = list(seen)[-1000:]
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(recent, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"seen_deals 저장 에러: {e}")


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
    # 1) Windows 환경: winocr (단어 바운딩박스 기준 정확한 상->하 정렬)
    try:
        import winocr
        import asyncio

        async def _winocr():
            return await winocr.recognize_pil(pil_img, lang="ko")

        res_obj = asyncio.run(_winocr())
        if res_obj and getattr(res_obj, "lines", None):
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
    except Exception:
        pass

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
            logger.debug(f"OCR 추출 실패 ({u}): {e}")

    return "\n===NEXT_IMAGE===\n".join(collected_blocks)


# ==========================================
# 상품명 정제 및 가성비 단가 계산
# ==========================================
def clean_display_title(raw_title: str) -> str:
    """커뮤니티 말머리/가격/배송비 꼬리표를 제거하고 깔끔한 실제 상품명만 남김"""
    t = raw_title.strip()
    t = re.sub(r"^뽐뿌::\s*", "", t)
    t = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", t)
    # 끝에 붙은 댓글 수 제거: [12]
    t = re.sub(r"\s*\[\d+\]\s*$", "", t)
    # 끝에 붙은 (가격/배송) 괄호 제거: (23,100원/무료), (89,890/무료), (13,600원) (무배)
    for _ in range(3):
        t = re.sub(
            r"\s*\([^\)]*?(?:[\d,]{4,}\s*원?|무료|무배|와우|로켓|배송)[^\)]*?\)\s*$",
            "",
            t,
            flags=re.IGNORECASE,
        )
    # 슬래시 구분 가격 꼬리표 제거: / 9,370원(로켓무배) / 11% 세일, /17,220원 와우무배
    t = re.sub(r"\s*/\s*[\d,]+\s*원.*$", "", t)
    # 끝에 단독으로 붙은 가격+무배 꼬리표 제거: 66030원
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

    counts = re.findall(
        r"(\d+)\s*(개입|개|봉|팩|캔|병|포|박스|롤|통|입|매|세트|정|캡슐|알)(?![a-zA-Z])",
        name,
        re.IGNORECASE,
    )
    weights = re.findall(
        r"(\d+(?:\.\d+)?)\s*(kg|l|리터|킬로|g|ml|그램)(?![a-zA-Z])",
        name,
        re.IGNORECASE,
    )

    count_val = 0
    count_unit = ""
    if len(counts) >= 2:
        c1, u1 = int(counts[0][0]), counts[0][1]
        c2, u2 = int(counts[-1][0]), counts[-1][1]
        # 예: '5개입 1팩' -> 5개, '24개입 2개' -> 48개, '60정 2개' -> 120정
        if c2 == 1 and c1 > 1:
            count_val = c1
            count_unit = "개" if u1 == "개입" else u1
        elif c1 > 1 and c2 > 1 and u1 in ("개입", "입", "매", "정", "캡슐", "알", "롤", "포"):
            count_val = c1 * c2
            count_unit = "개" if u1 in ("개입", "입") else u1
        else:
            count_val = c2
            count_unit = "개" if u2 == "개입" else u2
    elif len(counts) == 1:
        count_val = int(counts[0][0])
        count_unit = "개" if counts[0][1] == "개입" else counts[0][1]

    total_amount = 0.0
    amount_unit = "g"
    if weights:
        w_val = float(weights[0][0])
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


def _extract_clean_won_numbers(text: str, blacklisted_nums: Set[int]) -> List[int]:
    """정상 표기된 원화 금액 추출 (OCR 콤마 뒤 공백 '23, 100원' 포함)"""
    s = _strip_inline_non_prices(text)
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
    s = _strip_inline_non_prices(text)
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


def parse_pricing_details(title: str, body: str, ocr_text: str = "") -> Dict[str, Any]:
    """
    제목, 본문, 이미지 OCR 헤더에서 명시적으로 적힌 정상가·할인가·할인율만 정밀 추출.
    [절대 원칙]
    1. 정가 역산(sale_price / (1 - r)) 절대 금지
    2. 임의 쿠폰 차감(sale_price - coupon) 절대 금지
    3. 할부금(월 XXX원~), 적립금(쿠팡캐시), 보험료(AppleCare+/안심케어), 옵션추가금(+XXX원) 원천 배제
    """
    # 0. 배송비 기준금액 블랙리스트
    blacklisted_prices: Set[int] = {19800, 29800, 30000, 50000}
    for sm in re.finditer(r"(?:로켓배송|배송|주문|무료배송)?\s*([\d,]+)\s*원\s*(?:이상|미만)", f"{body}\n{ocr_text}"):
        try:
            blacklisted_prices.add(int(sm.group(1).replace(",", "")))
        except Exception:
            pass

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
    title_clean = _strip_inline_non_prices(title)
    # 제목에 화살표(10,570원 -> 9,370원)가 있으면 뒤쪽 가격이 할인가
    m_title_arrow = re.search(r"([\d,]+)\s*원?\s*(?:에서|->|→|~>)\s*([\d,]+)\s*원", title_clean)
    if m_title_arrow:
        p_to = int(m_title_arrow.group(2).replace(",", ""))
        if p_to >= 1000:
            sale_price = p_to

    if sale_price == 0:
        t_won = [
            int(p.replace(",", ""))
            for p in re.findall(r"(?<![+\-\d월])([\d,]+)\s*원", title_clean)
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
    body_clean = _strip_inline_non_prices(body)
    m_body_arrow = re.search(r"([\d,]+)\s*원?\s*(?:에서|->|→|~>)\s*([\d,]+)\s*원", body_clean)
    if sale_price == 0 and m_body_arrow:
        p_to = int(m_body_arrow.group(2).replace(",", ""))
        if p_to >= 1000 and p_to not in blacklisted_prices:
            sale_price = p_to

    if sale_price == 0:
        for pat in (
            r"(?:가격|특가|판매가|할인가|쿠팡가|와우가|실구매가)\s*[:\s]*([\d,]+)\s*원",
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
            if re.search(r"(?:정가|정상가|기존가|출고가|시중가|쿠폰할인|원\s*할인|체감)", line):
                continue
            b_nums = _extract_clean_won_numbers(line, blacklisted_prices)
            b_nums = [n for n in b_nums if n not in (2500, 3000)]
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

    # 커뮤니티 반응 (URL, 테이블 헤더, 단순 감상평 배제)
    community_reactions: List[str] = []
    for line in body.splitlines():
        ln = line.strip()
        if not ln or ln.startswith("http") or len(ln) < 5:
            continue
        if any(bad in ln for bad in ["맛있", "맛나", "좋네", "샀습", "추천합", "감사", "양도 많고", "구매링크", "쇼핑몰"]):
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
    }


def send_telegram(
    chat_id: str,
    text: str,
    photo_url: Optional[str] = None,
    button_text: Optional[str] = None,
    button_url: Optional[str] = None,
) -> bool:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "parse_mode": "HTML",
    }
    if button_text and button_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": button_text, "url": button_url}]]
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
                return True
        except Exception as e:
            logger.warning(f"사진 전송 실패, 텍스트 전송으로 자동 전환 ({photo_url[:50]}...): {e}")

    # 2. 사진 없거나 사진 전송 실패 시 일반 텍스트 메시지로 자동 안전 전송
    try:
        t_payload = dict(payload)
        t_payload["text"] = text
        res = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=t_payload, timeout=10
        ).json()
        return res.get("ok", False)
    except Exception as e:
        logger.error(f"텔레그램 메시지 전송 최종 실패: {e}")
        return False


# ==========================================
# 1. 루리웹 핫딜 수집기
# ==========================================
def collect_ruliweb_deals(max_pages: int = 2) -> List[Dict[str, Any]]:
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
                continue

            soup = BeautifulSoup(res.text, "html.parser")
            for row in soup.select("tr.table_body"):
                if row.select_one("span.strike, del"):
                    continue
                s = row.select_one("a.subject_link, a.deco")
                if not s:
                    continue
                title = " ".join(s.text.split())
                if "쿠팡" not in title and "coupang" not in title.lower():
                    continue
                if is_closed_deal(title):
                    continue

                link = s.get("href", "")
                m = re.search(r"/read/(\d+)", link)
                pid = f"ruliweb_{m.group(1)}" if m else link
                if not any(d["id"] == pid for d in deals):
                    deals.append({"id": pid, "title": title, "link": link})
        except Exception as e:
            logger.error(f"루리웹 수집 오류 ({url}): {e}")

    return deals


def parse_ruliweb_detail(url: str) -> Optional[Dict[str, Any]]:
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        if res.status_code != 200:
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
            body_text = vc.text.strip()
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
        }
    except Exception as e:
        logger.error(f"루리웹 상세 파싱 오류 ({url}): {e}")
        return None


# ==========================================
# 2. 뽐뿌 핫딜 수집기
# ==========================================
def collect_ppomppu_deals(max_pages: int = 2) -> List[Dict[str, Any]]:
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
                continue
            r.encoding = "euc-kr"
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a"):
                href = a.get("href", "")
                if "view.php?id=ppomppu&" in href and a.text.strip():
                    # 종료된 딜(취소선/종료 아이콘) 배제
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
                            deals.append({"id": deal_id, "title": title, "link": full_url})
            time.sleep(0.2)
        except Exception as e:
            logger.error(f"뽐뿌 수집 오류 ({url}): {e}")
    return deals


def parse_ppomppu_detail(url: str) -> Optional[Dict[str, Any]]:
    try:
        time.sleep(0.25)
        res = requests.get(url, headers=HEADERS, timeout=7)
        if res.status_code != 200:
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
            try:
                head_res = requests.head(cp_url, headers=HEADERS, allow_redirects=True, timeout=4)
                if "coupang.com" in head_res.url:
                    cp_url = head_res.url
            except Exception:
                pass

        body_text = ""
        content_el = soup.select_one("td.board-contents, td.han")
        if content_el:
            body_text = content_el.text.strip()

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
        }
    except Exception as e:
        logger.error(f"뽐뿌 상세 파싱 오류 ({url}): {e}")
        return None


# ==========================================
# 3. 아카라이브 핫딜 수집기
# ==========================================
def collect_arcalive_deals() -> List[Dict[str, Any]]:
    """아카라이브 핫딜 채널 쿠팡 실시간 검색 수집"""
    deals = []
    import urllib.parse
    kw = urllib.parse.quote("쿠팡")
    url = f"https://arca.live/b/hotdeal?target=all&keyword={kw}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return deals
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
            title = " ".join(a.text.split())
            title = re.sub(r"\s*\[\d+\]$", "", title).strip()
            if not title or is_closed_deal(title):
                continue
            deal_id = f"arca_{m.group(1)}"
            full_url = f"https://arca.live/b/hotdeal/{m.group(1)}"
            if not any(d["id"] == deal_id for d in deals):
                deals.append({"id": deal_id, "title": title, "link": full_url})
    except Exception as e:
        logger.error(f"아카라이브 수집 오류: {e}")
    return deals


def parse_arcalive_detail(url: str) -> Optional[Dict[str, Any]]:
    """아카라이브 핫딜 본문 파싱 (종료 딜 필터링 + 전용 테이블 정밀 추출)"""
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        if res.status_code != 200:
            return None
        soup = BeautifulSoup(res.text, "html.parser")

        title_row = soup.select_one("div.title-row")
        if title_row:
            if title_row.select_one(".deal-close, del, s") or is_closed_deal(title_row.text):
                return None

        cp_url = None
        extracted_title = ""
        extracted_price = 0

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
                elif "가격" in lbl and val:
                    m = re.search(r"([\d,]+)\s*원?", val)
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
            body_text = body_el.text.strip()

        return {
            "coupang_url": cp_url,
            "body_text": body_text,
            "img_url": img_urls[0] if img_urls else None,
            "ocr_img_urls": img_urls[:2],
            "extracted_title": extracted_title,
            "extracted_price": extracted_price,
        }
    except Exception as e:
        logger.error(f"아카라이브 상세 파싱 오류 ({url}): {e}")
        return None


# ==========================================
# 4. 핫딜 송출 엔진
# ==========================================
def format_deal_message(clean_title: str, p_info: Dict[str, Any]) -> str:
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
    if 10 <= disc_rate <= 85 and orig_p > best_price > 0:
        header_line = f"🔥 <b>[{disc_rate}% 파격할인] 정상가 <s>{orig_p:,}원</s> ➡️ 특가 {best_price:,}원!</b>"

    price_lines = []
    if orig_p > best_price > 0:
        save_amt = orig_p - best_price
        price_lines.append(f"• <b>정상가격</b>: <s>{orig_p:,}원</s> (<b>{save_amt:,}원 절약!</b>)")

    if best_price > 0:
        disc_text = f" (🔥 <b>{disc_rate}% 할인</b>)" if (3 <= disc_rate <= 85 and orig_p > best_price) else ""
        price_lines.append(f"• <b>최종특가</b>: <b>{best_price:,}원</b>{disc_text}")
    else:
        price_lines.append("• <b>최종특가</b>: <b>한정수량 최저가</b>")

    if p_info["unit_info"]:
        price_lines.append(f"• <b>단가계산</b>: <b>{p_info['unit_info']}</b>")

    price_block = "\n".join(price_lines)
    reaction_block = (
        f"💬 <b>커뮤니티 반응</b>: <code>{esc(p_info['reaction_text'])}</code>\n\n"
        if p_info["reaction_text"]
        else ""
    )

    return (
        f"{header_line}\n\n"
        f"📌 <b>{esc(clean_title)}</b>\n\n"
        f"💵 <b>특가 정보</b>\n"
        f"{price_block}\n\n"
        f"{reaction_block}"
        f"🚚 🚀 <b>로켓배송 / 무료배송</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>핫딜 특성상 가격 변동 및 조기 품절될 수 있습니다.</i>\n"
        f"※ 이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
    )


def process_deal(d: Dict[str, Any], detail_parser, source_name: str, seen: Set[str]) -> bool:
    deal_id = d["id"]
    if deal_id in seen:
        return False

    detail = detail_parser(d["link"])
    if not detail or not detail.get("coupang_url"):
        seen.add(deal_id)
        save_seen(seen)
        return False

    raw_url = detail["coupang_url"]
    body_text = detail.get("body_text", "")
    img_url = detail.get("img_url")
    ocr_imgs = detail.get("ocr_img_urls") or ([img_url] if img_url else [])

    base_title = detail.get("extracted_title") or d["title"]
    if is_closed_deal(base_title):
        seen.add(deal_id)
        save_seen(seen)
        return False

    pricing_title = base_title
    if detail.get("extracted_price") and detail["extracted_price"] >= 1000:
        if f"{detail['extracted_price']:,}" not in pricing_title and str(detail["extracted_price"]) not in pricing_title:
            pricing_title = f"{pricing_title} ({detail['extracted_price']:,}원)"

    clean_title = clean_display_title(base_title)

    # 이미지 OCR 가격 헤더 추출 및 팩트 기반 가격 분석
    ocr_text = extract_ocr_text(ocr_imgs)
    p_info = parse_pricing_details(pricing_title, body_text, ocr_text)

    if p_info["sale_price"] < 1000:
        logger.info(f"⏭️ [스킵] 유효 가격 식별 불가: {base_title}")
        seen.add(deal_id)
        save_seen(seen)
        return False

    # 파트너스 딥링크 생성
    try:
        res = coupang_api.create_deeplink([raw_url], sub_id=f"auto_{source_name}")
        items = res.get("data", [])
        if not items:
            seen.add(deal_id)
            save_seen(seen)
            return False
        deeplink = items[0].get("shortenUrl") or items[0].get("landingUrl")
    except Exception as e:
        logger.error(f"딥링크 생성 실패: {e}")
        return False

    msg = format_deal_message(clean_title, p_info)

    ok = send_telegram(
        chat_id=CHANNEL_ID,
        text=msg,
        photo_url=img_url,
        button_text="👉 실시간 특가 및 수량 확인하기",
        button_url=deeplink,
    )

    if ok:
        seen.add(deal_id)
        save_seen(seen)
        logger.info(
            f"✅ [{source_name}] 송출 완료: {clean_title} "
            f"(정가={p_info['orig_price']:,}원, 특가={p_info['final_price']:,}원, 할인율={p_info['discount_rate']}%)"
        )
        time.sleep(2)
        return True

    return False


# ==========================================
# 5. 아침 골드박스 & 밤 23시 결산 리포트
# ==========================================
def run_goldbox_if_morning(seen: Set[str]) -> int:
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    today_key = f"gb_{now.strftime('%Y%m%d')}"

    if "--force-goldbox" not in sys.argv:
        if today_key in seen:
            return 0
        if now.hour < 7:
            return 0

    logger.info("🍯 아침 골드박스 상위 5개 자동 송출 시작...")
    res = coupang_api.get_goldbox_products(sub_id="auto_goldbox")
    items = res.get("data", [])
    if not items:
        return 0

    sent = 0
    for i, it in enumerate(items[:5]):
        raw_name = it.get("productName", "")
        p_price = int(float(it.get("productPrice", 0)))
        p_url = it.get("productUrl", "")
        img_url = it.get("productImage")
        cat = it.get("categoryName", "")

        p_name = enrich_product_spec(raw_name, p_url, img_url)
        unit_str = compute_unit_pricing(p_name, p_price)

        msg = (
            f"🍯 <b>[쿠팡 공식 골드박스] 👑 와우회원 단독 하루 특가</b>\n"
            f"🏆 <b>오늘의 골드박스 랭킹 {i+1}위!</b>\n\n"
            f"📌 <b>{esc(p_name)}</b>\n\n"
            f"💵 <b>특가 정보</b>\n"
            f"• <b>골드특가</b>: <b>{p_price:,}원</b> (와우회원 한정가)\n"
            f"{unit_str}"
            f"• <b>카테고리</b>: {esc(cat)}\n"
            f"⏰ <b>특가시간</b>: 오늘 자정(24:00) 마감 또는 한정수량 소진 시 종료\n\n"
            f"🚚 🚀 <b>로켓배송 (내일 도착 보장)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"💡 <i>쿠팡 공식 엄선 당일 최저가 상품입니다.</i>\n"
            f"※ 이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
        )

        if send_telegram(CHANNEL_ID, msg, img_url, "👉 실시간 수량 및 최저가 확인", p_url):
            sent += 1
            time.sleep(2)

    if sent > 0:
        seen.add(today_key)
        save_seen(seen)
    return sent


def load_sales_tracker() -> Dict[str, Any]:
    if os.path.exists(TRACKER_FILE):
        try:
            with open(TRACKER_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"date": "", "last_orders": 0, "last_gmv": 0, "last_milestone_step": 0}


def save_sales_tracker(data: Dict[str, Any]):
    try:
        with open(TRACKER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"sales_tracker 저장 오류: {e}")


def check_and_notify_sales():
    """실시간 새 주문 감지 및 누적 판매액 10만원 단위 마일스톤 알림"""
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    today_str = now.strftime("%Y%m%d")
    target_channel = REPORT_CHANNEL_ID or ADMIN_CHAT_ID

    try:
        res = coupang_api.get_order_report(start_date=today_str, end_date=today_str)
        items = res.get("data", [])
        cur_orders = len(items)
        cur_gmv = sum(int(it.get("gmv", 0)) for it in items)
        est_comm = round(cur_gmv * 0.03)

        tracker = load_sales_tracker()
        if tracker.get("date") != today_str:
            tracker = {"date": today_str, "last_orders": 0, "last_gmv": 0, "last_milestone_step": 0}

        last_orders = tracker.get("last_orders", 0)
        last_step = tracker.get("last_milestone_step", 0)

        if cur_orders > last_orders:
            new_count = cur_orders - last_orders
            diff_gmv = cur_gmv - tracker.get("last_gmv", 0)
            msg = (
                f"🔔 <b>[새 주문 체결 알림!]</b>\n\n"
                f"📦 오늘 <b>{cur_orders}번째 주문</b>이 발생했습니다! (+{new_count}건)\n"
                f"• 신규 체결액: <b>약 {diff_gmv:,}원</b>\n"
                f"• 오늘 누적 판매액: <b>{cur_gmv:,}원</b>\n"
                f"• 오늘 예상 수익: <b>약 {est_comm:,}원</b> (3%)\n\n"
                f"🎉 축하합니다! 실시간 주문이 계속 늘어나고 있습니다. 🚀"
            )
            send_telegram(chat_id=target_channel, text=msg)
            logger.info(f"📢 새 주문 알림 발송: {cur_orders}건 누적")

        cur_step = cur_gmv // 100000
        if cur_step > last_step and cur_step > 0:
            target_amount = cur_step * 100000
            milestone_msg = (
                f"🏆 <b>[판매액 마일스톤 달성!]</b>\n\n"
                f"💰 오늘 누적 판매액 <b>{target_amount:,}원</b>을 돌파했습니다!\n"
                f"• 현재 누적 판매액: <b>{cur_gmv:,}원</b>\n"
                f"• 현재 예상 수익: <b>약 {est_comm:,}원</b>\n"
                f"• 총 체결 건수: <b>{cur_orders}건</b>\n\n"
                f"📈 대박 행진 중! 다음 목표({(cur_step+1)*10:,}만원)를 향해 달립니다! 🔥"
            )
            send_telegram(chat_id=target_channel, text=milestone_msg)
            logger.info(f"🏆 마일스톤 알림 발송: {target_amount:,}원 돌파")
            tracker["last_milestone_step"] = cur_step

        tracker["last_orders"] = cur_orders
        tracker["last_gmv"] = cur_gmv
        save_sales_tracker(tracker)

    except Exception as e:
        logger.error(f"판매 모니터링 오류: {e}")


def run_daily_report_if_night():
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)

    if now.hour != 23 and "--force-report" not in sys.argv:
        return

    logger.info("📊 일일 파트너스 결산 리포트 생성 중...")
    today_str = now.strftime("%Y%m%d")
    try:
        res = coupang_api.get_order_report(start_date=today_str, end_date=today_str)
        items = res.get("data", [])

        total_orders = len(items)
        total_gmv = sum(int(it.get("gmv", 0)) for it in items)
        est_commission = round(total_gmv * 0.03)

        report_msg = (
            f"📊 <b>[오늘의 쿠팡 파트너스 일일 결산]</b>\n"
            f"📅 기준 일자: {now.strftime('%Y년 %m월 %d일')}\n\n"
            f"• <b>총 주문 건수</b>: <b>{total_orders}건</b>\n"
            f"• <b>총 결제 금액</b>: <b>{total_gmv:,}원</b>\n"
            f"• <b>예상 정산 수익</b>: <b>약 {est_commission:,}원</b> (3% 기준)\n\n"
            f"오늘 하루도 고생 많으셨습니다! 내일도 알짜 핫딜이 자동으로 배달됩니다. 🚀"
        )

        target_channel = REPORT_CHANNEL_ID or ADMIN_CHAT_ID
        send_telegram(chat_id=target_channel, text=report_msg)
        logger.info("✅ 일일 결산 리포트 관리자 전송 완료")
    except Exception as e:
        logger.error(f"결산 리포트 조회 오류: {e}")


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
        )
    except Exception:
        pass


def sync_git_state():
    """로컬 실행 시 깃허브 액션이 올렸을 수 있는 최신 seen_deals를 pull 동기화"""
    if os.getenv("GITHUB_ACTIONS"):
        return
    try:
        import subprocess
        subprocess.run(["git", "pull", "--rebase"], cwd=BASE_DIR, capture_output=True, timeout=10)
    except Exception:
        pass


def sync_git_push():
    """로컬에서 신규 딜 송출 완료 시 깃허브로 즉시 push 동기화"""
    if os.getenv("GITHUB_ACTIONS"):
        return
    try:
        import subprocess
        subprocess.run(["git", "add", "seen_deals.json", "sales_tracker.json"], cwd=BASE_DIR, capture_output=True, timeout=5)
        subprocess.run(
            ["git", "commit", "-m", "chore: sync seen deals from local runner [skip ci]"],
            cwd=BASE_DIR,
            capture_output=True,
            timeout=5,
        )
        subprocess.run(["git", "push"], cwd=BASE_DIR, capture_output=True, timeout=10)
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

    logger.info("🚀 핫딜 수집 및 송출 작업 시작 (명시적 가격·할인가·할인율 정밀 검증 엔진)...")
    seen = load_seen()
    total_sent = 0

    # 1. 루리웹 핫딜 검사 & 송출
    for d in collect_ruliweb_deals():
        if process_deal(d, parse_ruliweb_detail, "ruliweb", seen):
            total_sent += 1

    # 2. 뽐뿌 핫딜 검사 & 송출
    for d in collect_ppomppu_deals():
        if process_deal(d, parse_ppomppu_detail, "ppomppu", seen):
            total_sent += 1

    # 3. 아카라이브 핫딜 검사 & 송출
    for d in collect_arcalive_deals():
        if process_deal(d, parse_arcalive_detail, "arca", seen):
            total_sent += 1

    # 4. 아침 골드박스
    total_sent += run_goldbox_if_morning(seen)

    # 5. 실시간 판매액 및 마일스톤 모니터링
    check_and_notify_sales()

    # 6. 밤 23시 결산 리포트
    run_daily_report_if_night()

    if total_sent > 0:
        sync_git_push()

    logger.info(f"🎉 모든 작업 완료! (새로 송출된 핫딜 총 {total_sent}건)")


if __name__ == "__main__":
    main()
