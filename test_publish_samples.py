import os
import sys
import io
import re
import html
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8')
load_dotenv()

from coupang_api import CoupangPartnersAPI
coupang_api = CoupangPartnersAPI()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_PUBLIC_CHANNEL_ID", "@HOTpang911")

def esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)

def parse_deal_metrics(title: str, body: str, price: int):
    """제목과 본문에서 할인율(%) 및 개당 단가 자동 계산"""
    # 1. 할인율(%) 추출
    discount_match = re.search(r'(\d+)%\s*(?:할인|dc|DC)?', title + ' ' + body, re.IGNORECASE)
    discount_str = f"🔥 <b>{discount_match.group(1)}% 파격할인!</b>" if discount_match else ""

    # 2. 수량(개, 봉, 팩, 캔, 병, 포, 박스, 롤) 추출해서 개당 단가 계산
    unit_match = re.search(r'(\d+)\s*(개|봉|팩|캔|병|포|박스|롤|단|통)', title)
    unit_price_str = ""
    if unit_match and price > 0:
        count = int(unit_match.group(1))
        unit_name = unit_match.group(2)
        if count > 1:
            u_price = round(price / count)
            unit_price_str = f"💰 <b>가성비 단가</b>: <b>{unit_name}당 약 {u_price:,}원 꼴!</b>"

    return discount_str, unit_price_str

def fetch_and_publish_post(post_id: str, post_title: str):
    url = f"https://bbs.ruliweb.com/market/board/1020/read/{post_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=8)
    if r.status_code != 200:
        return False

    soup = BeautifulSoup(r.text, "html.parser")
    
    # 1. 쿠팡 URL 추출
    coupang_url = None
    source = soup.select_one("div.source_url a")
    if source and "coupang.com" in source.get("href", ""):
        coupang_url = source["href"]
        m = re.search(r"ol=([^&]+)", coupang_url)
        if m:
            import urllib.parse
            coupang_url = urllib.parse.unquote(m.group(1))

    if not coupang_url:
        all_cp = re.findall(r"https?://(?:www\.|m\.)?coupang\.com/vp/products/\d+[^\s\"\'<>]*", r.text)
        if all_cp:
            coupang_url = all_cp[0]

    if not coupang_url:
        print(f"No coupang URL in {post_id}")
        return False

    # 2. 본문 및 이미지
    view_content = soup.select_one("div.view_content")
    reason = "커뮤니티 유저 추천 검증 특가"
    body_text = ""
    img_url = None
    if view_content:
        body_text = view_content.text.strip()
        lines = [l.strip() for l in body_text.splitlines() if l.strip() and not l.strip().startswith("http")]
        if lines:
            reason = " / ".join(lines[:2])
            if len(reason) > 100:
                reason = reason[:97] + "..."
        img = view_content.select_one("img")
        if img:
            img_url = img.get("src")
            if img_url and img_url.startswith("//"):
                img_url = "https:" + img_url

    # 3. 가격 추출 (제목에서 '3,050원' 등 파싱)
    price = 0
    price_match = re.search(r'([\d,]+)\s*원', post_title)
    if price_match:
        price = int(price_match.group(1).replace(",", ""))

    # 4. 할인율 & 개당 단가 계산
    discount_badge, unit_price_line = parse_deal_metrics(post_title, body_text, price)

    # 5. 파트너스 딥링크 생성
    res = coupang_api.create_deeplink([coupang_url], sub_id="sample_smart")
    items = res.get("data", [])
    if not items:
        return False
    deeplink = items[0].get("shortenUrl") or items[0].get("landingUrl")

    # 6. 제목 정리
    clean_title = post_title.replace("[쿠팡]", "").strip()
    clean_title = re.sub(r"\s*\(\d+\)$", "", clean_title)

    # 메시지 조립
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
        f"🚚 <b>배송혜택</b>: 🚀 로켓배송 / 무료배송\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>핫딜 특성상 가격 변동 및 조기 품절될 수 있습니다.</i>\n"
        f"※ 이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
    )

    reply_markup = {
        "inline_keyboard": [
            [{"text": "👉 특가 혜택 및 수량 확인하기", "url": deeplink}]
        ]
    }

    if img_url:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            json={
                "chat_id": CHANNEL_ID,
                "photo": img_url,
                "caption": msg,
                "parse_mode": "HTML",
                "reply_markup": reply_markup
            }
        ).json()
    else:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHANNEL_ID,
                "text": msg,
                "parse_mode": "HTML",
                "reply_markup": reply_markup
            }
        ).json()

    print(f"Posted {post_id} -> {resp.get('ok')}")
    return resp.get("ok", False)

# 실제 핫딜 2건 즉시 채널로 전송!
print("1번 핫딜 전송 중: 열라면...")
fetch_and_publish_post("108640", "[쿠팡] 오뚜기 열라면 120g, 5봉 (3,050원/무료) (3)")

print("2번 핫딜 전송 중: 토마토 케챂...")
fetch_and_publish_post("108515", "[쿠팡] 오뚜기 토마토 케챂, 800g, 2개 (4,680원/무료) (5)")
