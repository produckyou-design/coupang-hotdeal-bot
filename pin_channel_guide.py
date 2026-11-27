"""@HOTpang911 채널 상단 고정 가이드 메시지 생성 및 게시·핀 고정 유틸리티."""

import argparse
import os
import sys
from typing import Any, Dict, List, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

BOT_USERNAME_DEFAULT = "coupang_deal_hub_bot"


def build_guide_payload(
    bot_username: str = BOT_USERNAME_DEFAULT,
) -> Tuple[str, List[List[Dict[str, Any]]]]:
    """채널 상단 고정 가이드 HTML 본문과 딥링크 버튼 3종 반환."""
    uname = str(bot_username or BOT_USERNAME_DEFAULT).strip().lstrip("@")
    text = (
        "📌 <b>[🔥오늘의 핫딜🔥 200% 활용 가이드]</b>\n\n"
        "• 아카라이브·뽐뿌·루리웹·딜바다에서 <b>할인율 검증(20%+, 생필품·식품 저가 15%+)</b>된 딜만 엄선해 올립니다.\n"
        "• 채널 알림이 부담스럽다면 <b>원하는 상품 키워드만 DM 알림</b>으로 받아보세요.\n"
        "• 생수·생필품 <code>100g/1L당</code> 단가 비교도 봇에서 무료로 바로 이용할 수 있습니다."
    )
    buttons: List[List[Dict[str, Any]]] = [
        [{"text": "🔔 맞춤 키워드 알림 등록", "url": f"https://t.me/{uname}?start=alert"}],
        [
            {"text": "⚖️ 배송비 포함 단가 비교", "url": f"https://t.me/{uname}?start=compare"},
            {"text": "💧 생수 2L 자동 비교", "url": f"https://t.me/{uname}?start=water"},
        ],
    ]
    return text, buttons


def send_and_pin_guide(
    chat_id: str,
    bot_token: str,
    bot_username: str = BOT_USERNAME_DEFAULT,
) -> int:
    """채널에 가이드 메시지를 발송하고 무음 핀 고정(message_id 반환)."""
    if not bot_token or not chat_id:
        raise ValueError("BOT_TOKEN 또는 CHANNEL_ID가 비어 있습니다.")

    text, buttons = build_guide_payload(bot_username=bot_username)
    base_api = f"https://api.telegram.org/bot{bot_token}"

    send_res = requests.post(
        f"{base_api}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": buttons},
        },
        timeout=10,
    ).json()
    if not send_res.get("ok"):
        raise RuntimeError(f"sendMessage 실패: {send_res}")

    message_id = int(send_res["result"]["message_id"])
    pin_res = requests.post(
        f"{base_api}/pinChatMessage",
        json={
            "chat_id": chat_id,
            "message_id": message_id,
            "disable_notification": True,
        },
        timeout=10,
    ).json()
    if not pin_res.get("ok"):
        raise RuntimeError(f"pinChatMessage 실패: {pin_res}")

    return message_id


def main() -> int:
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="채널 상단 고정 가이드 메시지 관리")
    parser.add_argument("--send", action="store_true", help="실제 채널에 게시 및 핀 고정")
    parser.add_argument("--dry-run", action="store_true", help="미리보기 출력 (기본)")
    args = parser.parse_args()

    text, buttons = build_guide_payload()
    if not args.send:
        print("=== DRY RUN PREVIEW ===")
        print(text)
        print("BUTTONS:", buttons)
        return 0

    tok = os.getenv("TELEGRAM_BOT_TOKEN", "")
    cid = os.getenv("TELEGRAM_PUBLIC_CHANNEL_ID", "@HOTpang911")
    mid = send_and_pin_guide(cid, tok)
    print(f"채널 가이드 게시 및 무음 핀 고정 완료: chat_id={cid}, message_id={mid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
