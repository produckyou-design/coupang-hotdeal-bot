import asyncio
import html
import io
import json
import logging
import os
import re
import socket
import sys
import time as _time
from datetime import datetime, timedelta
from typing import Dict, Optional

# Windows 콘솔 utf-8 설정
sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from coupang_api import CoupangPartnersAPI
from deal_crawler import DealCrawler, load_seen_deals, save_seen_deals

# 환경변수 로드
load_dotenv()

# 로깅 설정
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# 설정값
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
try:
    ADMIN_CHAT_ID = int(os.getenv("TELEGRAM_ADMIN_CHAT_ID", "0") or "0")
except ValueError:
    ADMIN_CHAT_ID = 0
PUBLIC_CHANNEL_ID = os.getenv("TELEGRAM_PUBLIC_CHANNEL_ID", "@HOTpang911")

# 리모콘 상태
START_TIME = datetime.now()
AUTO_ENABLED = True
SEARCH_WAIT: Dict[int, bool] = {}
AUTO_GOLDBOX_LIMIT = 5  # 자동 송출은 상위 N개만 (스팸 방지, 수동 전체송출은 제한 없음)
CACHE_TTL_SEC = 30 * 60  # 송출 캐시 유효시간 30분
_SINGLETON_SOCK = None


def acquire_single_instance(port: int = 48291):
    """중복 실행 방지: 이미 돌고 있으면 종료 (409 Conflict 원천 차단)"""
    global _SINGLETON_SOCK
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        print("❌ bot.py가 이미 실행 중입니다. 기존 창을 끄고 1개만 켜세요. (409 방지)")
        sys.exit(1)
    _SINGLETON_SOCK = s  # 프로세스 종료까지 소켓 유지

# 쿠팡 API 인스턴스
coupang_api = CoupangPartnersAPI()

# 인라인 버튼 캐시 (TTL 포함)
POST_CACHE: Dict[str, dict] = {}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def cache_set(key: str, item: dict):
    item["_exp"] = _time.time() + CACHE_TTL_SEC
    POST_CACHE[key] = item


def cache_get(key: str):
    item = POST_CACHE.get(key)
    if not item:
        return None
    if _time.time() > item.get("_exp", 0):
        POST_CACHE.pop(key, None)
        return None
    return item


def clean_cache():
    now = _time.time()
    for k in [k for k, v in POST_CACHE.items() if now > v.get("_exp", 0)]:
        POST_CACHE.pop(k, None)


def is_admin(user_id: int) -> bool:
    """관리자 권한 확인"""
    # ADMIN_CHAT_ID가 0이면 누구나 허용(초기 세팅용)
    if ADMIN_CHAT_ID == 0:
        return True
    return user_id == ADMIN_CHAT_ID


def is_admin_update(update: Update) -> bool:
    """DM/그룹 모두 대응: user id 기준"""
    u = update.effective_user
    if u and is_admin(u.id):
        return True
    c = update.effective_chat
    if c and is_admin(c.id):
        return True
    logger.warning(f"권한 없는 사용자 접근: user={u.id if u else 'N/A'}, chat={c.id if c else 'N/A'}")
    return False


def build_remote_keyboard() -> ReplyKeyboardMarkup:
    """관리자 DM 하단 고정 리모콘"""
    rows = [
        [KeyboardButton("🔥 검증핫딜 수집"), KeyboardButton("🚀 전체송출")],
        [KeyboardButton("🍯 골드박스"), KeyboardButton("📊 리포트")],
        [KeyboardButton("🔍 검색"), KeyboardButton("⏰ 자동 ON/OFF")],
        [KeyboardButton("📡 상태"), KeyboardButton("🏠 메뉴")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, persistent=True)


def build_main_menu() -> InlineKeyboardMarkup:
    """원클릭 터치형 메인 리모콘 패널"""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔥 실시간 검증 핫딜 지금 수집/송출", callback_data="menu:crawl")],
            [InlineKeyboardButton("🚀 오늘 골드박스 전품목 싹 다 송출 (28개)", callback_data="menu:post_all")],
            [
                InlineKeyboardButton("🍯 골드박스 5개 보기", callback_data="menu:goldbox"),
                InlineKeyboardButton("📊 실적 리포트", callback_data="menu:report"),
            ],
            [
                InlineKeyboardButton("🧪 채널 테스트 송출", callback_data="menu:test"),
                InlineKeyboardButton(
                    "⏰ 자동송출: 켜짐" if AUTO_ENABLED else "⏰ 자동송출: 꺼짐",
                    callback_data="menu:auto",
                ),
            ],
            [
                InlineKeyboardButton("🔍 상품 검색하기", callback_data="menu:search"),
                InlineKeyboardButton("📡 봇 상태 확인", callback_data="menu:status"),
            ],
        ]
    )


def status_text() -> str:
    up = datetime.now() - START_TIME
    h, rem = divmod(int(up.total_seconds()), 3600)
    m, _ = divmod(rem, 60)
    return (
        f"📡 <b>봇 상태</b>\n\n"
        f"• 가동시간: {h}시간 {m}분\n"
        f"• 자동송출: {'✅ ON' if AUTO_ENABLED else '⏸ OFF'} (매일 07:05 상위 {AUTO_GOLDBOX_LIMIT}개 + 60분마다 검증핫딜)\n"
        f"• 송출 채널: <code>{esc(PUBLIC_CHANNEL_ID)}</code>\n"
        f"• 관리자 ID: <code>{ADMIN_CHAT_ID}</code>\n\n"
        f"버튼이나 명령어(/menu)로 조작하세요."
    )


def format_deal_message(
    title: str,
    price: int,
    original_price: Optional[int] = None,
    badge: str = "🍯 [오늘의 골드박스 특가]",
    category: Optional[str] = None,
    rank: Optional[int] = None,
    is_rocket: bool = True,
    is_free_shipping: bool = False,
    custom_note: Optional[str] = None,
) -> str:
    """풍성한 정보를 담은 채널 송출용 메시지 템플릿"""
    header_parts = [badge]
    if rank:
        header_parts.append(f"🏆 랭킹 {rank}위")
    if category:
        header_parts.append(f"📂 {category}")
    header_str = " | ".join(header_parts)

    discount_text = ""
    discount_rate = 0
    if original_price and original_price > price:
        discount_rate = round((original_price - price) / original_price * 100)
        discount_text = f" 🔥 **{discount_rate}% 파격할인!** (~{original_price:,}원~)"

    # 배송 혜택
    shipping_badges = []
    if is_rocket:
        shipping_badges.append("🚀 **로켓배송** (내일 도착 보장)")
    if is_free_shipping:
        shipping_badges.append("📦 **무료배송**")
    if not shipping_badges:
        shipping_badges.append("🚚 **일반배송**")
    shipping_str = " + ".join(shipping_badges)

    # 커스텀 수량/메모가 있으면 추가
    t = esc(title)
    header_str = esc(header_str)
    shipping_str = shipping_str.replace("**", "")  # 이모지+한글만, 볼드는 아래 태그로
    note_line = f"📦 <b>구성/수량</b>: <code>{esc(custom_note)}</code>\n" if custom_note else ""

    # shipping 뱃지 HTML 변환
    ship_html = esc(shipping_str)
    disc_html = f" 🔥 <b>{discount_rate}% 파격할인!</b> (<s>{original_price:,}원</s>)" if (original_price and original_price > price) else ""

    msg = (
        f"{header_str}\n\n"
        f"📌 <b>{t}</b>\n\n"
        f"{note_line}"
        f"💰 <b>특가가격</b>: <b>{price:,}원</b>{disc_html}\n"
        f"🚚 <b>배송혜택</b>: {ship_html}\n"
        f"⏰ <b>한정특가</b>: 오늘 자정(24:00)까지 또는 한정수량 마감\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>정확한 옵션별 잔여수량과 추가 쿠폰은 아래 링크에서 확인하세요.</i>\n"
        f"※ 이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
    )
    return msg


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """리모콘 메뉴 표시 (공통)"""
    text = (
        f"🎛 <b>리모콘 메뉴</b>\n\n"
        f"📢 송출 채널: <code>{esc(PUBLIC_CHANNEL_ID)}</code>\n"
        f"⏰ 자동송출: {'✅ ON' if AUTO_ENABLED else '⏸ OFF'}\n\n"
        f"버튼을 누르거나 하단 키보드를 사용하세요.\n"
        f"쿠팡 링크를 그냥 붙여넣으면 딥링크 변환됩니다."
    )
    kb = build_remote_keyboard()
    # DM에 하단 고정 키보드 먼저 세팅
    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🎛 리모콘 키보드 활성화됨 (하단 버튼 사용)",
            reply_markup=kb,
        )
    except Exception:
        pass
    if update.callback_query:
        await update.callback_query.message.reply_text(
            text, reply_markup=build_main_menu(), parse_mode=ParseMode.HTML
        )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=build_main_menu(), parse_mode=ParseMode.HTML
        )


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start 핸들러"""
    user = update.effective_user
    if not is_admin_update(update):
        await update.effective_message.reply_text(
            f"안녕하세요 {user.first_name}님! 오늘의 핫 꿀템 알리미 봇입니다. 🍯"
        )
        return
    await show_menu(update, context)


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    await show_menu(update, context)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    target = update.callback_query.message if update.callback_query else update.effective_message
    await target.reply_text(status_text(), parse_mode=ParseMode.HTML)


async def auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/auto [on|off] — 자동송출 토글"""
    global AUTO_ENABLED
    if not is_admin_update(update):
        return
    if context.args:
        arg = context.args[0].lower()
        if arg in ("on", "켜기", "1"):
            AUTO_ENABLED = True
        elif arg in ("off", "끄기", "0"):
            AUTO_ENABLED = False
    else:
        AUTO_ENABLED = not AUTO_ENABLED
    target = update.callback_query.message if update.callback_query else update.effective_message
    await target.reply_text(
        f"⏰ 자동송출이 <b>{'✅ ON' if AUTO_ENABLED else '⏸ OFF'}</b> 되었습니다.",
        reply_markup=build_main_menu(),
        parse_mode=ParseMode.HTML,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        return
    await update.effective_message.reply_text(
        "🛠 <b>사용법</b>\n\n"
        "• /menu — 리모콘 메뉴\n"
        "• /goldbox — 골드박스 5개 조회\n"
        "• /search &lt;키워드&gt; — 상품 검색 (버튼 ‘🔍 검색’ 후 키워드 입력도 가능)\n"
        "• /report — 실적 요약\n"
        "• /post_test — 테스트 송출 1건\n"
        "• /status — 봇 상태\n"
        "• /auto [on|off] — 자동송출 토글\n\n"
        "💡 쿠팡 링크를 그냥 보내면 딥링크 변환 + 송출 버튼 생성",
        parse_mode=ParseMode.HTML,
    )


async def send_goldbox_list(target_message, context: ContextTypes.DEFAULT_TYPE, count: int = 5):
    """골드박스 목록 전송 공용 로직 (명령어/버튼 공용, HTML)"""
    clean_cache()
    res = coupang_api.get_goldbox_products(sub_id="tg_goldbox")
    items = res.get("data", [])
    if not items:
        await target_message.reply_text("❌ 조회된 골드박스 상품이 없습니다.")
        return
    await target_message.reply_text(
        f"🍯 <b>오늘의 골드박스 특가</b> (총 {len(items)}개 중 상위 {min(count, len(items))}개):\n"
        f"원하시는 상품의 [📢 채널에 송출] 버튼을 누르면 <code>{esc(PUBLIC_CHANNEL_ID)}</code> 채널로 게시됩니다.",
        parse_mode=ParseMode.HTML,
    )
    for i, item in enumerate(items[:count]):
        p_name = item.get("productName", "")
        p_price = int(item.get("productPrice", 0))
        p_url = item.get("productUrl", "")
        img_url = item.get("productImage", "")
        cache_key = f"gb_{i}_{int(_time.time())}"
        cache_set(cache_key, {
            "name": p_name,
            "price": p_price,
            "url": p_url,
            "img": img_url,
            "badge": "🍯 [오늘의 골드박스 특가]",
        })
        caption = f"[{i+1}] <b>{esc(p_name)}</b>\n💰 가격: <b>{p_price:,}원</b>"
        keyboard = [[InlineKeyboardButton("📢 채널에 송출하기", callback_data=f"publish:{cache_key}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        if img_url:
            try:
                await target_message.reply_photo(photo=img_url, caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
                continue
            except Exception as e:
                logger.warning(f"사진 전송 실패 대체: {e}")
        await target_message.reply_text(caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def goldbox_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/goldbox 명령어 핸들러"""
    if not is_admin_update(update):
        return
    msg = await update.effective_message.reply_text("🔄 골드박스 특가 상품을 불러오는 중...")
    try:
        await send_goldbox_list(update.effective_message, context, count=5)
        await msg.delete()
    except Exception as e:
        logger.error(f"골드박스 에러: {e}")
        await msg.edit_text(f"❌ 골드박스 조회 중 오류: {esc(e)}")


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/search <검색어> 핸들러"""
    if not is_admin_update(update):
        return

    if not context.args:
        await update.effective_message.reply_text("사용법: <code>/search 키워드</code> (예: <code>/search 생수</code>)", parse_mode=ParseMode.HTML)
        return

    keyword = " ".join(context.args)
    msg = await update.effective_message.reply_text(f"🔍 '{keyword}' 검색 중...")

    try:
        res = coupang_api.search_products(keyword=keyword, limit=5, sub_id="tg_search")
        data = res.get("data", {})
        items = data.get("productData", []) if isinstance(data, dict) else []

        if not items:
            await msg.edit_text(f"❌ '{keyword}'에 대한 검색 결과가 없습니다.")
            return

        await msg.edit_text(f"🔎 <b>'{esc(keyword)}' 검색 결과 상위 5개</b>:", parse_mode=ParseMode.HTML)

        for i, item in enumerate(items[:5]):
            p_name = item.get("productName", "")
            p_price = int(item.get("productPrice", 0))
            p_url = item.get("productUrl", "")
            img_url = item.get("productImage", "")

            cache_key = f"sr_{i}_{int(_time.time())}"
            cache_set(cache_key, {
                "name": p_name,
                "price": p_price,
                "url": p_url,
                "img": img_url,
                "badge": f"🔥 [추천 특가: {keyword}]",
            })

            caption = f"[{i+1}] <b>{esc(p_name)}</b>\n💰 가격: <b>{p_price:,}원</b>"

            keyboard = [
                [
                    InlineKeyboardButton("👉 쿠팡에서 보기", url=p_url),
                    InlineKeyboardButton("📢 채널에 송출하기", callback_data=f"publish:{cache_key}"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            if img_url:
                try:
                    await update.effective_message.reply_photo(
                        photo=img_url,
                        caption=caption,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.HTML,
                    )
                    continue
                except Exception as e:
                    logger.warning(f"사진 전송 실패 대체: {e}")

            await update.effective_message.reply_text(
                caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML
            )

    except Exception as e:
        logger.error(f"검색 오류: {e}")
        await msg.edit_text(f"❌ 검색 중 오류 발생: {esc(e)}")


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/report 명령어: 최근 실적 조회"""
    if not is_admin_update(update):
        return

    msg = await update.effective_message.reply_text("📊 최근 파트너스 실적 조회 중...")

    try:
        today = datetime.now()
        yesterday = today - timedelta(days=1)
        start_date = yesterday.strftime("%Y%m%d")
        end_date = today.strftime("%Y%m%d")

        res = coupang_api.get_order_report(start_date=start_date, end_date=end_date)
        items = res.get("data", [])

        if not items:
            await msg.edit_text(
                f"📊 <b>[최근 실적 리포트 ({start_date} ~ {end_date})]</b>\n\n"
                f"기록된 주문 내역이 아직 없습니다.\n"
                f"꾸준히 특가를 송출해 보세요! 💪",
                parse_mode=ParseMode.HTML,
            )
            return

        total_orders = len(items)
        report_text = (
            f"📊 <b>[최근 실적 리포트 ({start_date} ~ {end_date})]</b>\n\n"
            f"• 총 주문 건수: <b>{total_orders}건</b>\n\n"
            f"<b>[최근 주문 품목]</b>\n"
        )
        for i, item in enumerate(items[:5]):
            p_name = item.get("productName", "상품")
            gmv = item.get("gmv", 0)  # 결제금액
            try:
                gmv_n = int(gmv)
            except Exception:
                gmv_n = 0
            report_text += f"{i+1}. {esc(p_name)} ({gmv_n:,}원)\n"

        await msg.edit_text(report_text, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"리포트 에러: {e}")
        await msg.edit_text(f"❌ 실적 조회 중 오류: {esc(e)}")


async def post_test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/post_test 명령어: 송출용 채널로 테스트 메시지 1건 발송"""
    if not is_admin_update(update):
        return

    status = await update.effective_message.reply_text(f"📡 <code>{esc(PUBLIC_CHANNEL_ID)}</code> 채널로 테스트 발송 중...", parse_mode=ParseMode.HTML)

    try:
        res = coupang_api.get_goldbox_products(sub_id="tg_test")
        items = res.get("data", [])
        if not items:
            await status.edit_text("❌ 골드박스 상품이 없습니다.")
            return

        item = items[0]
        p_name = item.get("productName")
        p_price = int(item.get("productPrice", 0))
        p_url = item.get("productUrl")
        img_url = item.get("productImage")

        deal_msg = format_deal_message(
            title=p_name,
            price=p_price,
            badge="🍯 [오늘의 핫 꿀템 특가 테스트]",
        )

        keyboard = [[InlineKeyboardButton("👉 최저가 특가 확인하기", url=p_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if img_url:
            await context.bot.send_photo(
                chat_id=PUBLIC_CHANNEL_ID,
                photo=img_url,
                caption=deal_msg,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(
                chat_id=PUBLIC_CHANNEL_ID,
                text=deal_msg,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )

        await status.edit_text(f"🎉 <code>{esc(PUBLIC_CHANNEL_ID)}</code> 채널로 특가 메시지가 성공적으로 송출되었습니다!", parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"테스트 송출 실패: {e}")
        await status.edit_text(f"❌ 채널 송출 실패: {esc(e)}")


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """하단 리모콘 버튼 + 검색 대기 + 쿠팡 URL 처리 (통합)"""
    text = (update.effective_message.text or "").strip()
    uid = update.effective_user.id if update.effective_user else 0
    logger.info(f"📩 수신 텍스트: '{text}' (from user: {uid})")

    if not is_admin_update(update):
        await update.effective_message.reply_text(
            f"안녕하세요! 오늘의 핫 꿀템 알리미입니다. 🍯\n(사용자 ID: <code>{uid}</code>)",
            parse_mode=ParseMode.HTML,
        )
        return

    # 1) 리모콘 버튼 텍스트
    if text in ("🔥 검증핫딜 수집", "검증핫딜", "핫딜"):
        await crawl_now_cmd(update, context)
        return
    if text in ("🍯 골드박스", "골드박스"):
        await goldbox_cmd(update, context)
        return
    if text in ("🚀 전체송출", "전체송출", "전체"):
        await post_all_cmd(update, context)
        return
    if text in ("🔍 검색", "검색"):
        SEARCH_WAIT[uid] = True
        await update.effective_message.reply_text(
            "🔍 검색할 키워드를 입력하세요. (예: 생수, 기저귀)\n취소하려면 /menu"
        )
        return
    if text in ("📊 리포트", "리포트"):
        await report_cmd(update, context)
        return
    if text in ("🧪 테스트송출", "테스트송출", "테스트"):
        await post_test_cmd(update, context)
        return
    if text in ("📡 상태", "상태"):
        await update.effective_message.reply_text(status_text(), parse_mode=ParseMode.HTML)
        return
    if text in ("⏰ 자동 ON/OFF", "자동"):
        await auto_cmd(update, context)
        return
    if text in ("🏠 메뉴", "메뉴"):
        await show_menu(update, context)
        return

    # 2) 검색 대기 중이면 키워드로 처리
    if SEARCH_WAIT.get(uid):
        SEARCH_WAIT.pop(uid, None)
        context.args = text.split()
        await search_cmd(update, context)
        return

    # 3) 쿠팡 URL → 딥링크 변환
    urls = re.findall(r"https?://[^\s]+coupang\.com/[^\s]+", text)
    if not urls:
        # URL이 아닌 일반 대화일 경우 메뉴 및 안내 표시
        await update.effective_message.reply_text(
            f"💡 <b>쿠팡 링크를 보내주시면 파트너스 딥링크로 즉시 변환해 드립니다.</b>\n\n"
            f"아래 메뉴 버튼을 누르시거나 명령어를 입력하세요:\n"
            f"• 🍯 <b>골드박스</b>: 상위 특가 5개 조회\n"
            f"• 🚀 <b>전체송출</b>: 골드박스 전 품목 채널로 싹 다 송출\n"
            f"• 🔍 <b>검색</b>: 키워드 상품 검색",
            reply_markup=build_main_menu(),
            parse_mode=ParseMode.HTML,
        )
        return

    target_url = urls[0]
    custom_note = text.replace(target_url, "").strip() or None
    status_msg = await update.effective_message.reply_text("🔗 쿠팡 파트너스 딥링크 생성 중...")

    try:
        res = coupang_api.create_deeplink([target_url], sub_id="tg_manual")
        data = res.get("data", [])
        if not data:
            await status_msg.edit_text("❌ 딥링크 생성 실패 (응답 데이터 없음)")
            return

        deeplink = data[0].get("shortenUrl") or data[0].get("landingUrl")

        cache_key = f"manual_{int(_time.time())}"
        cache_set(cache_key, {
            "name": "🔥 실시간 엄선 핫딜 꿀템",
            "price": 0,
            "url": deeplink,
            "img": None,
            "badge": "🍯 [실시간 엄선 핫딜]",
            "custom_note": custom_note,
        })

        note_str = f"\n📦 <b>입력된 수량/메모</b>: <code>{esc(custom_note)}</code>" if custom_note else ""
        preview_text = (
            f"✅ <b>파트너스 딥링크 변환 완료!</b>\n\n"
            f"🔗 <b>단축 딥링크</b>: {esc(deeplink)}{note_str}\n\n"
            f"아래 [📢 채널에 송출하기] 버튼을 누르면 <code>{esc(PUBLIC_CHANNEL_ID)}</code> 채널에 즉시 게시됩니다."
        )

        keyboard = [
            [
                InlineKeyboardButton("👉 링크 열기", url=deeplink),
                InlineKeyboardButton("📢 채널에 송출하기", callback_data=f"publish:{cache_key}"),
            ]
        ]
        await status_msg.edit_text(
            preview_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        logger.error(f"딥링크 생성 에러: {e}")
        await status_msg.edit_text(f"❌ 딥링크 생성 중 오류 발생: {esc(e)}")


async def publish_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[📢 채널에 송출하기] 버튼 콜백 핸들러"""
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data.startswith("publish:"):
        return

    clean_cache()
    cache_key = data.split("publish:", 1)[1]
    item = cache_get(cache_key)
    if not item:
        await query.message.reply_text("⚠️ 만료되었거나 찾을 수 없는 상품 데이터입니다. (30분 유효, 다시 조회해주세요)")
        return

    p_name = item["name"]
    p_price = item["price"]
    p_url = item["url"]
    p_img = item.get("img")
    badge = item.get("badge", "🍯 [오늘의 핫 꿀템 특가]")

    deal_msg = format_deal_message(
        title=p_name,
        price=p_price,
        badge=badge,
        category=item.get("category"),
        rank=item.get("rank"),
        is_rocket=item.get("is_rocket", True),
        is_free_shipping=item.get("is_free_shipping", False),
        custom_note=item.get("custom_note"),
    )
    keyboard = [[InlineKeyboardButton("👉 수량/옵션 및 최저가 확인하기", url=p_url)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if p_img:
            await context.bot.send_photo(
                chat_id=PUBLIC_CHANNEL_ID,
                photo=p_img,
                caption=deal_msg,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(
                chat_id=PUBLIC_CHANNEL_ID,
                text=deal_msg,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
            )

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"🎉 <b>송출 완료!</b> 채널(<code>{esc(PUBLIC_CHANNEL_ID)}</code>)에 게시되었습니다.", parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"채널 전송 실패: {e}")
        await query.message.reply_text(f"❌ 채널 송출 실패: {esc(e)}")


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """리모콘 인라인 메뉴 (menu:*)"""
    query = update.callback_query
    await query.answer()
    if not is_admin_update(update):
        return
    action = (query.data or "").split("menu:", 1)[-1]
    uid = update.effective_user.id if update.effective_user else 0
    if action == "goldbox":
        await query.message.reply_text("🔄 골드박스 불러오는 중...")
        try:
            await send_goldbox_list(query.message, context, count=5)
        except Exception as e:
            await query.message.reply_text(f"❌ 골드박스 조회 중 오류: {esc(e)}")
    elif action == "search":
        SEARCH_WAIT[uid] = True
        await query.message.reply_text("🔍 검색할 키워드를 입력하세요. (예: 생수)")
    elif action == "report":
        await report_cmd(update, context)
    elif action == "test":
        await post_test_cmd(update, context)
    elif action == "status":
        await query.message.reply_text(status_text(), parse_mode=ParseMode.HTML)
    elif action == "auto":
        await auto_cmd(update, context)
    elif action == "post_all":
        await post_all_cmd(update, context)
    elif action == "crawl":
        await crawl_now_cmd(update, context)
    elif action == "help":
        await query.message.reply_text(
            "🛠 /menu /goldbox /search 키워드 /report /post_test /status /auto",
            parse_mode=ParseMode.HTML,
        )


async def post_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/post_all 명령어: 현재 골드박스 전 품목을 채널로 싹 다 송출"""
    if not is_admin_update(update):
        return

    status = await update.effective_message.reply_text("🚀 골드박스 전 품목 송출을 시작합니다! 잠시만 기다려주세요...")

    try:
        res = coupang_api.get_goldbox_products(sub_id="tg_all_manual")
        items = res.get("data", [])
        if not items:
            await status.edit_text("❌ 조회된 골드박스 상품이 없습니다.")
            return

        total = len(items)
        success_count = 0

        for i, item in enumerate(items):
            p_name = item.get("productName")
            p_price = int(item.get("productPrice", 0))
            p_url = item.get("productUrl")
            img_url = item.get("productImage")

            deal_msg = format_deal_message(
                title=p_name,
                price=p_price,
                badge="🍯 [오늘의 골드박스 핫특가]",
                category=item.get("categoryName"),
                rank=item.get("rank", i + 1),
                is_rocket=item.get("isRocket", True),
                is_free_shipping=item.get("isFreeShipping", False),
            )
            keyboard = [[InlineKeyboardButton("👉 수량/옵션 및 최저가 확인하기", url=p_url)]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                if img_url:
                    await context.bot.send_photo(
                        chat_id=PUBLIC_CHANNEL_ID,
                        photo=img_url,
                        caption=deal_msg,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await context.bot.send_message(
                        chat_id=PUBLIC_CHANNEL_ID,
                        text=deal_msg,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.HTML,
                    )
                success_count += 1
            except Exception as e:
                logger.error(f"개별 상품 송출 오류 ({p_name}): {e}")

            await asyncio.sleep(1.5)

        await status.edit_text(
            f"🎉 <b>골드박스 전체 송출 완료!</b>\n"
            f"총 {total}개 중 <b>{success_count}개</b> 상품이 <code>{esc(PUBLIC_CHANNEL_ID)}</code> 채널에 등록되었습니다.",
            reply_markup=build_main_menu(),
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        logger.error(f"전체 송출 에러: {e}")
        await status.edit_text(f"❌ 전체 송출 중 오류 발생: {esc(e)}")


async def scheduled_morning_goldbox(bot):
    """아침 자동 송출 — 스팸 방지 위해 상위 N개만 (수동 전체송출은 제한 없음)"""
    clean_cache()
    if not AUTO_ENABLED:
        logger.info("자동송출 OFF — 스케줄 스킵")
        return
    logger.info(f"아침 골드박스 자동 송출 시작 (상위 {AUTO_GOLDBOX_LIMIT}개)")
    try:
        res = coupang_api.get_goldbox_products(sub_id="tg_auto_all")
        items = res.get("data", [])
        if not items:
            return

        success_count = 0
        for i, item in enumerate(items[:AUTO_GOLDBOX_LIMIT]):
            p_name = item.get("productName")
            p_price = int(item.get("productPrice", 0))
            p_url = item.get("productUrl")
            img_url = item.get("productImage")

            deal_msg = format_deal_message(
                title=p_name,
                price=p_price,
                badge="🍯 [오늘의 골드박스 핫특가]",
                category=item.get("categoryName"),
                rank=item.get("rank", i + 1),
                is_rocket=item.get("isRocket", True),
                is_free_shipping=item.get("isFreeShipping", False),
            )
            keyboard = [[InlineKeyboardButton("👉 수량/옵션 및 최저가 확인하기", url=p_url)]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            try:
                if img_url:
                    await bot.send_photo(
                        chat_id=PUBLIC_CHANNEL_ID,
                        photo=img_url,
                        caption=deal_msg,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await bot.send_message(
                        chat_id=PUBLIC_CHANNEL_ID,
                        text=deal_msg,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.HTML,
                    )
                success_count += 1
            except Exception as e:
                logger.error(f"자동 송출 오류: {e}")

            await asyncio.sleep(1.5)

        if ADMIN_CHAT_ID:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"📢 <b>[골드박스 자동 송출 완료]</b> 오늘자 상위 <b>{success_count}개</b>가 <code>{esc(PUBLIC_CHANNEL_ID)}</code> 채널에 등록되었습니다.",
                parse_mode=ParseMode.HTML,
            )

    except Exception as e:
        logger.error(f"아침 자동 송출 오류: {e}")
        if ADMIN_CHAT_ID:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"⚠️ 아침 골드박스 자동 송출 중 오류 발생: {esc(e)}",
            )


async def check_and_publish_community_deals(bot) -> int:
    """커뮤니티 검증 핫딜 실시간 자동 수집 및 송출"""
    if not AUTO_ENABLED:
        return 0
    logger.info("📡 커뮤니티 검증 핫딜 스캔 시작...")
    seen = load_seen_deals()
    deals = DealCrawler.fetch_ruliweb_deals()
    published_count = 0

    for d in deals:
        deal_id = d["id"]
        if deal_id in seen:
            continue

        detail = DealCrawler.parse_deal_detail(d["link"])
        if not detail or not detail.get("coupang_url"):
            seen.add(deal_id)
            save_seen_deals(seen)
            continue

        coupang_raw_url = detail["coupang_url"]
        reason = detail.get("reason", "커뮤니티 추천 초특가 핫딜")
        img_url = detail.get("image_url")
        title = d["title"].replace("[쿠팡]", "").strip()

        # 쿠팡 파트너스 딥링크 생성
        try:
            res = coupang_api.create_deeplink([coupang_raw_url], sub_id="tg_community_auto")
            items = res.get("data", [])
            if not items:
                seen.add(deal_id)
                save_seen_deals(seen)
                continue
            deeplink = items[0].get("shortenUrl") or items[0].get("landingUrl")
        except Exception as e:
            logger.error(f"딥링크 변환 실패: {e}")
            continue

        deal_msg = (
            "🔥 <b>[실시간 커뮤니티 검증 핫딜]</b>\n\n"
            f"📌 <b>{esc(title)}</b>\n\n"
            f"💡 <b>특가 핵심 포인트</b>:\n"
            f"<code>{esc(reason)}</code>\n\n"
            "🚚 <b>배송혜택</b>: 🚀 로켓배송 / 한정수량\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <i>핫딜 특성상 가격 변동 및 조기 품절될 수 있습니다.</i>\n"
            "※ 이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
        )

        keyboard = [[InlineKeyboardButton("👉 특가 혜택 및 수량 확인하기", url=deeplink)]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            if img_url:
                await bot.send_photo(
                    chat_id=PUBLIC_CHANNEL_ID,
                    photo=img_url,
                    caption=deal_msg,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML,
                )
            else:
                await bot.send_message(
                    chat_id=PUBLIC_CHANNEL_ID,
                    text=deal_msg,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML,
                )

            seen.add(deal_id)
            save_seen_deals(seen)
            published_count += 1
            logger.info(f"🎉 커뮤니티 핫딜 송출 성공: {title}")

            if ADMIN_CHAT_ID:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=f"📢 <b>[검증 핫딜 자동 송출]</b>\n{esc(title)}\n<code>{esc(reason)}</code>",
                    parse_mode=ParseMode.HTML,
                )

            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"채널 전송 실패: {e}")

    return published_count


async def crawl_now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """수동 핫딜 즉시 수집 커맨드"""
    if not is_admin_update(update):
        return

    status = await update.effective_message.reply_text("🔄 실시간 커뮤니티 핫딜 스캔 중...")
    count = await check_and_publish_community_deals(context.bot)
    if count > 0:
        await status.edit_text(f"🎉 새로운 검증 핫딜 <b>{count}건</b>을 채널에 송출했습니다!", reply_markup=build_main_menu(), parse_mode=ParseMode.HTML)
    else:
        await status.edit_text("✅ 최신 핫딜이 이미 모두 송출되었거나 새 쿠팡 딜이 없습니다.", reply_markup=build_main_menu())


async def post_init(application: Application):
    """비동기 이벤트 루프 시작 후 스케줄러 가동"""
    clean_cache()
    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
    # 1) 매일 07:05 골드박스 상위 N개 자동 송출 (스팸 방지)
    scheduler.add_job(
        scheduled_morning_goldbox,
        trigger=CronTrigger(hour=7, minute=5),
        args=[application.bot],
    )
    # 2) 60분마다 커뮤니티 검증 핫딜 자동 수집 및 송출 (10분→60분, 채널 도배 방지)
    scheduler.add_job(
        check_and_publish_community_deals,
        trigger=IntervalTrigger(minutes=60),
        args=[application.bot],
    )
    # 3) 30분마다 만료 캐시 청소
    scheduler.add_job(clean_cache, trigger=IntervalTrigger(minutes=30))
    scheduler.start()
    logger.info("⏰ 스케줄러 등록 완료: (1) 07:05 골드박스 상위 %d개, (2) 60분 주기 검증핫딜, (3) 30분 캐시청소", AUTO_GOLDBOX_LIMIT)


def main():
    acquire_single_instance()
    if not BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN이 설정되지 않았습니다.")
        return

    print("🤖 오늘의 핫 꿀템 봇 시작 중...")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # 핸들러 등록
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("auto", auto_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("goldbox", goldbox_cmd))
    app.add_handler(CommandHandler("post_all", post_all_cmd))
    app.add_handler(CommandHandler("crawl_now", crawl_now_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("post_test", post_test_cmd))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu:"))
    app.add_handler(CallbackQueryHandler(publish_callback, pattern="^publish:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    print(f"🚀 봇 가동 시작! 송출 채널: {PUBLIC_CHANNEL_ID}, 관리자 ID: {ADMIN_CHAT_ID}")
    app.run_polling()


if __name__ == "__main__":
    main()
