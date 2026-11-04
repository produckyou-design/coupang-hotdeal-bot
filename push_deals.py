import html
import io
import json
import logging
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# 콘솔 인코딩 대응
sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding="utf-8", errors="replace")

load_dotenv()

from coupang_api import CoupangPartnersAPI

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# 설정값
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_PUBLIC_CHANNEL_ID", "@HOTpang911")
ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "7525598874")

coupang_api = CoupangPartnersAPI()
SEEN_FILE = os.path.join(os.path.dirname(__file__), "seen_deals.json")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


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


def extract_ocr_text(img_url: Optional[str]) -> str:
    """첨부된 이미지(쿠팡 상세 캡처 등)에서 OCR로 정상가, 할인율(%), 가격 등을 추출"""
    if not img_url:
        return ""
    try:
        res = requests.get(img_url, headers=HEADERS, timeout=7)
        if res.status_code != 200:
            return ""

        from PIL import Image
        img = Image.open(io.BytesIO(res.content))

        # 1) Windows 환경: winocr 호출
        try:
            import winocr
            import asyncio
            async def _winocr():
                return await winocr.recognize_pil(img, lang="ko")
            res_obj = asyncio.run(_winocr())
            if res_obj and res_obj.text:
                logger.info("📸 [OCR] 이미지 텍스트 인식 성공 (Windows)")
                return res_obj.text
        except Exception:
            pass

        # 2) Linux/GitHub Actions 환경: pytesseract 호출
        try:
            import pytesseract
            text = pytesseract.image_to_string(img, lang="kor+eng")
            if text:
                logger.info("📸 [OCR] 이미지 텍스트 인식 성공 (Tesseract)")
                return text
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"OCR 추출 실패 ({img_url}): {e}")

    return ""


def parse_pricing_details(title: str, body: str, ocr_text: str = "") -> Dict[str, Any]:
    """
    제목, 본문, 이미지 OCR 텍스트에서 가격 메리트(정상가, 할인율, 최종특가, 가성비 단가, 핫딜 포인트)를 분석.
    """
    full_text = f"{title}\n{body}\n{ocr_text}"

    # 1. 정상가 -> 할인가 화살표 패턴 (예: 21,900원 -> 12,900원)
    orig_price = 0
    sale_price = 0
    arrow_match = re.search(r"([\d,]+)\s*원?\s*(?:에서|->|→|~>)\s*([\d,]+)\s*원", full_text)
    if arrow_match:
        p1 = int(arrow_match.group(1).replace(",", ""))
        p2 = int(arrow_match.group(2).replace(",", ""))
        if p1 > p2:
            orig_price = p1
            sale_price = p2

    # 정상가 키워드 체크 (예: 정가 65,000원, 마트가 15,000원, 소비자가 등)
    if orig_price == 0:
        orig_match = re.search(r"(?:정가|원가|정상가|마트가|기존가|소비자가|시중가|출고가)\s*[:~약]?\s*([\d,]+)\s*원?", full_text)
        if orig_match:
            orig_price = int(orig_match.group(1).replace(",", ""))

    # 판매가(현재 표시가격) 찾기
    if sale_price == 0:
        title_prices = [int(p.replace(",", "")) for p in re.findall(r"([\d,]+)\s*원", title) if int(p.replace(",", "")) >= 1000]
        ocr_prices = [int(p.replace(",", "")) for p in re.findall(r"([\d,]+)\s*원", ocr_text) if int(p.replace(",", "")) >= 1000]

        if ocr_prices:
            sale_price = ocr_prices[0]
        elif title_prices:
            sale_price = title_prices[-1]
        else:
            m_body_price = re.search(r"(?:가격|특가|판매가|구매가)?\s*[:~약]?\s*([\d,]+)\s*원", body)
            if m_body_price:
                sale_price = int(m_body_price.group(1).replace(",", ""))

    # 2. 할인율(%) 계산 또는 추출
    discount_rate = 0
    disc_match = re.search(r"(\d+)%\s*(?:할인|dc|DC)?", full_text, re.IGNORECASE)
    if disc_match:
        discount_rate = int(disc_match.group(1))

    # 정상가가 없고 할인율(%)과 판매가가 있는 경우 -> 정상가 역산 복원!
    if orig_price == 0 and discount_rate >= 10 and sale_price > 0:
        raw_orig = sale_price / (1 - discount_rate / 100)
        orig_price = int(round(raw_orig, -1))
    elif orig_price > 0 and sale_price > 0 and orig_price > sale_price and discount_rate == 0:
        discount_rate = round((orig_price - sale_price) / orig_price * 100)

    # 3. 체감 최종가 계산 (쿠폰/할인 적용 시 최종 도달 금액만 추출, 긴 설명문 배제)
    final_price = sale_price
    final_matches = re.finditer(r"(?:체감가|최종가|실구매가|적용가)?\s*[:~약]?\s*([\d,]+)\s*원(?:까지)?\s*(?:떨어|내려|나옵|가능)", body)
    for fm in final_matches:
        val = int(fm.group(1).replace(",", ""))
        if 0 < val < sale_price:
            final_price = val
            break

    if final_price == sale_price:
        coupon_match = re.search(r"쿠폰\s*[\w\s]*(?:받으면|적용|다운|먹이면)?\s*([\d,]+|천|만)\s*원?\s*(?:할인|추가할인)?", body)
        if coupon_match:
            c_raw = coupon_match.group(0).strip()
            c_amt = 0
            if "천원" in c_raw or "천 원" in c_raw:
                c_amt = 1000
            elif "만원" in c_raw:
                c_amt = 10000
            else:
                m_num = re.search(r"([\d,]+)\s*원", c_raw)
                if m_num:
                    c_amt = int(m_num.group(1).replace(",", ""))
            if c_amt > 0 and sale_price > 0:
                final_price = max(0, sale_price - c_amt)

    # 4. 가성비 단가 계산
    unit_info = ""
    eff_p = final_price if 0 < final_price < sale_price else sale_price
    if eff_p > 0:
        units = re.findall(r"(\d+)\s*(개|봉|팩|캔|병|포|박스|롤|통|입|매)", title)
        if units:
            count = int(units[-1][0])
            unit_name = units[-1][1]
            if count > 1:
                u_price = round(eff_p / count)
                unit_info = f"{unit_name}당 약 {u_price:,}원 꼴!"

        if not unit_info:
            weight_match = re.search(r"(\d+(?:\.\d+)?)\s*(kg|l|리터|킬로)", title, re.IGNORECASE)
            if weight_match:
                val = float(weight_match.group(1))
                if val > 0:
                    unit_p = round(eff_p / (val * 10))
                    unit_info = f"100g/ml당 약 {unit_p:,}원 꼴!"
            else:
                gram_match = re.search(r"(\d+)\s*(g|ml|그램)", title, re.IGNORECASE)
                if gram_match:
                    val = int(gram_match.group(1))
                    if val >= 100:
                        unit_p = round(eff_p / (val / 100))
                        unit_info = f"100g당 약 {unit_p:,}원 꼴!"

    # 5. 핫딜 포인트 (핵심 메리트 문장만 간결하게 추출)
    hotdeal_points = []
    lines = [l.strip() for l in body.splitlines() if l.strip() and not l.strip().startswith("http")]
    for line in lines:
        if any(bad in line for bad in ["맛있", "맛나", "좋네", "샀습", "추천합", "감사", "양도 많고"]):
            continue
        if any(kw in line for kw in ["최저가", "역대", "마트", "당근", "가성비", "단가", "무배", "품절", "싼듯", "저렴"]):
            hotdeal_points.append(line)

    point_text = ""
    if hotdeal_points:
        combined = " / ".join(hotdeal_points[:2])
        if len(combined) > 70:
            combined = combined[:67] + "..."
        point_text = combined

    return {
        "orig_price": orig_price,
        "sale_price": sale_price,
        "final_price": final_price,
        "discount_rate": discount_rate,
        "unit_info": unit_info,
        "point_text": point_text,
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

    try:
        if photo_url:
            payload["photo"] = photo_url
            payload["caption"] = text
            res = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", json=payload, timeout=10
            ).json()
        else:
            payload["text"] = text
            res = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=10
            ).json()

        return res.get("ok", False)
    except Exception as e:
        logger.error(f"텔레그램 전송 실패: {e}")
        return False


# ==========================================
# 1. 루리웹 핫딜 수집기
# ==========================================
def collect_ruliweb_deals() -> List[Dict[str, Any]]:
    deals = []
    url = "https://bbs.ruliweb.com/market/board/1020"
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        if res.status_code != 200:
            return deals

        soup = BeautifulSoup(res.text, "html.parser")
        for row in soup.select("tr.table_body"):
            s = row.select_one("a.subject_link")
            if not s:
                continue
            title = " ".join(s.text.split())
            if "쿠팡" not in title and "coupang" not in title.lower():
                continue

            link = s.get("href", "")
            m = re.search(r"/read/(\d+)", link)
            pid = f"ruliweb_{m.group(1)}" if m else link
            deals.append({"id": pid, "title": title, "link": link})
    except Exception as e:
        logger.error(f"루리웹 수집 오류: {e}")

    return deals


def parse_ruliweb_detail(url: str) -> Optional[Dict[str, Any]]:
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        if res.status_code != 200:
            return None
        soup = BeautifulSoup(res.text, "html.parser")

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
        img_url = None
        vc = soup.select_one("div.view_content")
        if vc:
            body_text = vc.text.strip()
            img = vc.select_one("img")
            if img:
                img_url = img.get("src")
                if img_url and img_url.startswith("//"):
                    img_url = "https:" + img_url

        return {"coupang_url": cp_url, "body_text": body_text, "img_url": img_url}
    except Exception as e:
        logger.error(f"루리웹 상세 파싱 오류 ({url}): {e}")
        return None


# ==========================================
# 2. 뽐뿌 RSS 핫딜 수집기
# ==========================================
def collect_ppomppu_deals() -> List[Dict[str, Any]]:
    deals = []
    url = "https://www.ppomppu.co.kr/rss.php?id=ppomppu"
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        if res.status_code != 200:
            return deals

        res.encoding = "utf-8"
        root = ET.fromstring(res.text)

        for it in root.findall(".//item"):
            title = it.find("title").text if it.find("title") is not None else ""
            link = it.find("link").text if it.find("link") is not None else ""
            if "쿠팡" not in title and "coupang" not in title.lower():
                continue

            m = re.search(r"no=(\d+)", link)
            pid = f"ppomppu_{m.group(1)}" if m else link
            deals.append({"id": pid, "title": title, "link": link})
    except Exception as e:
        logger.error(f"뽐뿌 RSS 수집 오류: {e}")

    return deals


def parse_ppomppu_detail(url: str) -> Optional[Dict[str, Any]]:
    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        if res.status_code != 200:
            return None
        res.encoding = "euc-kr"
        soup = BeautifulSoup(res.text, "html.parser")

        cp_url = None
        all_cp = re.findall(r"https?://(?:www\.|m\.)?coupang\.com/vp/products/\d+[^\s\"\'<>]*", res.text)
        if all_cp:
            cp_url = all_cp[0]

        if not cp_url:
            return None

        img_url = None
        content_td = soup.select_one("td.board-contents")
        body_text = ""
        if content_td:
            body_text = content_td.text.strip()
            img = content_td.select_one("img")
            if img:
                img_url = img.get("src")
                if img_url and img_url.startswith("//"):
                    img_url = "https:" + img_url

        return {"coupang_url": cp_url, "body_text": body_text, "img_url": img_url}
    except Exception as e:
        logger.error(f"뽐뿌 상세 파싱 오류 ({url}): {e}")
        return None


# ==========================================
# 3. 핫딜 송출 엔진
# ==========================================
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
    title = d["title"]

    # 1. 본문 이미지 OCR 텍스트 추출 (정상가, 할인율 팩트 확인)
    ocr_text = extract_ocr_text(img_url)

    # 2. 가격 및 메리트 종합 분석
    p_info = parse_pricing_details(title, body_text, ocr_text)

    # 3. 가격 메리트 필터링 (정상가/할인율/단가/핫딜포인트 없는 글 제외)
    has_merit = any([
        p_info["orig_price"] > 0,
        p_info["discount_rate"] > 0,
        bool(p_info["unit_info"]),
        bool(p_info["point_text"]),
    ])

    if not has_merit:
        logger.info(f"⏭️ [스킵] 가격 메리트 불충분: {title}")
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

    # 제목 정리 (태그 및 댓글수 제거)
    clean_title = re.sub(r"^\[.*?\]\s*", "", title)
    clean_title = re.sub(r"\s*\(\d+\)$", "", clean_title)

    # 4. 가격 블록 정밀 구성 (쿠폰/와우 등 불필요한 줄 전부 삭제, 핵심 3~4줄로 초간결화!)
    price_lines = []
    orig_p = p_info["orig_price"]
    sale_p = p_info["sale_price"]
    final_p = p_info["final_price"]
    disc_rate = p_info["discount_rate"]

    # 최종 판매가 결정
    best_price = final_p if 0 < final_p < sale_p else sale_p

    # 1) 상단 헤드라인: 할인율이 명확한 경우
    header_line = "🔥 <b>[실시간 검증 특가]</b>"
    if disc_rate >= 15 and orig_p > 0 and best_price > 0:
        header_line = f"🔥 <b>[{disc_rate}% 파격할인] 정상가 <s>{orig_p:,}원</s> ➡️ 특가 {best_price:,}원!</b>"

    # 2) 정상가격
    if orig_p > 0 and orig_p > best_price:
        save_amt = orig_p - best_price
        price_lines.append(f"• <b>정상가격</b>: <s>{orig_p:,}원</s> (<b>{save_amt:,}원 절약!</b>)")

    # 3) 최종특가
    if best_price > 0:
        disc_text = f" (🔥 <b>{disc_rate}% 할인</b>)" if disc_rate > 0 else ""
        price_lines.append(f"• <b>최종특가</b>: <b>{best_price:,}원</b>{disc_text}")
    else:
        price_lines.append(f"• <b>최종특가</b>: <b>한정수량 최저가</b>")

    # 4) 가성비 단가
    if p_info["unit_info"]:
        price_lines.append(f"• <b>단가계산</b>: <b>{p_info['unit_info']}</b>")

    price_block = "\n".join(price_lines)

    # 5) 핫딜 포인트 (기존 '가격 팩트' 대신 직관적인 문구로 변경)
    point_block = f"💡 <b>핫딜 포인트</b>: <code>{esc(p_info['point_text'])}</code>\n\n" if p_info["point_text"] else ""

    msg = (
        f"{header_line}\n\n"
        f"📌 <b>{esc(clean_title)}</b>\n\n"
        f"💵 <b>특가 정보</b>\n"
        f"{price_block}\n\n"
        f"{point_block}"
        f"🚚 🚀 <b>로켓배송 / 무료배송</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>핫딜 특성상 가격 변동 및 조기 품절될 수 있습니다.</i>\n"
        f"※ 이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
    )

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
        logger.info(f"✅ [{source_name}] 송출 완료: {clean_title}")
        import time
        time.sleep(2)
        return True

    return False


# ==========================================
# 4. 아침 골드박스 & 밤 23시 결산 리포트
# ==========================================
def run_goldbox_if_morning(seen: Set[str]) -> int:
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    today_key = f"gb_{now.strftime('%Y%m%d')}"

    if today_key in seen:
        return 0
    if now.hour != 7 and "--force-goldbox" not in sys.argv:
        return 0

    logger.info("🍯 아침 골드박스 상위 3개 자동 송출 시작...")
    res = coupang_api.get_goldbox_products(sub_id="auto_goldbox")
    items = res.get("data", [])
    if not items:
        return 0

    sent = 0
    for i, it in enumerate(items[:3]):
        p_name = it.get("productName", "")
        p_price = int(it.get("productPrice", 0))
        p_url = it.get("productUrl", "")
        img_url = it.get("productImage")
        cat = it.get("categoryName", "")

        unit_str = ""
        units = re.findall(r"(\d+)\s*(개|봉|팩|캔|병|포|박스|롤|통|입|매)", p_name)
        if units and p_price > 0:
            count = int(units[-1][0])
            unit_name = units[-1][1]
            if count > 1:
                u_p = round(p_price / count)
                unit_str = f"• <b>단가계산</b>: <b>{unit_name}당 약 {u_p:,}원 꼴!</b>\n"

        msg = (
            f"🍯 <b>[오늘의 골드박스 특가] | 🏆 인기 {i+1}위</b>\n\n"
            f"📌 <b>{esc(p_name)}</b>\n\n"
            f"💵 <b>특가 정보</b>\n"
            f"• <b>골드특가</b>: <b>{p_price:,}원</b>\n"
            f"{unit_str}"
            f"• <b>카테고리</b>: {esc(cat)}\n\n"
            f"💡 <b>핫딜 포인트</b>: 오늘 자정(24:00)까지 한정 수량 하루 특가\n\n"
            f"🚚 🚀 <b>로켓배송 (내일 도착 보장)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <i>핫딜 특성상 실시간 잔여수량에 따라 조기 마감될 수 있습니다.</i>\n"
            f"※ 이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
        )

        if send_telegram(CHANNEL_ID, msg, img_url, "👉 실시간 수량 및 최저가 확인", p_url):
            sent += 1
            import time
            time.sleep(2)

    if sent > 0:
        seen.add(today_key)
        save_seen(seen)
    return sent


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

        send_telegram(chat_id=ADMIN_CHAT_ID, text=report_msg)
        logger.info("✅ 일일 결산 리포트 관리자 전송 완료")
    except Exception as e:
        logger.error(f"결산 리포트 조회 오류: {e}")


def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN이 없습니다.")
        sys.exit(1)

    logger.info("🚀 핫딜 수집 및 송출 작업 시작 (초경량 고임팩트 템플릿)...")
    seen = load_seen()
    total_sent = 0

    # 1. 루리웹 핫딜 검사 & 송출
    r_deals = collect_ruliweb_deals()
    for d in r_deals:
        if process_deal(d, parse_ruliweb_detail, "ruliweb", seen):
            total_sent += 1

    # 2. 뽐뿌 RSS 핫딜 검사 & 송출
    p_deals = collect_ppomppu_deals()
    for d in p_deals:
        if process_deal(d, parse_ppomppu_detail, "ppomppu", seen):
            total_sent += 1

    # 3. 아침 골드박스
    gb_sent = run_goldbox_if_morning(seen)
    total_sent += gb_sent

    # 4. 밤 23시 결산 리포트
    run_daily_report_if_night()

    logger.info(f"🎉 모든 작업 완료! (새로 송출된 핫딜 총 {total_sent}건)")


if __name__ == "__main__":
    main()
