import html
import io
import logging
import os
import pathlib
import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFont

try:
    from price_history import strip_purchase_limit_phrases
except Exception:
    def strip_purchase_limit_phrases(text: str) -> str:
        return text or ""

logger = logging.getLogger(__name__)

BASE_DIR = pathlib.Path(__file__).resolve().parent
FONT_SEARCH_DIRS = [
    BASE_DIR / "assets" / "fonts",
    pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
    pathlib.Path(r"C:\Windows\Fonts"),
    pathlib.Path("/usr/share/fonts/truetype/nanum"),
    pathlib.Path("/usr/share/fonts"),
]

HV_CANDIDATES = (
    "NanumSquareNeo-eHv.ttf",
    "NanumSquareNeo-dEb.ttf",
    "NanumGothicExtraBold.ttf",
    "NanumBarunGothicBold.ttf",
    "malgunbd.ttf",
)
EB_CANDIDATES = (
    "NanumSquareNeo-dEb.ttf",
    "NanumSquareNeo-cBd.ttf",
    "NanumGothicBold.ttf",
    "NanumBarunGothic.ttf",
    "malgunbd.ttf",
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"
}

NUM_ICONS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]


def _find_font_file(candidates: Tuple[str, ...]) -> Optional[str]:
    for d in FONT_SEARCH_DIRS:
        if not d or not d.exists():
            continue
        for name in candidates:
            p = d / name
            if p.is_file():
                return str(p)
    return None


def resolve_fonts() -> Tuple[Optional[str], Optional[str]]:
    """크로스 플랫폼(Windows/Linux) 한글 굵은 폰트 경로 반환 (hv, eb)"""
    hv = _find_font_file(HV_CANDIDATES)
    eb = _find_font_file(EB_CANDIDATES) or hv
    return hv, eb


def _load_font(path: Optional[str], size: int) -> ImageFont.FreeTypeFont:
    if path:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            pass
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> Tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _fit_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: Optional[str],
    start_size: int,
    max_w: int,
    min_size: int = 24,
) -> ImageFont.FreeTypeFont:
    sz = start_size
    f = _load_font(font_path, sz)
    while _text_size(draw, text, f)[0] > max_w and sz > min_size:
        sz -= 2
        f = _load_font(font_path, sz)
    return f


def _wrap_title_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_w: int,
    max_lines: int = 2,
) -> List[str]:
    """단어/글자 경계를 지켜 최대 max_lines줄로 줄바꿈 (초과 시 말줄임표)"""
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    if not cleaned:
        return [""]
    words = cleaned.split(" ")
    lines: List[str] = []
    cur = ""
    idx = 0
    while idx < len(words):
        w = words[idx]
        cand = f"{cur} {w}".strip() if cur else w
        if _text_size(draw, cand, font)[0] <= max_w:
            cur = cand
            idx += 1
        elif cur:
            lines.append(cur)
            cur = ""
            if len(lines) == max_lines:
                break
        else:
            part = ""
            for ch in w:
                if _text_size(draw, part + ch, font)[0] <= max_w:
                    part += ch
                else:
                    break
            if not part:
                part = w[:1]
            lines.append(part)
            words[idx] = w[len(part):]
            if not words[idx]:
                idx += 1
            if len(lines) == max_lines:
                break

    if len(lines) < max_lines and cur:
        lines.append(cur)

    # 잘린 후속 단어가 남아 있으면 마지막 줄 끝에 말줄임표
    if idx < len(words):
        last = lines[-1] if lines else ""
        while last and _text_size(draw, last + "…", font)[0] > max_w:
            last = last[:-1]
        if lines:
            lines[-1] = last.rstrip(" ,.-/") + "…"
        else:
            lines = [last + "…"]

    return lines[:max_lines]


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    bg: Tuple[int, int, int],
    fg: Tuple[int, int, int],
    pad_x: int = 22,
    pad_y: int = 10,
    radius: int = 999,
    outline: Optional[Tuple[int, int, int]] = None,
    outline_width: int = 2,
) -> Tuple[int, int]:
    tw, _ = _text_size(draw, text, font)
    if hasattr(font, "getmetrics"):
        ascent, descent = font.getmetrics()
        th = ascent + descent
    else:
        _, th = _text_size(draw, text, font)
    w = tw + pad_x * 2
    h = th + pad_y * 2
    r = min(radius, h // 2)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=bg, outline=outline, width=outline_width if outline else 0)
    bbox = draw.textbbox((0, 0), text, font=font)
    bx = x + (w - (bbox[2] - bbox[0])) // 2 - bbox[0]
    by = y + (h - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((bx, by), text, font=font, fill=fg)
    return w, h


def _trim_white_margin(img: Image.Image, threshold: int = 246, pad: int = 8) -> Image.Image:
    arr = np.asarray(img.convert("RGB"))
    mask = np.any(arr < threshold, axis=2)
    coords = np.argwhere(mask)
    if coords.size == 0:
        return img
    y0, x0 = coords.min(axis=0)
    y1, x1 = coords.max(axis=0) + 1
    h, w = arr.shape[:2]
    if (x1 - x0) < w * 0.15 or (y1 - y0) < h * 0.15:
        return img
    return img.crop((
        int(max(0, x0 - pad)),
        int(max(0, y0 - pad)),
        int(min(w, x1 + pad)),
        int(min(h, y1 + pad)),
    ))


def _download_clean_image(url: str) -> Optional[Image.Image]:
    if not url or not url.startswith("http"):
        return None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code != 200:
            return None
        img = Image.open(io.BytesIO(resp.content))
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            rgb = bg.convert("RGB")
        else:
            rgb = img.convert("RGB")
        return _trim_white_margin(rgb)
    except Exception:
        return None


def _make_placeholder_image(label: str, hv_font: Optional[str]) -> Image.Image:
    img = Image.new("RGB", (400, 400), (248, 250, 252))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([20, 20, 380, 380], radius=36, fill=(241, 245, 249), outline=(203, 213, 225), width=4)
    f = _fit_font(draw, label[:10] or "쿠팡 핫딜", hv_font, 42, 300, min_size=24)
    txt = label[:10] or "쿠팡 핫딜"
    tw, th = _text_size(draw, txt, f)
    bbox = draw.textbbox((0, 0), txt, font=f)
    draw.text(((400 - tw) // 2 - bbox[0], (400 - th) // 2 - bbox[1]), txt, font=f, fill=(71, 85, 105))
    return img


def resolve_clean_product_image(
    coupang_api: Any,
    product_id: str,
    clean_title: str,
    sale_price: int,
    fallback_img_url: Optional[str],
    hv_font: Optional[str] = None,
) -> Image.Image:
    """쿠팡 검색 API로 깔끔한 공식 스튜디오 사진 우선 확보 (실패 시 원글 이미지 → 플레이스홀더 순 폴백)"""
    if coupang_api is not None:
        raw_tokens = [
            re.sub(r"[^\w가-힣]", "", t)
            for t in re.split(r"[\s,/()]+", clean_title)
        ]
        tokens = [t for t in raw_tokens if len(t) >= 2 and not t.isdigit()][:4]
        kw = " ".join(tokens) if tokens else clean_title[:24]
        try:
            res = coupang_api.search_products(keyword=kw, limit=10)
            pdata = (res.get("data") or {}).get("productData") or []

            if product_id:
                for it in pdata:
                    if str(it.get("productId") or "") == str(product_id) and it.get("productImage"):
                        pil = _download_clean_image(it["productImage"])
                        if pil is not None:
                            return pil

            if tokens:
                anchor = tokens[0].lower()
                for it in pdata:
                    pname = str(it.get("productName") or "").lower()
                    img_u = it.get("productImage")
                    p_val = int(round(float(it.get("productPrice", 0) or 0)))
                    price_ok = True
                    if sale_price >= 1000 and p_val >= 1000:
                        price_ok = (0.35 * sale_price) <= p_val <= (3.0 * sale_price)
                    if img_u and anchor in pname and price_ok:
                        pil = _download_clean_image(img_u)
                        if pil is not None:
                            return pil
        except Exception as e:
            logger.debug(f"공식 상품 이미지 검색 실패 ({kw}): {e}")

    if fallback_img_url:
        pil = _download_clean_image(fallback_img_url)
        if pil is not None:
            return pil

    return _make_placeholder_image(shorten_card_title(clean_title), hv_font)


def shorten_card_title(clean_title: str, max_len: int = 16) -> str:
    """버튼/요약용 핵심 상품명 축약"""
    t = re.sub(r"\([^\)]*\)", "", clean_title or "")
    t = re.sub(r"\[[^\]]*\]", "", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.-/")
    if len(t) <= max_len:
        return t or clean_title[:max_len]
    cut = t[:max_len].rstrip()
    if " " in cut and len(cut.rsplit(" ", 1)[0]) >= 8:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.-/")


def extract_unit_part(clean_title: str, unit_info: str = "") -> str:
    """단가정보에서 핵심 단가 + 수량 추출 (없으면 빈 문자열)"""
    clean_unit = re.sub(r"<[^>]+>", "", unit_info or "").strip()
    if not clean_unit:
        return ""
    m_first = re.match(r"^([^\(]+)", clean_unit)
    part = (m_first.group(1) if m_first else clean_unit).replace("약 ", "").strip()
    clean_for_cnt = strip_purchase_limit_phrases(clean_title or "")
    counts = re.findall(
        r"(\d+)\s*(개입|개|봉|팩|캔|병|포|박스|롤|통|입|매|세트|정|캡슐)(?![a-zA-Z])",
        clean_for_cnt,
    )
    if counts:
        c_val, c_u = counts[-1]
        cand = f"{part} · {c_val}{c_u}"
        return cand if len(cand) <= 18 else part
    return part


def resolve_shipping_label(deal: Dict[str, Any]) -> str:
    """원문 근거가 확인된 배송 라벨 반환 (근거 없는 기존 큐/미확인 항목은 빈 문자열)"""
    lbl = str(deal.get("shipping_label") or "").strip()
    if lbl:
        return lbl
    fee = int(deal.get("shipping_fee", 0) or 0)
    cond = str(deal.get("shipping_condition") or "").strip()
    if fee > 0 and cond:
        return f"배송비 {fee:,}원 ({cond.replace('무료배송', '무료')})"
    if cond:
        return cond
    if fee > 0:
        return f"배송비 {fee:,}원"
    return ""


_CONDITIONAL_SHIP_TOKENS = ("이상", "와우", "회원", "만", "전용", "한정", "제외", "일부")
_NON_FREE_SHIP_TOKENS = ("배송비", "착불", "별도") + _CONDITIONAL_SHIP_TOKENS


def _shipping_pill_colors(label: str) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    if any(k in label for k in ("배송비", "착불", "별도")):
        return (254, 243, 199), (180, 83, 9)
    if any(k in label for k in _CONDITIONAL_SHIP_TOKENS):
        return (238, 242, 255), (67, 56, 202)
    return (219, 234, 254), (29, 78, 216)


def _draw_row_badges(
    draw: ImageDraw.ImageDraw,
    x1: int,
    y: int,
    max_x2: int,
    ship_label: str,
    extra_chip: str,
    extra_bg: Tuple[int, int, int],
    extra_fg: Tuple[int, int, int],
    hv_font: Optional[str],
    base_sz: int = 30,
    pad_x: int = 18,
    pad_y: int = 10,
    radius: int = 12,
) -> bool:
    """배송 배지(있는 경우만)와 단가/절약 배지를 겹침 없이 한 줄에 배치"""
    cur_x = x1
    avail = max(80, max_x2 - cur_x)
    if ship_label:
        s_bg, s_fg = _shipping_pill_colors(ship_label)
        f_ship = _fit_font(draw, ship_label, hv_font, base_sz, avail - pad_x * 2, min_size=20)
        pw_ship, _ = _draw_pill(draw, cur_x, y, ship_label, f_ship, bg=s_bg, fg=s_fg, pad_x=pad_x, pad_y=pad_y, radius=radius)
        cur_x += pw_ship + 12

    if extra_chip:
        rem = (max_x2 - cur_x) - pad_x * 2
        if rem >= 80:
            f_ex = _load_font(hv_font, base_sz)
            if cur_x == x1:
                f_ex = _fit_font(draw, extra_chip, hv_font, base_sz, rem, min_size=22)
                _draw_pill(draw, cur_x, y, extra_chip, f_ex, bg=extra_bg, fg=extra_fg, pad_x=pad_x, pad_y=pad_y, radius=radius)
                return True
            if _text_size(draw, extra_chip, f_ex)[0] <= rem:
                _draw_pill(draw, cur_x, y, extra_chip, f_ex, bg=extra_bg, fg=extra_fg, pad_x=pad_x, pad_y=pad_y, radius=radius)
            elif rem >= 120:
                f_ex_fit = _fit_font(draw, extra_chip, hv_font, base_sz - 2, rem, min_size=20)
                if _text_size(draw, extra_chip, f_ex_fit)[0] <= rem:
                    _draw_pill(draw, cur_x, y, extra_chip, f_ex_fit, bg=extra_bg, fg=extra_fg, pad_x=pad_x, pad_y=pad_y, radius=radius)
    return cur_x > x1 or bool(extra_chip)


def build_card_chip(
    clean_title: str,
    orig_price: int,
    sale_price: int,
    unit_info: str = "",
    shipping_label: str = "",
) -> str:
    """전단지 카드용 굵은 보조 칩 문구 (단가 우선, 배송 정보는 원문 근거가 있을 때만 표시)"""
    unit_part = extract_unit_part(clean_title, unit_info)
    ship = (shipping_label or "").strip()
    is_paid_or_cond = bool(ship) and any(k in ship for k in _NON_FREE_SHIP_TOKENS)
    if unit_part and is_paid_or_cond:
        return f"{unit_part} · {ship}"
    if unit_part:
        return unit_part
    save_amt = max(0, orig_price - sale_price)
    if save_amt >= 1000 and ship:
        return f"{save_amt:,}원 절약 · {ship}"
    if save_amt >= 1000:
        return f"{save_amt:,}원 절약"
    return ship


def _tier_badge(rate: int) -> Tuple[str, Tuple[int, int, int]]:
    if rate >= 60:
        return f"폭탄 {rate}%", (225, 29, 72)
    if rate >= 50:
        return f"반값 {rate}%", (234, 88, 12)
    if rate >= 40:
        return f"파격 {rate}%", (220, 38, 38)
    if rate >= 30:
        return f"특가 {rate}%", (220, 38, 38)
    return f"알뜰 {rate}%", (37, 99, 235)


def _tier_emoji(rate: int) -> str:
    if rate >= 60:
        return "🚨"
    if rate >= 50:
        return "💥"
    if rate >= 40:
        return "⚡"
    if rate >= 30:
        return "🔥"
    return "🏷️"


def is_half_off_mega(deal: Dict[str, Any]) -> bool:
    """50% 이상 반값 초특가 여부 (명시적 정상가·판매가 검증 시에만)"""
    orig_p = int(deal.get("orig_price", 0) or 0)
    sale_p = int(deal.get("sale_price", 0) or 0)
    return orig_p > sale_p >= 1000 and int(deal.get("discount_rate", 0) or 0) >= 50


def is_high_ticket_mega(deal: Dict[str, Any]) -> bool:
    """백만원 이상 고가 상품이면서 20% 이상 할인되는 특가 딜 여부"""
    orig_p = int(deal.get("orig_price", 0) or 0)
    sale_p = int(deal.get("sale_price", 0) or 0)
    disc_r = int(deal.get("discount_rate", 0) or 0)
    return orig_p > sale_p >= 1000 and (orig_p >= 1_000_000 or sale_p >= 1_000_000) and disc_r >= 20


def _deal_theme(deal: Dict[str, Any]) -> str:
    if is_half_off_mega(deal):
        return "half"
    if is_high_ticket_mega(deal):
        return "vip"
    return "normal"


def _format_save_kor(save_p: int) -> str:
    if save_p >= 10000:
        man = save_p // 10000
        rem_k = (save_p % 10000) // 1000
        return f"{man}만원" if rem_k == 0 else f"{man}.{rem_k}만원"
    return f"{save_p:,}원"


def _deal_badge_style(
    deal: Dict[str, Any],
) -> Tuple[str, Tuple[int, int, int], Tuple[int, int, int], Optional[Tuple[int, int, int]]]:
    """카드 배지 문구, 배경색, 글자색, 외곽선 색 반환"""
    disc_r = int(deal.get("discount_rate", 0) or 0)
    orig_p = int(deal.get("orig_price", 0) or 0)
    sale_p = int(deal.get("sale_price", 0) or 0)
    save_p = max(0, orig_p - sale_p)
    theme = _deal_theme(deal)
    if theme == "half":
        if disc_r >= 60:
            return "반값 특가 · 품절 주의", (185, 28, 28), (254, 240, 138), (254, 240, 138)
        return "반값 특가", (225, 29, 72), (255, 255, 255), None
    if theme == "vip":
        save_str = _format_save_kor(save_p) if save_p >= 10000 else f"{disc_r}%"
        return f"고가 특가 · {save_str}↓", (15, 23, 42), (253, 224, 71), (245, 158, 11)
    ev_badge = str(deal.get("evidence_badge") or "").strip()
    if ev_badge and disc_r <= 0:
        return ev_badge, (5, 150, 105), (255, 255, 255), None
    lbl, col = _tier_badge(disc_r) if disc_r >= 12 else ("단가·반응 선별" if not ev_badge else ev_badge, (5, 150, 105))
    return lbl, col, (255, 255, 255), None


def _draw_vertical_gradient(
    canvas: Image.Image,
    box: Tuple[int, int, int, int],
    top_rgb: Tuple[int, int, int],
    bot_rgb: Tuple[int, int, int],
) -> None:
    x1, y1, x2, y2 = box
    h = y2 - y1
    arr = np.zeros((h, x2 - x1, 3), dtype=np.uint8)
    for c in range(3):
        arr[:, :, c] = np.linspace(top_rgb[c], bot_rgb[c], h)[:, None].astype(np.uint8)
    canvas.paste(Image.fromarray(arr, mode="RGB"), (x1, y1))


def _draw_right_arrow(draw: ImageDraw.ImageDraw, x1: int, cy: int, length: int, color: Tuple[int, int, int]) -> None:
    x2 = x1 + length
    draw.line([(x1, cy), (x2 - 12, cy)], fill=color, width=6)
    draw.polygon([(x2 - 15, cy - 12), (x2, cy), (x2 - 15, cy + 12)], fill=color)


def _paste_fit_center(canvas: Image.Image, img: Image.Image, box: Tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    bw, bh = max(1, x2 - x1), max(1, y2 - y1)
    iw, ih = img.size
    scale = min(bw / iw, bh / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas.paste(resized, (x1 + (bw - nw) // 2, y1 + (bh - nh) // 2))


def _draw_price_box(
    draw: ImageDraw.ImageDraw,
    box: Tuple[int, int, int, int],
    orig_p: int,
    sale_p: int,
    disc_r: int,
    hv_font: Optional[str],
    eb_font: Optional[str],
    orig_prefix: str = "",
    theme: str = "normal",
    evidence_note: str = "",
) -> None:
    box_x1, box_y1, box_x2, box_y2 = box
    if theme == "half":
        box_bg = (190, 18, 60)
        box_bd = (254, 240, 138)
        bd_w = 4
        orig_col = (254, 205, 211)
        arr_col = (254, 240, 138)
        save_col = (254, 240, 138)
        rate_col = (254, 240, 138)
        price_col = (255, 255, 255)
    elif theme == "vip":
        box_bg = (15, 23, 42)
        box_bd = (245, 158, 11)
        bd_w = 4
        orig_col = (148, 163, 184)
        arr_col = (251, 191, 36)
        save_col = (253, 224, 71)
        rate_col = (253, 224, 71)
        price_col = (255, 255, 255)
    else:
        is_hot = disc_r >= 40
        box_bg = (255, 241, 242) if is_hot else (248, 250, 252)
        box_bd = (254, 205, 211) if is_hot else (203, 213, 225)
        bd_w = 3
        orig_col = (100, 116, 139)
        arr_col = (225, 29, 72)
        save_col = (225, 29, 72)
        rate_col = (225, 29, 72)
        price_col = (15, 23, 42)

    draw.rounded_rectangle([box_x1, box_y1, box_x2, box_y2], radius=20, fill=box_bg, outline=box_bd, width=bd_w)

    avail_box_w = (box_x2 - box_x1) - 44
    l1_x = box_x1 + 22
    l1_y = box_y1 + 20

    if orig_p > sale_p >= 1000:
        save_p = orig_p - sale_p
        price_only = f"{orig_p:,}원"
        orig_txt = f"{orig_prefix}{price_only}"
        save_txt = f"{save_p:,}원 절약" if (box_x2 - box_x1) >= 820 else f"{_format_save_kor(save_p)}↓"

        r1_sz = 38
        while r1_sz > 20:
            f_o = _load_font(eb_font, r1_sz)
            f_s = _load_font(hv_font, r1_sz)
            if _text_size(draw, orig_txt, f_o)[0] + 52 + _text_size(draw, save_txt, f_s)[0] <= avail_box_w:
                break
            r1_sz -= 2
        f_o = _load_font(eb_font, r1_sz)
        f_s = _load_font(hv_font, r1_sz)

        ob = draw.textbbox((0, 0), orig_txt, font=f_o)
        ow, oh = ob[2] - ob[0], ob[3] - ob[1]
        draw.text((l1_x - ob[0], l1_y - ob[1]), orig_txt, font=f_o, fill=orig_col)
        pref_w = _text_size(draw, orig_prefix, f_o)[0] if orig_prefix else 0
        mid_y = l1_y + oh // 2
        draw.line([(l1_x + pref_w - 3, mid_y), (l1_x + ow + 3, mid_y)], fill=orig_col, width=4)

        arr_x = l1_x + ow + 14
        _draw_right_arrow(draw, arr_x, mid_y, 32, arr_col)

        sb = draw.textbbox((0, 0), save_txt, font=f_s)
        draw.text((arr_x + 44 - sb[0], l1_y - sb[1]), save_txt, font=f_s, fill=save_col)
    else:
        ev_txt = (evidence_note or "").strip() or "실시간 커뮤니티 검증 특가"
        f_sub = _fit_font(draw, ev_txt, eb_font, 32, avail_box_w, min_size=20)
        sb = draw.textbbox((0, 0), ev_txt, font=f_sub)
        draw.text((l1_x - sb[0], l1_y - sb[1]), ev_txt, font=f_sub, fill=orig_col)

    l2_y = box_y1 + 80
    r_str = f"{disc_r}%" if disc_r > 0 else "특가"
    p_str = f"{sale_p:,}원" if sale_p >= 1000 else "최저가"
    r2_sz = 84
    while r2_sz > 34:
        f_r2 = _load_font(hv_font, r2_sz)
        if _text_size(draw, r_str, f_r2)[0] + 24 + _text_size(draw, p_str, f_r2)[0] <= avail_box_w:
            break
        r2_sz -= 2
    f_r2 = _load_font(hv_font, r2_sz)

    rb = draw.textbbox((0, 0), r_str, font=f_r2)
    rw, rh = rb[2] - rb[0], rb[3] - rb[1]
    draw.text((l1_x - rb[0], l2_y - rb[1]), r_str, font=f_r2, fill=rate_col)

    pb = draw.textbbox((0, 0), p_str, font=f_r2)
    pw = pb[2] - pb[0]
    px_right = (box_x2 - 22) - pw - pb[0]
    draw.text((px_right, l2_y + rh - (pb[3] - pb[1]) - pb[1]), p_str, font=f_r2, fill=price_col)


def _draw_single_rich_card(
    canvas: Image.Image,
    deal: Dict[str, Any],
    prod_img: Image.Image,
    hv_font: Optional[str],
    eb_font: Optional[str],
) -> bytes:
    """1건 전용 가로형 리치 카드 (1560x980 2배 -> 780x490): 50%+ 반값 / 100만+ 프리미엄 / 일반 특가 테마 자동 적용"""
    W, H = 1560, 980
    header_h = 146
    theme = _deal_theme(deal)
    disc_r = int(deal.get("discount_rate", 0) or 0)
    orig_p = int(deal.get("orig_price", 0) or 0)
    sale_p = int(deal.get("sale_price", 0) or 0)
    save_p = max(0, orig_p - sale_p)

    bg_canvas = (255, 241, 242) if theme == "half" else ((226, 232, 240) if theme == "vip" else (241, 245, 249))
    canvas = Image.new("RGB", (W, H), bg_canvas)

    if theme == "half":
        _draw_vertical_gradient(canvas, (0, 0, W, header_h), (127, 29, 29), (190, 18, 60))
        hdr_title = "50%+ 반값 특가"
        hdr_badge_txt = f"{disc_r}%↓ 특가"
        hb_bg, hb_fg, hb_bd = (15, 23, 42), (254, 240, 138), (254, 240, 138)
        card_bd, card_bd_w = (225, 29, 72), 6
    elif theme == "vip":
        _draw_vertical_gradient(canvas, (0, 0, W, header_h), (10, 15, 28), (30, 41, 59))
        hdr_title = "100만원대 프리미엄 특가"
        hdr_badge_txt = f"{_format_save_kor(save_p)} 할인"
        hb_bg, hb_fg, hb_bd = (245, 158, 11), (15, 23, 42), None
        card_bd, card_bd_w = (245, 158, 11), 6
    else:
        _draw_vertical_gradient(canvas, (0, 0, W, header_h), (11, 18, 33), (30, 41, 59))
        hdr_title = "쿠팡 실시간 특가"
        ev_badge = str(deal.get("evidence_badge") or "").strip()
        hdr_badge_txt = f"{disc_r}%↓ 특가" if disc_r > 0 else (ev_badge or "단가·반응 선별")
        hb_bg, hb_fg, hb_bd = (225, 29, 72), (255, 255, 255), None
        card_bd, card_bd_w = (226, 232, 240), 3

    draw = ImageDraw.Draw(canvas)

    f_hdr = _load_font(hv_font, 66)
    f_hdr_badge = _load_font(hv_font, 40)
    hb = draw.textbbox((0, 0), hdr_title, font=f_hdr)
    draw.text((44, (header_h - (hb[3] - hb[1])) // 2 - hb[1]), hdr_title, font=f_hdr, fill=(255, 255, 255))

    btw, _ = _text_size(draw, hdr_badge_txt, f_hdr_badge)
    _draw_pill(
        draw, W - 44 - (btw + 60), 28, hdr_badge_txt, f_hdr_badge,
        bg=hb_bg, fg=hb_fg, pad_x=30, pad_y=18, outline=hb_bd, outline_width=3,
    )

    # Outer main card
    cx1, cy1, cx2, cy2 = 34, header_h + 26, W - 34, H - 28
    shadow_col = (244, 182, 194) if theme == "half" else ((180, 190, 206) if theme == "vip" else (216, 224, 236))
    draw.rounded_rectangle([cx1 + 4, cy1 + 8, cx2 + 4, cy2 + 8], radius=30, fill=shadow_col)
    draw.rounded_rectangle([cx1, cy1, cx2, cy2], radius=30, fill=(255, 255, 255), outline=card_bd, width=card_bd_w)

    # Left: large product photo + FOMO/VIP tier badge
    stage_x1, stage_y1 = cx1 + 28, cy1 + 28
    stage_x2, stage_y2 = cx1 + 560, cy2 - 28
    draw.rounded_rectangle([stage_x1, stage_y1, stage_x2, stage_y2], radius=22, fill=(255, 255, 255), outline=(241, 245, 249), width=3)

    t_lbl, t_bg, t_fg, t_bd = _deal_badge_style(deal)
    f_tag = _load_font(hv_font, 36)
    _draw_pill(draw, stage_x1 + 16, stage_y1 + 16, t_lbl, f_tag, bg=t_bg, fg=t_fg, pad_x=22, pad_y=11, radius=16, outline=t_bd, outline_width=2)

    _paste_fit_center(canvas, prod_img, (stage_x1 + 24, stage_y1 + 96, stage_x2 - 24, stage_y2 - 24))

    # Right: full rich info column
    rx1 = stage_x2 + 34
    rx2 = cx2 - 30
    rw = rx2 - rx1
    cur_y = cy1 + 32

    # 1) Delivery (only when verified) + Unit/Savings pills
    ship_label = resolve_shipping_label(deal)
    unit_part = extract_unit_part(str(deal.get("clean_title") or ""), str(deal.get("unit_info") or ""))
    if theme == "vip" and save_p >= 10000:
        extra_chip = f"{_format_save_kor(save_p)} 할인"
        ex_bg, ex_fg = (254, 243, 199), (146, 64, 14)
    elif theme == "half":
        extra_chip = unit_part or "반값 이상 할인"
        ex_bg, ex_fg = (255, 228, 230), (190, 18, 60)
    else:
        extra_chip = unit_part or (f"{orig_p - sale_p:,}원 절약" if orig_p > sale_p >= 1000 else "")
        ex_bg, ex_fg = (220, 252, 231), (4, 120, 87)

    if _draw_row_badges(draw, rx1, cur_y, rx2, ship_label, extra_chip, ex_bg, ex_fg, hv_font, base_sz=32, pad_x=22, pad_y=11, radius=14):
        cur_y += 82

    # 2) Full product title (up to 3 lines cleanly wrapped, zero harsh truncation)
    full_title = str(deal.get("clean_title") or "쿠팡 핫딜").strip()
    f_title = _load_font(hv_font, 46)
    t_lines = _wrap_title_lines(draw, full_title, f_title, rw, max_lines=3)
    for ln in t_lines:
        tb = draw.textbbox((0, 0), ln, font=f_title)
        draw.text((rx1 - tb[0], cur_y - tb[1]), ln, font=f_title, fill=(15, 23, 42))
        cur_y += 60

    # 3) Community reaction or urgency banner if space permits
    reaction = str(deal.get("reaction_text") or "").strip()
    box_y1 = cy2 - 224
    if (cur_y + 72) <= box_y1:
        if reaction:
            f_react = _load_font(eb_font, 28)
            r_lines = _wrap_title_lines(draw, f"반응: {reaction}", f_react, rw - 32, max_lines=1)
            _draw_pill(draw, rx1, cur_y + 10, r_lines[0], f_react, bg=(241, 245, 249), fg=(71, 85, 105), pad_x=18, pad_y=10, radius=12)
        elif theme == "half":
            f_react = _load_font(hv_font, 28)
            _draw_pill(draw, rx1, cur_y + 10, "조기 품절·가격 변동 가능", f_react, bg=(255, 241, 242), fg=(190, 18, 60), pad_x=18, pad_y=10, radius=12)
        elif theme == "vip":
            f_react = _load_font(hv_font, 28)
            vip_note = f"고가 상품 할인 · {ship_label}" if ship_label else "고가 상품 할인"
            _draw_pill(draw, rx1, cur_y + 10, vip_note, f_react, bg=(254, 249, 195), fg=(113, 63, 18), pad_x=18, pad_y=10, radius=12)

    # 4) Rich Price Story Box
    _draw_price_box(
        draw, (rx1, box_y1, rx2, cy2 - 26), orig_p, sale_p, disc_r, hv_font, eb_font,
        orig_prefix="정상가 ", theme=theme, evidence_note=str(deal.get("evidence_note") or ""),
    )

    final = canvas.resize((780, 490), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    final.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _draw_grid_card_item(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    cx1: int,
    cy1: int,
    cx2: int,
    cy2: int,
    idx_num: int,
    deal: Dict[str, Any],
    prod_img: Image.Image,
    hv_font: Optional[str],
    eb_font: Optional[str],
    rich_mode: bool = False,
) -> None:
    card_w = cx2 - cx1
    theme = _deal_theme(deal)
    card_bd = (225, 29, 72) if theme == "half" else ((245, 158, 11) if theme == "vip" else (226, 232, 240))
    card_bd_w = 6 if theme in ("half", "vip") else 3

    draw.rounded_rectangle([cx1 + 4, cy1 + 8, cx2 + 4, cy2 + 8], radius=28, fill=(216, 224, 236))
    draw.rounded_rectangle([cx1, cy1, cx2, cy2], radius=28, fill=(255, 255, 255), outline=card_bd, width=card_bd_w)

    f_num = _load_font(hv_font, 44)
    f_tag = _load_font(hv_font, 36)
    num_bg = (190, 18, 60) if theme == "half" else (15, 23, 42)
    num_fg = (254, 240, 138) if theme == "half" else ((253, 224, 71) if theme == "vip" else (255, 255, 255))
    _draw_pill(draw, cx1 + 20, cy1 + 18, f"{idx_num}", f_num, bg=num_bg, fg=num_fg, pad_x=22, pad_y=10, radius=16)

    disc_r = int(deal.get("discount_rate", 0) or 0)
    t_lbl, t_bg, t_fg, t_bd = _deal_badge_style(deal)
    ttw, _ = _text_size(draw, t_lbl, f_tag)
    _draw_pill(draw, cx2 - 20 - (ttw + 44), cy1 + 18, t_lbl, f_tag, bg=t_bg, fg=t_fg, pad_x=22, pad_y=10, radius=16, outline=t_bd, outline_width=2)

    box_h = 200
    box_x1, box_y1 = cx1 + 20, cy2 - 18 - box_h
    box_x2, box_y2 = cx2 - 20, cy2 - 18

    chip_y = box_y1 - 66
    title_font_sz = 40 if rich_mode else 38
    line_step = 48
    title_y = chip_y - (line_step * 2 + 12)

    img_box = (cx1 + 36, cy1 + 98, cx2 - 36, title_y - 14)
    _paste_fit_center(canvas, prod_img, img_box)

    # Full 2-line wrapped title inside card
    full_title = str(deal.get("clean_title") or "").strip()
    avail_title_w = card_w - 44
    f_title = _load_font(hv_font, title_font_sz)
    t_lines = _wrap_title_lines(draw, full_title, f_title, avail_title_w, max_lines=2)
    cur_ty = title_y if len(t_lines) == 2 else title_y + 24
    for ln in t_lines:
        tb = draw.textbbox((0, 0), ln, font=f_title)
        draw.text((cx1 + 22 - tb[0], cur_ty - tb[1]), ln, font=f_title, fill=(15, 23, 42))
        cur_ty += line_step

    orig_p = int(deal.get("orig_price", 0) or 0)
    sale_p = int(deal.get("sale_price", 0) or 0)
    unit_part = extract_unit_part(full_title, str(deal.get("unit_info") or ""))
    ship_label = resolve_shipping_label(deal)

    if rich_mode:
        # 2건·하단 2열 모드: 근거가 확인된 배송 배지 + [단가/절약] 배지 노출
        extra_chip = unit_part or (f"{orig_p - sale_p:,}원 절약" if orig_p > sale_p >= 1000 else "")
        _draw_row_badges(draw, cx1 + 22, chip_y, cx2 - 22, ship_label, extra_chip, (220, 252, 231), (4, 120, 87), hv_font, base_sz=30, pad_x=18, pad_y=10, radius=12)
    else:
        chip_txt = build_card_chip(full_title, orig_p, sale_p, str(deal.get("unit_info") or ""), shipping_label=ship_label)
        if chip_txt:
            f_chip = _fit_font(draw, chip_txt, hv_font, 34, avail_title_w - 40, min_size=22)
            has_paid_or_cond = any(k in chip_txt for k in _NON_FREE_SHIP_TOKENS)
            chip_bg = (254, 243, 199) if (theme == "vip" or (theme == "normal" and has_paid_or_cond)) else ((255, 228, 230) if theme == "half" else (220, 252, 231))
            chip_fg = (146, 64, 14) if (theme == "vip" or (theme == "normal" and has_paid_or_cond)) else ((190, 18, 60) if theme == "half" else (4, 120, 87))
            _draw_pill(draw, cx1 + 22, chip_y, chip_txt, f_chip, bg=chip_bg, fg=chip_fg, pad_x=20, pad_y=10, radius=12)

    _draw_price_box(
        draw, (box_x1, box_y1, box_x2, box_y2), orig_p, sale_p, disc_r, hv_font, eb_font,
        theme=theme, evidence_note=str(deal.get("evidence_note") or ""),
    )


def _draw_wide_card_item(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    cx1: int,
    cy1: int,
    cx2: int,
    cy2: int,
    idx_num: int,
    deal: Dict[str, Any],
    prod_img: Image.Image,
    hv_font: Optional[str],
    eb_font: Optional[str],
) -> None:
    """3건 1+2 벤토 레이아웃 상단 가로 와이드 카드"""
    theme = _deal_theme(deal)
    disc_r = int(deal.get("discount_rate", 0) or 0)
    orig_p = int(deal.get("orig_price", 0) or 0)
    sale_p = int(deal.get("sale_price", 0) or 0)
    save_p = max(0, orig_p - sale_p)

    card_bd = (225, 29, 72) if theme == "half" else ((245, 158, 11) if theme == "vip" else (226, 232, 240))
    card_bd_w = 6 if theme in ("half", "vip") else 3

    draw.rounded_rectangle([cx1 + 4, cy1 + 8, cx2 + 4, cy2 + 8], radius=28, fill=(216, 224, 236))
    draw.rounded_rectangle([cx1, cy1, cx2, cy2], radius=28, fill=(255, 255, 255), outline=card_bd, width=card_bd_w)

    # Left: photo stage + index pill
    stage_x1, stage_y1 = cx1 + 22, cy1 + 20
    stage_x2, stage_y2 = cx1 + 540, cy2 - 20
    draw.rounded_rectangle([stage_x1, stage_y1, stage_x2, stage_y2], radius=20, fill=(255, 255, 255), outline=(241, 245, 249), width=3)

    f_num = _load_font(hv_font, 42)
    num_bg = (190, 18, 60) if theme == "half" else (15, 23, 42)
    num_fg = (254, 240, 138) if theme == "half" else ((253, 224, 71) if theme == "vip" else (255, 255, 255))
    _draw_pill(draw, stage_x1 + 14, stage_y1 + 14, f"{idx_num}", f_num, bg=num_bg, fg=num_fg, pad_x=22, pad_y=10, radius=16)

    _paste_fit_center(canvas, prod_img, (stage_x1 + 22, stage_y1 + 88, stage_x2 - 22, stage_y2 - 20))

    # Right column
    rx1 = stage_x2 + 30
    rx2 = cx2 - 26
    rw = rx2 - rx1

    # Top row: left delivery/extra pills + right tier badge
    t_lbl, t_bg, t_fg, t_bd = _deal_badge_style(deal)
    f_tag = _load_font(hv_font, 34)
    badge_w = _text_size(draw, t_lbl, f_tag)[0] + 40
    badge_x = rx2 - badge_w
    _draw_pill(draw, badge_x, cy1 + 24, t_lbl, f_tag, bg=t_bg, fg=t_fg, pad_x=20, pad_y=10, radius=16, outline=t_bd, outline_width=2)

    ship_label = resolve_shipping_label(deal)
    full_title = str(deal.get("clean_title") or "").strip()
    unit_part = extract_unit_part(full_title, str(deal.get("unit_info") or ""))
    if theme == "vip" and save_p >= 10000:
        extra_chip = f"{_format_save_kor(save_p)} 할인"
        ex_bg, ex_fg = (254, 243, 199), (146, 64, 14)
    elif theme == "half":
        extra_chip = unit_part or "반값 이상 할인"
        ex_bg, ex_fg = (255, 228, 230), (190, 18, 60)
    else:
        extra_chip = unit_part or (f"{orig_p - sale_p:,}원 절약" if orig_p > sale_p >= 1000 else "")
        ex_bg, ex_fg = (220, 252, 231), (4, 120, 87)

    _draw_row_badges(draw, rx1, cy1 + 26, badge_x - 16, ship_label, extra_chip, ex_bg, ex_fg, hv_font, base_sz=30, pad_x=18, pad_y=10, radius=12)

    # Wrapped 2-line title
    cur_y = cy1 + 98
    f_title = _load_font(hv_font, 44)
    t_lines = _wrap_title_lines(draw, full_title, f_title, rw, max_lines=2)
    for ln in t_lines:
        tb = draw.textbbox((0, 0), ln, font=f_title)
        draw.text((rx1 - tb[0], cur_y - tb[1]), ln, font=f_title, fill=(15, 23, 42))
        cur_y += 54

    # Bottom price box
    box_h = 204
    box_y1 = cy2 - 22 - box_h
    box_y2 = cy2 - 22

    # Optional community reaction quote
    reaction = str(deal.get("reaction_text") or "").strip()
    if reaction and (cur_y + 62) <= box_y1:
        f_react = _load_font(eb_font, 28)
        r_lines = _wrap_title_lines(draw, f"반응: {reaction}", f_react, rw - 32, max_lines=1)
        _draw_pill(draw, rx1, cur_y + 10, r_lines[0], f_react, bg=(241, 245, 249), fg=(71, 85, 105), pad_x=18, pad_y=10, radius=12)

    _draw_price_box(
        draw, (rx1, box_y1, rx2, box_y2), orig_p, sale_p, disc_r, hv_font, eb_font,
        orig_prefix="정상가 ", theme=theme, evidence_note=str(deal.get("evidence_note") or ""),
    )


def render_hotdeal_flyer(deals: List[Dict[str, Any]], coupang_api: Any = None) -> bytes:
    """1~4건 커뮤니티 핫딜 목록을 빅타이포 전단지 PNG 바이트로 렌더링 (세로 슬림 비율 + 3건 1+2 벤토 레이아웃)"""
    batch = deals[:4]
    if not batch:
        raise ValueError("deals must not be empty")

    hv_font, eb_font = resolve_fonts()
    n = len(batch)

    prod_imgs = [
        resolve_clean_product_image(
            coupang_api,
            str(d.get("product_id") or ""),
            str(d.get("clean_title") or ""),
            int(d.get("sale_price", 0) or 0),
            d.get("img_url"),
            hv_font=hv_font,
        )
        for d in batch
    ]

    if n == 1:
        canvas = Image.new("RGB", (1560, 980), (241, 245, 249))
        return _draw_single_rich_card(canvas, batch[0], prod_imgs[0], hv_font, eb_font)

    has_half = any(is_half_off_mega(d) for d in batch)
    has_vip = any(is_high_ticket_mega(d) for d in batch)

    if n >= 4:
        W, H = 1800, 1980
        out_size = (900, 990)
        hdr_title = "50%+ 반값 포함 · 쿠팡 핫딜" if has_half else ("프리미엄 특가 포함 · 쿠팡 핫딜" if has_vip else "쿠팡 실시간 핫딜")
    elif n == 3:
        W, H = 1800, 1700
        out_size = (900, 850)
        hdr_title = "50%+ 반값 포함 · 쿠팡 핫딜" if has_half else ("프리미엄 특가 포함 · 쿠팡 핫딜" if has_vip else "쿠팡 실시간 핫딜")
    else:
        W, H = 1800, 1080
        out_size = (900, 540)
        hdr_title = "50%+ 반값 포함 · 추천 핫딜" if has_half else ("프리미엄 특가 포함 · 추천 핫딜" if has_vip else "쿠팡 추천 핫딜")

    header_h = 152
    canvas = Image.new("RGB", (W, H), (241, 245, 249))
    if has_half:
        _draw_vertical_gradient(canvas, (0, 0, W, header_h), (127, 29, 29), (190, 18, 60))
    else:
        _draw_vertical_gradient(canvas, (0, 0, W, header_h), (11, 18, 33), (30, 41, 59))
    draw = ImageDraw.Draw(canvas)

    f_hdr = _load_font(hv_font, 68)
    f_hdr_badge = _load_font(hv_font, 42)
    hb = draw.textbbox((0, 0), hdr_title, font=f_hdr)
    draw.text((44, (header_h - (hb[3] - hb[1])) // 2 - hb[1]), hdr_title, font=f_hdr, fill=(255, 255, 255))

    max_rate = max(int(d.get("discount_rate", 0) or 0) for d in batch)
    badge_txt = f"최대 {max_rate}%↓" if max_rate > 0 else "단가·반응 선별"
    btw, _ = _text_size(draw, badge_txt, f_hdr_badge)
    b_bg, b_fg = ((15, 23, 42), (254, 240, 138)) if has_half else ((225, 29, 72), (255, 255, 255))
    _draw_pill(draw, W - 44 - (btw + 60), 28, badge_txt, f_hdr_badge, bg=b_bg, fg=b_fg, pad_x=30, pad_y=18)

    margin_x = 34
    margin_top = header_h + 26
    margin_bot = 26
    gap = 30

    if n == 3:
        top_h = 600
        _draw_wide_card_item(
            canvas, draw, margin_x, margin_top, W - margin_x, margin_top + top_h,
            1, batch[0], prod_imgs[0], hv_font, eb_font,
        )
        bot_y1 = margin_top + top_h + gap
        bot_y2 = H - margin_bot
        card_w = (W - margin_x * 2 - gap) // 2
        for col_idx, deal_idx in enumerate((1, 2)):
            cx1 = margin_x + col_idx * (card_w + gap)
            _draw_grid_card_item(
                canvas, draw, cx1, bot_y1, cx1 + card_w, bot_y2,
                deal_idx + 1, batch[deal_idx], prod_imgs[deal_idx],
                hv_font, eb_font, rich_mode=True,
            )
    else:
        cols = 2
        rows = 2 if n >= 4 else 1
        card_w = (W - margin_x * 2 - gap * (cols - 1)) // cols
        card_h = (H - margin_top - margin_bot - gap * (rows - 1)) // rows
        for idx in range(min(n, cols * rows)):
            row, col = divmod(idx, cols)
            cx1 = margin_x + col * (card_w + gap)
            cy1 = margin_top + row * (card_h + gap)
            _draw_grid_card_item(
                canvas, draw, cx1, cy1, cx1 + card_w, cy1 + card_h,
                idx + 1, batch[idx], prod_imgs[idx],
                hv_font, eb_font, rich_mode=(n == 2),
            )

    final = canvas.resize(out_size, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    final.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _build_caption_with_limit(batch: List[Dict[str, Any]], max_title_len: int) -> str:
    n = len(batch)
    has_half = any(is_half_off_mega(d) for d in batch)
    has_vip = any(is_high_ticket_mega(d) for d in batch)

    if n == 1:
        d0 = batch[0]
        save0 = max(0, int(d0.get("orig_price", 0) or 0) - int(d0.get("sale_price", 0) or 0))
        if is_half_off_mega(d0):
            header = f"🚨 <b>[{int(d0.get('discount_rate', 0) or 0)}%↓ 반값 특가]</b>"
        elif is_high_ticket_mega(d0):
            header = f"💎 <b>[고가 특가 · {_format_save_kor(save0)} 할인]</b>"
        else:
            header = "🔥 <b>[쿠팡 실시간 특가]</b>"
    elif has_half:
        header = "🚨 <b>[반값 특가 포함 · 쿠팡 핫딜]</b>"
    elif has_vip:
        header = "💎 <b>[고가 특가 포함 · 쿠팡 핫딜]</b>"
    elif n >= 3:
        header = "🔥 <b>[쿠팡 실시간 핫딜 모아보기]</b>"
    else:
        header = "🔥 <b>[쿠팡 추천 핫딜]</b>"
    lines = [header, ""]

    for i, d in enumerate(batch):
        full_t = str(d.get("clean_title") or "쿠팡 핫딜").strip()
        if len(full_t) > max_title_len:
            full_t = full_t[: max_title_len - 1].rstrip() + "…"
        url = str(d.get("deeplink") or "").strip()
        orig_p = int(d.get("orig_price", 0) or 0)
        sale_p = int(d.get("sale_price", 0) or 0)
        disc_r = int(d.get("discount_rate", 0) or 0)
        save_amt = max(0, orig_p - sale_p)
        unit_raw = re.sub(r"<[^>]+>", "", str(d.get("unit_info") or "")).strip()

        icon = NUM_ICONS[i] if i < len(NUM_ICONS) else f"{i + 1}."
        if url:
            lines.append(f'<b>{icon} <a href="{html.escape(url, quote=True)}">{html.escape(full_t, quote=False)}</a></b>')
        else:
            lines.append(f"<b>{icon} {html.escape(full_t, quote=False)}</b>")

        tags: List[str] = []
        if is_half_off_mega(d):
            tags.append("🚨<b>[반값 특가]</b>")
        if is_high_ticket_mega(d):
            tags.append(f"💎<b>[{_format_save_kor(save_amt)} 할인]</b>")
        tag_prefix = (" ".join(tags) + " ") if tags else ""

        emo = _tier_emoji(disc_r) if disc_r >= 20 else ("🏷️" if disc_r >= 12 else "💡")
        price_str = f"<b>{sale_p:,}원</b>" if sale_p >= 1000 else "<b>특가 확인</b>"
        ev_badge = str(d.get("evidence_badge") or "").strip()
        if orig_p > sale_p >= 1000 and disc_r >= 3:
            orig_part = f"<s>{orig_p:,}원</s> → "
            rate_part = f"{tag_prefix}{emo}<b>{disc_r}%↓</b> "
            if unit_raw:
                m_u = re.match(r"^([^\(]+)", unit_raw)
                u_short = (m_u.group(1) if m_u else unit_raw).replace("약 ", "").strip()
                extra = f" · {html.escape(u_short, quote=False)}"
            else:
                extra = f" ({save_amt:,}원 절약)"
        else:
            orig_part = ""
            if ev_badge:
                rate_part = f"{tag_prefix}{emo}[{html.escape(ev_badge, quote=False)}] "
            else:
                rate_part = f"{tag_prefix}{emo} "
            extra = f" · {html.escape(unit_raw, quote=False)}" if unit_raw else ""

        ship_label = resolve_shipping_label(d)
        ship_part = f" · {html.escape(ship_label, quote=False)}" if ship_label else ""
        lines.append(f"   {rate_part}{orig_part}{price_str}{extra}{ship_part}")
        lines.append("")

    lines.append("<i>※ 쿠팡 파트너스 활동으로 일정액의 수수료를 제공받습니다.</i>")
    return "\n".join(lines)


def format_flyer_caption_and_buttons(
    deals: List[Dict[str, Any]],
) -> Tuple[str, List[List[Dict[str, Any]]]]:
    """텔레그램 sendPhoto용 1,024자 안전 캡션과 번호별 초록(success) CTA 버튼 생성"""
    batch = deals[:4]
    caption = ""
    for title_cap in (60, 42, 30, 20, 14):
        caption = _build_caption_with_limit(batch, title_cap)
        if len(caption) <= 980:
            break
    if len(caption) > 1020:
        caption = caption[:1016] + "…"

    n = len(batch)
    inline_keyboard: List[List[Dict[str, Any]]] = []
    for i, d in enumerate(batch):
        icon = NUM_ICONS[i] if i < len(NUM_ICONS) else f"{i + 1}."
        short_t = str(d.get("short_title") or shorten_card_title(str(d.get("clean_title") or ""), 13))
        if len(short_t) > 13:
            short_t = short_t[:12].rstrip() + "…"
        disc_r = int(d.get("discount_rate", 0) or 0)
        sale_p = int(d.get("sale_price", 0) or 0)
        rate_part = f"{disc_r}%↓ " if disc_r > 0 else ""
        price_part = f"{sale_p:,}원" if sale_p >= 1000 else "확인"
        urgent_emo = "🚨" if is_half_off_mega(d) else ("💎" if is_high_ticket_mega(d) else "")
        if n == 1 and urgent_emo:
            btn_text = f"{urgent_emo} {short_t} · {rate_part}{price_part} ↗"
        elif urgent_emo:
            btn_text = f"{icon} {urgent_emo}{short_t} · {rate_part}{price_part} ↗"
        else:
            btn_text = f"{icon} {short_t} · {rate_part}{price_part} ↗"
        url = str(d.get("deeplink") or "").strip()
        if url:
            inline_keyboard.append([{"text": btn_text, "url": url, "style": "success"}])

    return caption, inline_keyboard
