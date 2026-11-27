"""Cloudflare Workers 기반 실시간 클릭 카운터 클라이언트."""

import argparse
import html
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SOURCE_LABEL_MAP = {
    "arca": "아카라이브",
    "ppomppu": "뽐뿌",
    "ruliweb": "루리웹",
    "dealbada": "딜바다",
    "goldbox": "골드박스",
}


def _get_tracker_config() -> tuple[str, str]:
    base_url = str(os.getenv("CLICK_TRACKER_BASE_URL", "") or "").strip().rstrip("/")
    api_key = str(os.getenv("CLICK_TRACKER_API_KEY", "") or "").strip()
    return base_url, api_key


def is_click_tracker_enabled() -> bool:
    """실시간 클릭 트래커 URL과 API 키가 모두 설정되어 있으면 True 반환"""
    base_url, api_key = _get_tracker_config()
    return bool(base_url and api_key)


def register_tracked_link(
    target_url: str,
    *,
    deal_id: str = "",
    product_id: str = "",
    source: str = "",
    track: str = "A",
    title: str = "",
    sale_price: int = 0,
    discount_rate: int = 0,
    timeout: float = 4.0,
) -> str:
    """쿠팡 파트너스 딥링크를 클릭 트래킹 단축 URL로 등록하고 실패 시 원본 URL 반환(fail-open)."""
    raw_target = str(target_url or "").strip()
    if not raw_target or not is_click_tracker_enabled():
        return raw_target

    base_url, api_key = _get_tracker_config()
    payload = {
        "target_url": raw_target,
        "deal_id": str(deal_id or ""),
        "product_id": str(product_id or ""),
        "source": str(source or ""),
        "track": str(track or "A"),
        "title": str(title or ""),
        "sale_price": int(sale_price or 0),
        "discount_rate": int(discount_rate or 0),
    }

    try:
        res = requests.post(
            f"{base_url}/api/links",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        if res.status_code != 200:
            logger.warning(f"클릭 트래커 링크 등록 HTTP {res.status_code} — 원본 딥링크 사용")
            return raw_target
        data = res.json()
        short_url = str((data or {}).get("short_url") or "").strip()
        if short_url.startswith(("https://", "http://")):
            return short_url
        logger.warning("클릭 트래커 응답에 유효한 short_url 없음 — 원본 딥링크 사용")
    except Exception as e:
        logger.warning(f"클릭 트래커 등록 실패 (원본 딥링크 유지): {e}")

    return raw_target


def fetch_click_stats(
    days: int = 7,
    limit: int = 10,
    timeout: float = 6.0,
) -> Optional[Dict[str, Any]]:
    """Cloudflare Workers /api/stats 엔드포인트에서 실시간 클릭 집계 조회"""
    if not is_click_tracker_enabled():
        return None

    base_url, api_key = _get_tracker_config()
    try:
        res = requests.get(
            f"{base_url}/api/stats",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"days": int(days), "limit": int(limit)},
            timeout=timeout,
        )
        if res.status_code != 200:
            logger.warning(f"클릭 통계 조회 HTTP {res.status_code}")
            return None
        data = res.json()
        if isinstance(data, dict) and data.get("ok"):
            return data
    except Exception as e:
        logger.warning(f"클릭 통계 조회 실패: {e}")

    return None


def _source_kr(code: str) -> str:
    key = str(code or "").strip().lower()
    return SOURCE_LABEL_MAP.get(key, key or "기타")


def format_click_stats_telegram(stats: Dict[str, Any], top_n: int = 5) -> str:
    """실시간 클릭 통계를 텔레그램 HTML 요약 블록으로 포맷"""
    today = stats.get("today") or {}
    period = stats.get("period_total") or {}
    days = int(stats.get("days", 7) or 7)
    today_clicks = int(today.get("clicks", 0) or 0)
    today_unique = int(today.get("unique_links", 0) or 0)
    period_clicks = int(period.get("clicks", 0) or 0)
    total_links = int(period.get("total_links", 0) or 0)

    lines: List[str] = [
        "⚡ <b>실시간 클릭 현황</b>",
        f"• 오늘 클릭: <b>{today_clicks:,}회</b> (상품 {today_unique:,}개)",
        f"• 최근 {days}일 누적: <b>{period_clicks:,}회</b> (등록 링크 {total_links:,}개)",
    ]

    src_rows = stats.get("by_source_today") or stats.get("by_source_period") or []
    if src_rows:
        src_parts = [
            f"{html.escape(_source_kr(str(r.get('source') or '')), quote=False)} {int(r.get('clicks', 0) or 0):,}회"
            for r in src_rows[:5]
            if int(r.get("clicks", 0) or 0) > 0
        ]
        if src_parts:
            tag = "오늘 출처별" if stats.get("by_source_today") else f"{days}일 출처별"
            lines.append(f"• {tag}: {' · '.join(src_parts)}")

    top_rows = (stats.get("top_today") or stats.get("top_period") or [])[:max(1, top_n)]
    if top_rows:
        top_label = "오늘 인기 클릭" if stats.get("top_today") else f"최근 {days}일 인기 클릭"
        lines.append(f"• <b>{top_label} TOP</b>")
        for idx, item in enumerate(top_rows, 1):
            raw_title = str(item.get("title") or item.get("code") or "상품").strip()
            if len(raw_title) > 26:
                raw_title = raw_title[:25].rstrip() + "…"
            clicks_n = int(item.get("clicks", 0) or 0)
            src_name = _source_kr(str(item.get("source") or ""))
            sale_p = int(item.get("sale_price", 0) or 0)
            price_part = f" · {sale_p:,}원" if sale_p > 0 else ""
            lines.append(
                f"  {idx}. {html.escape(raw_title, quote=False)} ({html.escape(src_name, quote=False)}{price_part}) — <b>{clicks_n:,}회</b>"
            )

    return "\n".join(lines)


def normalize_alert_keyword(keyword: str) -> str:
    """알림 키워드 정규화(공백 제거 + 소문자)."""
    return re.sub(r"\s+", "", str(keyword or "").strip()).lower()


def list_keyword_alerts(
    chat_id: Optional[Any] = None,
    timeout: float = 5.0,
) -> Optional[List[Dict[str, Any]]]:
    """등록된 맞춤 키워드 알림 목록 조회 (chat_id 생략 시 전체 구독자 목록)."""
    if not is_click_tracker_enabled():
        return None
    base_url, api_key = _get_tracker_config()
    params = {"chat_id": str(chat_id)} if chat_id is not None and str(chat_id).strip() else None
    try:
        res = requests.get(
            f"{base_url}/api/alerts",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
            timeout=timeout,
        )
        if res.status_code != 200:
            logger.warning(f"키워드 알림 조회 HTTP {res.status_code}")
            return None
        data = res.json()
        if isinstance(data, dict) and data.get("ok") and isinstance(data.get("alerts"), list):
            return data["alerts"]
    except Exception as e:
        logger.warning(f"키워드 알림 조회 실패: {e}")
    return None


def add_keyword_alert(
    chat_id: Any,
    keyword: str,
    timeout: float = 5.0,
) -> Tuple[bool, str]:
    """구독자 맞춤 키워드 등록 후 (성공 여부, 안내 문구) 반환."""
    kw = " ".join(str(keyword or "").strip().split())
    kw_norm = normalize_alert_keyword(kw)
    if len(kw) < 2 or len(kw) > 24 or len(kw_norm) < 2:
        return False, "키워드는 2~24자로 입력해 주세요."
    if not is_click_tracker_enabled():
        return False, "알림 서버가 아직 설정되지 않았습니다."

    base_url, api_key = _get_tracker_config()
    try:
        res = requests.post(
            f"{base_url}/api/alerts",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"chat_id": str(chat_id), "keyword": kw},
            timeout=timeout,
        )
        data = res.json() if res.content else {}
        if res.status_code == 200 and data.get("ok"):
            saved_kw = str(data.get("keyword") or kw)
            if data.get("already_exists"):
                return True, f"이미 등록된 키워드예요: '{saved_kw}'"
            return True, f"알림 등록 완료: '{saved_kw}' ({int(data.get('count', 1) or 1)}/10)"
        err_code = str(data.get("error") or "")
        if err_code == "limit_exceeded":
            return False, "키워드는 최대 10개까지 등록할 수 있어요. 기존 키워드를 지운 뒤 다시 등록해 주세요."
        if err_code == "invalid_keyword":
            return False, "키워드는 2~24자로 입력해 주세요."
        logger.warning(f"키워드 알림 등록 HTTP {res.status_code}: {err_code}")
    except Exception as e:
        logger.warning(f"키워드 알림 등록 실패: {e}")
    return False, "일시적인 오류로 등록하지 못했습니다. 잠시 후 다시 시도해 주세요."


def remove_keyword_alert(
    chat_id: Any,
    keyword: str = "",
    remove_all: bool = False,
    timeout: float = 5.0,
) -> bool:
    """구독자 맞춤 키워드 단건 또는 전체 삭제."""
    if not is_click_tracker_enabled():
        return False
    base_url, api_key = _get_tracker_config()
    params: Dict[str, str] = {"chat_id": str(chat_id)}
    if remove_all:
        params["all"] = "1"
    else:
        kw_norm = normalize_alert_keyword(keyword)
        if not kw_norm:
            return False
        params["keyword"] = kw_norm
    try:
        res = requests.delete(
            f"{base_url}/api/alerts",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
            timeout=timeout,
        )
        if res.status_code == 200:
            data = res.json()
            return bool(isinstance(data, dict) and data.get("ok"))
        logger.warning(f"키워드 알림 삭제 HTTP {res.status_code}")
    except Exception as e:
        logger.warning(f"키워드 알림 삭제 실패: {e}")
    return False


def match_alerts_for_title(
    title: str,
    alerts: Optional[List[Dict[str, Any]]],
) -> Dict[str, List[str]]:
    """상품 제목과 매칭되는 구독자별 키워드 목록 {chat_id: [keyword, ...]} 반환."""
    title_norm = normalize_alert_keyword(title)
    if not title_norm or not alerts:
        return {}
    matched: Dict[str, List[str]] = {}
    for row in alerts:
        cid = str(row.get("chat_id") or "").strip()
        kw = str(row.get("keyword") or "").strip()
        kw_norm = str(row.get("keyword_norm") or normalize_alert_keyword(kw)).strip()
        if not cid or not kw_norm or len(kw_norm) < 2:
            continue
        if kw_norm in title_norm:
            bucket = matched.setdefault(cid, [])
            if kw not in bucket:
                bucket.append(kw)
    return matched


def main() -> int:
    parser = argparse.ArgumentParser(description="실시간 클릭 통계 조회 CLI")
    parser.add_argument("--days", type=int, default=7, help="조회 일수 (1~30)")
    parser.add_argument("--limit", type=int, default=10, help="TOP 상품 수 (1~30)")
    parser.add_argument("--json", action="store_true", help="원본 JSON 출력")
    args = parser.parse_args()

    if not is_click_tracker_enabled():
        print("CLICK_TRACKER_BASE_URL 또는 CLICK_TRACKER_API_KEY가 설정되지 않았습니다.")
        return 1

    stats = fetch_click_stats(days=args.days, limit=args.limit)
    if not stats:
        print("클릭 통계를 조회하지 못했습니다.")
        return 1

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        today = stats.get("today") or {}
        period = stats.get("period_total") or {}
        print(f"=== 실시간 클릭 통계 ({stats.get('today_kst', '')}, 최근 {stats.get('days', args.days)}일) ===")
        print(f"오늘 클릭: {today.get('clicks', 0)}회 (상품 {today.get('unique_links', 0)}개)")
        print(f"기간 누적: {period.get('clicks', 0)}회 (등록 링크 {period.get('total_links', 0)}개)")
        for idx, it in enumerate(stats.get("top_today") or stats.get("top_period") or [], 1):
            print(f"  {idx}. [{_source_kr(str(it.get('source') or ''))}] {it.get('title') or it.get('code')} -> {it.get('clicks', 0)}회")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
