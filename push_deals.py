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
        recent = list(seen)[-1000:]  # 최근 1000개 유지
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(recent, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"seen_deals 저장 에러: {e}")


def parse_deal_metrics(title: str, body: str, price: int):
    """제목/본문에서 할인율 및 단위당 단가(봉당/개당) 자동 계산"""
    discount_match = re.search(r"(\d+)%\s*(?:할인|dc|DC)?", title + " " + body, re.IGNORECASE)
    discount_str = f"🔥 <b>{discount_match.group(1)}% 파격할인!</b>" if discount_match else ""

    unit_match = re.search(r"(\d+)\s*(개|봉|팩|캔|병|포|박스|롤|단|통)", title)
    unit_price_str = ""
    if unit_match and price > 0:
        count = int(unit_match.group(1))
        unit_name = unit_match.group(2)
        if count > 1:
            u_price = round(price / count)
            unit_price_str = f"💰 <b>가성비 단가</b>: <b>{unit_name}당 약 {u_price:,}원 꼴!</b>"

    return discount_str, unit_price_str


def send_telegram(
    chat_id: str,
    text: str,
    photo_url: Optional[str] = None,
    button_text: Optional[str] = None,
    button_url: Optional[str] = None,
) -> bool:
    """텔레그램 메시지/사진 전송 (HTTP POST)"""
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

        # 쿠팡 URL 추출
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

        # 본문 및 이미지
        body_text = ""
        reason = "커뮤니티 유저 추천 검증 특가"
        img_url = None
        vc = soup.select_one("div.view_content")
        if vc:
            body_text = vc.text.strip()
            lines = [l.strip() for l in body_text.splitlines() if l.strip() and not l.strip().startswith("http")]
            if lines:
                reason = " / ".join(lines[:2])
                if len(reason) > 100:
                    reason = reason[:97] + "..."
            img = vc.select_one("img")
            if img:
                img_url = img.get("src")
                if img_url and img_url.startswith("//"):
                    img_url = "https:" + img_url

        return {"coupang_url": cp_url, "reason": reason, "body_text": body_text, "img_url": img_url}
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

        # 쿠팡 URL 추출
        cp_url = None
        all_cp = re.findall(r"https?://(?:www\.|m\.)?coupang\.com/vp/products/\d+[^\s\"\'<>]*", res.text)
        if all_cp:
            cp_url = all_cp[0]

        if not cp_url:
            return None

        # 본문 이유
        reason = "뽐뿌 유저 추천 가성비 특가"
        img_url = None
        content_td = soup.select_one("td.board-contents")
        body_text = ""
        if content_td:
            body_text = content_td.text.strip()
            lines = [l.strip() for l in body_text.splitlines() if l.strip() and not l.strip().startswith("http")]
            if lines:
                reason = " / ".join(lines[:2])
                if len(reason) > 100:
                    reason = reason[:97] + "..."
            img = content_td.select_one("img")
            if img:
                img_url = img.get("src")
                if img_url and img_url.startswith("//"):
                    img_url = "https:" + img_url

        return {"coupang_url": cp_url, "reason": reason, "body_text": body_text, "img_url": img_url}
    except Exception as e:
        logger.error(f"뽐뿌 상세 파싱 오류 ({url}): {e}")
        return None


# ==========================================
# 3. 핫딜 송출 엔진 (누락 없이 전수 처리)
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
    reason = detail.get("reason", "커뮤니티 추천 초특가 핫딜")
    body_text = detail.get("body_text", "")
    img_url = detail.get("img_url")
    title = d["title"]

    # 가격 파싱 (예: 3,050원)
    price = 0
    price_match = re.search(r"([\d,]+)\s*원", title)
    if price_match:
        price = int(price_match.group(1).replace(",", ""))

    # 할인율 & 가성비 단가 계산
    discount_badge, unit_price_line = parse_deal_metrics(title, body_text, price)

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

    # 제목 정리
    clean_title = re.sub(r"^\[.*?\]\s*", "", title)  # [쿠팡] 등 태그 제거
    clean_title = re.sub(r"\s*\(\d+\)$", "", clean_title)  # (3) 댓글수 제거

    metrics_parts = []
    if discount_badge:
        metrics_parts.append(discount_badge)
    if unit_price_line:
        metrics_parts.append(unit_price_line)
    metrics_block = "\n".join(metrics_parts) + "\n" if metrics_parts else ""

    price_line = f"🏷️ <b>특가가격</b>: <b>{price:,}원</b>" if price > 0 else "🏷️ <b>특가가격</b>: <b>한정수량 최저가</b>"

    msg = (
        f"🔥 <b>[실시간 커뮤니티 검증 핫딜]</b>\n\n"
        f"📌 <b>{esc(clean_title)}</b>\n\n"
        f"{price_line}\n"
        f"{metrics_block}"
        f"💡 <b>왜 특가인가요?</b>\n"
        f"<code>{esc(reason)}</code>\n\n"
        f"🚚 <b>배송혜택</b>: 🚀 로켓배송 / 무료배송 특가\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>핫딜 특성상 가격 변동 및 조기 품절될 수 있습니다.</i>\n"
        f"※ 이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
    )

    ok = send_telegram(
        chat_id=CHANNEL_ID,
        text=msg,
        photo_url=img_url,
        button_text="👉 특가 혜택 및 수량 확인하기",
        button_url=deeplink,
    )

    if ok:
        seen.add(deal_id)
        save_seen(seen)
        logger.info(f"✅ [{source_name}] 송출 완료: {clean_title}")
        import time
        time.sleep(2)  # 연속 송출 방지
        return True

    return False


# ==========================================
# 4. 아침 골드박스 & 밤 23시 결산 리포트
# ==========================================
def run_goldbox_if_morning(seen: Set[str]) -> int:
    """오전 7~8시 KST에 골드박스 상위 3개 송출"""
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

        msg = (
            f"🍯 <b>[오늘의 골드박스 특가] | 🏆 랭킹 {i+1}위</b>\n\n"
            f"📌 <b>{esc(p_name)}</b>\n\n"
            f"📂 <b>카테고리</b>: {esc(cat)}\n"
            f"💰 <b>특가가격</b>: <b>{p_price:,}원</b>\n"
            f"🚚 <b>배송혜택</b>: 🚀 로켓배송 (내일 도착 보장)\n"
            f"⏰ <b>한정특가</b>: 오늘 자정(24:00)까지 또는 한정수량 마감\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"💡 <i>정확한 옵션별 잔여수량과 추가 쿠폰은 아래 링크에서 확인하세요.</i>\n"
            f"※ 이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
        )

        if send_telegram(CHANNEL_ID, msg, img_url, "👉 수량/옵션 및 최저가 확인하기", p_url):
            sent += 1
            import time
            time.sleep(2)

    if sent > 0:
        seen.add(today_key)
        save_seen(seen)
    return sent


def run_daily_report_if_night():
    """밤 23시 KST에 Dx님 개인 톡으로 일일 결산 보고 전송"""
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
        est_commission = round(total_gmv * 0.03)  # 파트너스 기본 수수료 약 3%

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


# ==========================================
# 메인 실행 진입점
# ==========================================
def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN이 없습니다.")
        sys.exit(1)

    logger.info("🚀 핫딜 수집 및 송출 작업 시작 (누락 없이 전수 검사)...")
    seen = load_seen()
    total_sent = 0

    # 1. 루리웹 핫딜 누락 없이 전수 검사 & 송출
    r_deals = collect_ruliweb_deals()
    for d in r_deals:
        if process_deal(d, parse_ruliweb_detail, "ruliweb", seen):
            total_sent += 1

    # 2. 뽐뿌 RSS 핫딜 누락 없이 전수 검사 & 송출
    p_deals = collect_ppomppu_deals()
    for d in p_deals:
        if process_deal(d, parse_ppomppu_detail, "ppomppu", seen):
            total_sent += 1

    # 3. 아침 골드박스 (해당 시간대인 경우)
    gb_sent = run_goldbox_if_morning(seen)
    total_sent += gb_sent

    # 4. 밤 23시 일일 결산 리포트 (해당 시간대인 경우)
    run_daily_report_if_night()

    logger.info(f"🎉 모든 작업 완료! (새로 송출된 핫딜 총 {total_sent}건)")


if __name__ == "__main__":
    main()
