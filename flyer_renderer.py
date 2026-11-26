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


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    bg: Tuple[int, int, int],
    fg: Tuple[int, int, int],
    pad_x: int = 24,
    pad_y: int = 12,
    radius: int = 999,
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
    draw.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=bg)
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

            # 1) productId 정확 일치 최우선
            if product_id:
                for it in pdata:
                    if str(it.get("productId") or "") == str(product_id) and it.get("productImage"):
                        pil = _download_clean_image(it["productImage"])
                        if pil is not None:
                            return pil

            # 2) 핵심 브랜드/상품명 토큰 일치 + 합리적 가격대 일치
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
    """전단지 카드 1줄용 핵심 상품명 축약"""
    t = re.sub(r"\([^\)]*\)", "", clean_title or "")
    t = re.sub(r"\[[^\]]*\]", "", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.-/")
    if len(t) <= max_len:
        return t or clean_title[:max_len]
    # 단어 경계 기준 자연스럽게 자르기
    cut = t[:max_len].rstrip()
    if " " in cut and len(cut.rsplit(" ", 1)[0]) >= 8:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.-/")


def build_card_chip(clean_title: str, orig_price: int, sale_price: int, unit_info: str = "") -> str:
    """전단지 카드용 굵은 초록 칩 문구 (단가 우선, 없으면 절약금액/로켓무배)"""
    clean_unit = re.sub(r"<[^>]+>", "", unit_info or "").strip()
    if clean_unit:
        m_first = re.match(r"^([^\(]+)", clean_unit)
        part = (m_first.group(1) if m_first else clean_unit).replace("약 ", "").strip()
        counts = re.findall(
            r"(\d+)\s*(개입|개|봉|팩|캔|병|포|박스|롤|통|입|매|세트|정|캡슐)(?![a-zA-Z])",
            clean_title,
        )
        if counts:
            c_val, c_u = counts[-1]
            spec = f"{c_val}{c_u}"
            cand = f"{part} · {spec}"
            return cand if len(cand) <= 18 else part
        return part
    save_amt = max(0, orig_price - sale_price)
    if save_amt >= 1000:
        return f"{save_amt:,}원 절약 · 로켓무배"
    return "로켓무료배송 특가"


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


def _draw_deal_card(
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
    card_w = cx2 - cx1
    card_h = cy2 - cy1

    # Soft shadow + card body
    draw.rounded_rectangle([cx1 + 4, cy1 + 8, cx2 + 4, cy2 + 8], radius=30, fill=(216, 224, 236))
    draw.rounded_rectangle([cx1, cy1, cx2, cy2], radius=30, fill=(255, 255, 255), outline=(226, 232, 240), width=3)

    f_num = _load_font(hv_font, 48)
    f_tag = _load_font(hv_font, 42)

    _draw_pill(draw, cx1 + 22, cy1 + 20, f"{idx_num}", f_num, bg=(15, 23, 42), fg=(255, 255, 255), pad_x=24, pad_y=12, radius=18)

    disc_r = int(deal.get("discount_rate", 0) or 0)
    t_lbl, t_col = _tier_badge(disc_r) if disc_r >= 20 else ("검증특가", (37, 99, 235))
    ttw, _ = _text_size(draw, t_lbl, f_tag)
    _draw_pill(draw, cx2 - 22 - (ttw + 48), cy1 + 20, t_lbl, f_tag, bg=t_col, fg=(255, 255, 255), pad_x=24, pad_y=12, radius=18)

    # Dynamic vertical anchors anchored from card bottom so 1/2/3/4 layouts never overlap
    box_h = 232
    box_x1, box_y1 = cx1 + 22, cy2 - 20 - box_h
    box_x2, box_y2 = cx2 - 22, cy2 - 20

    chip_y = box_y1 - 80
    title_y = chip_y - 72

    img_box = (cx1 + 42, cy1 + 116, cx2 - 42, title_y - 18)
    _paste_fit_center(canvas, prod_img, img_box)

    short_title = deal.get("short_title") or shorten_card_title(deal.get("clean_title", ""))
    avail_title_w = card_w - 48
    f_title = _fit_font(draw, short_title, hv_font, 50, avail_title_w, min_size=34)
    tb = draw.textbbox((0, 0), short_title, font=f_title)
    draw.text((cx1 + 24 - tb[0], title_y - tb[1]), short_title, font=f_title, fill=(15, 23, 42))

    orig_p = int(deal.get("orig_price", 0) or 0)
    sale_p = int(deal.get("sale_price", 0) or 0)
    chip_txt = deal.get("chip") or build_card_chip(
        deal.get("clean_title", ""), orig_p, sale_p, deal.get("unit_info", "")
    )
    f_chip = _fit_font(draw, chip_txt, hv_font, 38, avail_title_w - 44, min_size=26)
    _draw_pill(draw, cx1 + 24, chip_y, chip_txt, f_chip, bg=(220, 252, 231), fg=(4, 120, 87), pad_x=22, pad_y=12, radius=14)

    is_hot = disc_r >= 40
    box_bg = (255, 241, 242) if is_hot else (248, 250, 252)
    box_bd = (254, 205, 211) if is_hot else (203, 213, 225)
    draw.rounded_rectangle([box_x1, box_y1, box_x2, box_y2], radius=22, fill=box_bg, outline=box_bd, width=3)

    avail_box_w = (box_x2 - box_x1) - 48
    l1_x = box_x1 + 24
    l1_y = box_y1 + 24

    if orig_p > sale_p >= 1000:
        save_p = orig_p - sale_p
        orig_txt = f"{orig_p:,}원"
        save_txt = f"{save_p:,}원↓"
        r1_sz = 40
        while r1_sz > 26:
            f_o = _load_font(eb_font, r1_sz)
            f_s = _load_font(hv_font, r1_sz)
            if _text_size(draw, orig_txt, f_o)[0] + 54 + _text_size(draw, save_txt, f_s)[0] <= avail_box_w:
                break
            r1_sz -= 2
        f_o = _load_font(eb_font, r1_sz)
        f_s = _load_font(hv_font, r1_sz)

        ob = draw.textbbox((0, 0), orig_txt, font=f_o)
        ow, oh = ob[2] - ob[0], ob[3] - ob[1]
        draw.text((l1_x - ob[0], l1_y - ob[1]), orig_txt, font=f_o, fill=(100, 116, 139))
        mid_y = l1_y + oh // 2
        draw.line([(l1_x - 4, mid_y), (l1_x + ow + 4, mid_y)], fill=(100, 116, 139), width=4)

        arr_x = l1_x + ow + 14
        _draw_right_arrow(draw, arr_x, mid_y, 34, (225, 29, 72))

        sb = draw.textbbox((0, 0), save_txt, font=f_s)
        draw.text((arr_x + 48 - sb[0], l1_y - sb[1]), save_txt, font=f_s, fill=(225, 29, 72))
    else:
        f_sub = _load_font(eb_font, 36)
        draw.text((l1_x, l1_y), "커뮤니티 실시간 검증 특가", font=f_sub, fill=(100, 116, 139))

    l2_y = box_y1 + 98
    r_str = f"{disc_r}%" if disc_r > 0 else "특가"
    p_str = f"{sale_p:,}원" if sale_p >= 1000 else "최저가"
    r2_sz = 88
    while r2_sz > 50:
        f_r2 = _load_font(hv_font, r2_sz)
        if _text_size(draw, r_str, f_r2)[0] + 28 + _text_size(draw, p_str, f_r2)[0] <= avail_box_w:
            break
        r2_sz -= 2
    f_r2 = _load_font(hv_font, r2_sz)

    rb = draw.textbbox((0, 0), r_str, font=f_r2)
    rw, rh = rb[2] - rb[0], rb[3] - rb[1]
    draw.text((l1_x - rb[0], l2_y - rb[1]), r_str, font=f_r2, fill=(225, 29, 72))

    pb = draw.textbbox((0, 0), p_str, font=f_r2)
    pw = pb[2] - pb[0]
    px_right = (box_x2 - 24) - pw - pb[0]
    draw.text((px_right, l2_y + rh - (pb[3] - pb[1]) - pb[1]), p_str, font=f_r2, fill=(15, 23, 42))


def _draw_brand_promo_card(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    cx1: int,
    cy1: int,
    cx2: int,
    cy2: int,
    hv_font: Optional[str],
    eb_font: Optional[str],
) -> None:
    """3건 전단지일 때 4번째 빈 칸에 넣는 @HOTpang911 브랜드 안내 카드"""
    draw.rounded_rectangle([cx1 + 4, cy1 + 8, cx2 + 4, cy2 + 8], radius=30, fill=(203, 213, 225))
    draw.rounded_rectangle([cx1, cy1, cx2, cy2], radius=30, fill=(15, 23, 42), outline=(30, 41, 59), width=3)

    card_w = cx2 - cx1
    card_h = cy2 - cy1
    f_badge = _load_font(hv_font, 36)
    f_big = _load_font(hv_font, 60)
    f_sub = _load_font(eb_font, 38)

    bw_txt = "실시간 핫딜 채널"
    btw, _ = _text_size(draw, bw_txt, f_badge)
    _draw_pill(draw, cx1 + (card_w - (btw + 52)) // 2, cy1 + int(card_h * 0.24), bw_txt, f_badge, bg=(225, 29, 72), fg=(255, 255, 255), pad_x=26, pad_y=14)

    for dy, line, col, fn in (
        (0.41, "20%+ 진짜 특가만", (255, 255, 255), f_big),
        (0.52, "선별해서 올립니다", (226, 232, 240), f_big),
        (0.67, "@HOTpang911", (74, 222, 128), f_sub),
    ):
        lw, _ = _text_size(draw, line, fn)
        bb = draw.textbbox((0, 0), line, font=fn)
        draw.text((cx1 + (card_w - lw) // 2 - bb[0], cy1 + int(card_h * dy) - bb[1]), line, font=fn, fill=col)


def render_hotdeal_flyer(deals: List[Dict[str, Any]], coupang_api: Any = None) -> bytes:
    """1~4건 커뮤니티 핫딜 목록을 v5 빅타이포 전단지 PNG 바이트로 렌더링"""
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

    if n >= 3:
        W, H = 1800, 2120
        cols, rows = 2, 2
        out_size = (900, 1060)
        hdr_title = f"쿠팡 핫딜 {n}선"
    elif n == 2:
        W, H = 1800, 1160
        cols, rows = 2, 1
        out_size = (900, 580)
        hdr_title = "쿠팡 핫딜 2선"
    else:
        W, H = 1200, 1360
        cols, rows = 1, 1
        out_size = (600, 680)
        hdr_title = "쿠팡 긴급 핫딜"

    header_h = 184
    canvas = Image.new("RGB", (W, H), (241, 245, 249))
    _draw_vertical_gradient(canvas, (0, 0, W, header_h), (11, 18, 33), (30, 41, 59))
    draw = ImageDraw.Draw(canvas)

    f_hdr = _load_font(hv_font, 82 if n >= 2 else 74)
    f_hdr_badge = _load_font(hv_font, 50 if n >= 2 else 44)
    hb = draw.textbbox((0, 0), hdr_title, font=f_hdr)
    draw.text((48, (header_h - (hb[3] - hb[1])) // 2 - hb[1]), hdr_title, font=f_hdr, fill=(255, 255, 255))

    max_rate = max(int(d.get("discount_rate", 0) or 0) for d in batch)
    badge_txt = f"최대 {max_rate}%↓" if n >= 2 else (f"{max_rate}%↓ 특가" if max_rate > 0 else "긴급 특가")
    btw, _ = _text_size(draw, badge_txt, f_hdr_badge)
    _draw_pill(draw, W - 48 - (btw + 68), 34, badge_txt, f_hdr_badge, bg=(225, 29, 72), fg=(255, 255, 255), pad_x=34, pad_y=22)

    margin_x = 34
    margin_top = header_h + 28
    margin_bot = 30
    gap = 38
    card_w = (W - margin_x * 2 - gap * (cols - 1)) // cols
    card_h = (H - margin_top - margin_bot - gap * (rows - 1)) // rows

    for idx in range(cols * rows):
        row, col = divmod(idx, cols)
        cx1 = margin_x + col * (card_w + gap)
        cy1 = margin_top + row * (card_h + gap)
        cx2 = cx1 + card_w
        cy2 = cy1 + card_h
        if idx < n:
            _draw_deal_card(canvas, draw, cx1, cy1, cx2, cy2, idx + 1, batch[idx], prod_imgs[idx], hv_font, eb_font)
        elif n == 3 and idx == 3:
            _draw_brand_promo_card(canvas, draw, cx1, cy1, cx2, cy2, hv_font, eb_font)

    final = canvas.resize(out_size, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    final.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _build_caption_with_limit(batch: List[Dict[str, Any]], max_title_len: int) -> str:
    n = len(batch)
    header = f"🔥 <b>[쿠팡 실시간 핫딜 {n}선]</b>" if n >= 2 else "🔥 <b>[쿠팡 긴급 핫딜]</b>"
    lines = [header, ""]

    for i, d in enumerate(batch):
        full_t = str(d.get("clean_title") or "쿠팡 핫딜").strip()
        if len(full_t) > max_title_len:
            full_t = full_t[: max_title_len - 1].rstrip() + "…"
        url = str(d.get("deeplink") or "").strip()
        orig_p = int(d.get("orig_price", 0) or 0)
        sale_p = int(d.get("sale_price", 0) or 0)
        disc_r = int(d.get("discount_rate", 0) or 0)
        unit_raw = re.sub(r"<[^>]+>", "", str(d.get("unit_info") or "")).strip()

        icon = NUM_ICONS[i] if i < len(NUM_ICONS) else f"{i + 1}."
        if url:
            lines.append(f'<b>{icon} <a href="{html.escape(url, quote=True)}">{html.escape(full_t, quote=False)}</a></b>')
        else:
            lines.append(f"<b>{icon} {html.escape(full_t, quote=False)}</b>")

        emo = _tier_emoji(disc_r) if disc_r >= 20 else "🔥"
        price_str = f"<b>{sale_p:,}원</b>" if sale_p >= 1000 else "<b>특가 확인</b>"
        if orig_p > sale_p >= 1000 and disc_r >= 3:
            orig_part = f"<s>{orig_p:,}원</s> → "
            rate_part = f"{emo}<b>{disc_r}%↓</b> "
            save_amt = orig_p - sale_p
            if unit_raw:
                m_u = re.match(r"^([^\(]+)", unit_raw)
                u_short = (m_u.group(1) if m_u else unit_raw).replace("약 ", "").strip()
                extra = f" · {html.escape(u_short, quote=False)}"
            else:
                extra = f" ({save_amt:,}원 절약)"
        else:
            orig_part = ""
            rate_part = f"{emo} "
            extra = f" · {html.escape(unit_raw, quote=False)}" if unit_raw else ""

        lines.append(f"   {rate_part}{orig_part}{price_str}{extra}")
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
        btn_text = f"{icon} {short_t} · {rate_part}{price_part} ↗"
        url = str(d.get("deeplink") or "").strip()
        if url:
            inline_keyboard.append([{"text": btn_text, "url": url, "style": "success"}])

    return caption, inline_keyboard
