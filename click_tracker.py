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
    "clien": "클리앙",
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


_SYMMETRIC_ALIAS_GROUPS = (
    ("에어팟", "airpods", "airpod"),
    ("아이폰", "iphone"),
    ("아이패드", "ipad"),
    ("맥북", "macbook"),
    ("애플워치", "applewatch"),
    ("갤럭시", "galaxy"),
    ("버즈", "buds"),
    ("소니", "sony"),
    ("다이슨", "dyson"),
    ("로지텍", "logitech"),
    ("닌텐도", "nintendo"),
    ("플스", "플레이스테이션", "ps5", "playstation"),
    ("코카콜라", "cocacola"),
    ("펩시", "pepsi"),
    ("스타벅스", "starbucks"),
    ("네스프레소", "nespresso"),
    ("나이키", "nike"),
    ("아디다스", "adidas"),
    ("뉴발란스", "newbalance"),
    ("필립스", "philips"),
    ("브라운", "braun"),
    ("레고", "lego"),
    ("샤오미", "xiaomi"),
    ("앤커", "anker"),
)

_ONE_WAY_CATEGORY_EXPANSIONS = {
    "생수": ("삼다수", "백산수", "아이시스", "탐사수", "몽베스트", "스파클", "평창수", "에비앙", "동원샘물"),
    "즉석밥": ("햇반", "오뚜기밥", "비비고밥"),
    "물티슈": ("베베숲", "브라운물티슈", "페넬로페", "순둥이", "도담이", "탐사물티슈"),
    "기저귀": ("하기스", "팸퍼스", "마미포코", "보솜이"),
    "화장지": ("휴지", "롤휴지", "두루마리", "크리넥스", "깨끗한나라", "잘풀리는집", "코디"),
    "휴지": ("화장지", "롤휴지", "두루마리", "크리넥스", "깨끗한나라", "잘풀리는집", "코디"),
    "제로콜라": ("코카콜라제로", "펩시제로"),
    "프로틴": ("단백질", "셀렉스", "마이프로틴", "신타6", "옵티멈"),
    "단백질": ("프로틴", "셀렉스", "마이프로틴", "신타6", "옵티멈"),
}


def _build_token_variant_map() -> Dict[str, Tuple[str, ...]]:
    out: Dict[str, List[str]] = {}
    for group in _SYMMETRIC_ALIAS_GROUPS:
        norms = [normalize_alert_keyword(g) for g in group if normalize_alert_keyword(g)]
        for k in norms:
            bucket = out.setdefault(k, [k])
            for alt in norms:
                if alt not in bucket:
                    bucket.append(alt)
    for key, extras in _ONE_WAY_CATEGORY_EXPANSIONS.items():
        k_norm = normalize_alert_keyword(key)
        bucket = out.setdefault(k_norm, [k_norm])
        for ex in extras:
            ex_norm = normalize_alert_keyword(ex)
            if ex_norm and ex_norm not in bucket:
                bucket.append(ex_norm)
    return {k: tuple(v) for k, v in out.items()}


_TOKEN_VARIANT_MAP = _build_token_variant_map()

_DISCOUNT_FILTER_RE = re.compile(
    r"(?:(?<=^)|(?<=\s))(?:>=\s*(\d{1,2})\s*%|(\d{1,2})\s*%\s*(?:이상|↑|\+|할인|초과))(?=\s|$)",
    re.IGNORECASE,
)
_MAN_PRICE_FILTER_RE = re.compile(
    r"(?:(?<=^)|(?<=\s))(?:"
    r"(?:~|<=|최대)\s*(\d+(?:\.\d+)?)\s*만\s*원?"
    r"|(\d+(?:\.\d+)?)\s*만\s*원?\s*(?:이하|미만|까지|↓)"
    r")(?=\s|$)"
)
_WON_PRICE_FILTER_RE = re.compile(
    r"(?:(?<=^)|(?<=\s))(?:"
    r"(?:~|<=|최대)\s*(\d[\d,]{2,})\s*원?"
    r"|(\d[\d,]{2,})\s*(?:원\s*(?:이하|미만|까지|↓)|(?:이하|미만|까지|↓))"
    r")(?=\s|$)"
)
_EXCLUDE_TERM_RE = re.compile(r"(?:(?<=^)|(?<=\s))-([^\s,]+)")


def _sanitize_exclude_terms(raw_terms: Any) -> List[str]:
    if isinstance(raw_terms, (list, tuple, set)):
        parts = [str(x) for x in raw_terms]
    else:
        parts = str(raw_terms or "").split(",")
    clean: List[str] = []
    seen_norms = set()
    for p in parts:
        tok = " ".join(str(p or "").strip().lstrip("-").split())
        norm = normalize_alert_keyword(tok)
        if not norm or norm in seen_norms:
            continue
        seen_norms.add(norm)
        clean.append(tok[:20])
        if len(clean) >= 8:
            break
    return clean


def parse_alert_input(raw_text: str) -> Dict[str, Any]:
    """자연어 키워드 조건 파싱 (예: '에어팟 25만원이하 -케이스', '닭가슴살 30%이상')."""
    text = " ".join(str(raw_text or "").strip().split())
    exclude_terms: List[str] = []
    min_discount = 0
    max_price = 0

    excl_found = _EXCLUDE_TERM_RE.findall(text)
    if excl_found:
        exclude_terms = _sanitize_exclude_terms(excl_found)
        text = _EXCLUDE_TERM_RE.sub(" ", text)

    m_disc = _DISCOUNT_FILTER_RE.search(text)
    if m_disc:
        raw_d = m_disc.group(1) or m_disc.group(2)
        try:
            d_val = int(raw_d)
            if 5 <= d_val <= 90:
                min_discount = d_val
        except Exception:
            pass
        text = _DISCOUNT_FILTER_RE.sub(" ", text)

    m_man = _MAN_PRICE_FILTER_RE.search(text)
    if m_man:
        raw_p = m_man.group(1) or m_man.group(2)
        try:
            p_val = int(round(float(raw_p) * 10000))
            if 1000 <= p_val <= 100000000:
                max_price = p_val
        except Exception:
            pass
        text = _MAN_PRICE_FILTER_RE.sub(" ", text)

    if max_price == 0:
        m_won = _WON_PRICE_FILTER_RE.search(text)
        if m_won:
            raw_w = (m_won.group(1) or m_won.group(2) or "").replace(",", "")
            try:
                w_val = int(raw_w)
                if 1000 <= w_val <= 100000000:
                    max_price = w_val
            except Exception:
                pass
            text = _WON_PRICE_FILTER_RE.sub(" ", text)

    keyword = " ".join(text.strip().split())
    return {
        "keyword": keyword,
        "keyword_norm": normalize_alert_keyword(keyword),
        "max_price": max_price,
        "min_discount": min_discount,
        "exclude_terms": exclude_terms,
    }


def _format_won_compact(price: int) -> str:
    if price <= 0:
        return ""
    if price >= 10000 and price % 10000 == 0:
        return f"{price // 10000}만↓"
    if price >= 10000 and price % 1000 == 0:
        return f"{price / 10000:.1f}만↓"
    return f"{price:,}원↓"


def format_alert_condition_badge(row: Dict[str, Any]) -> str:
    """키워드와 필터 조건을 짧은 한 줄 표기로 포맷."""
    kw = str(row.get("keyword") or "").strip()
    parts: List[str] = []
    max_p = int(row.get("max_price", 0) or 0)
    min_d = int(row.get("min_discount", 0) or 0)
    excl = _sanitize_exclude_terms(row.get("exclude_terms") or "")
    if max_p > 0:
        parts.append(_format_won_compact(max_p))
    if min_d > 0:
        parts.append(f"{min_d}%↑")
    for ex in excl[:2]:
        parts.append(f"-{ex}")
    if len(excl) > 2:
        parts.append(f"외 {len(excl) - 2}제외")
    disp_kw = f"📉 상품추적 #{kw[4:]}" if kw.lower().startswith("pid:") else kw
    if parts:
        return f"{disp_kw} ({' · '.join(parts)})"
    return disp_kw


def list_keyword_alerts_with_prefs(
    chat_id: Any,
    timeout: float = 5.0,
) -> Optional[Tuple[List[Dict[str, Any]], Dict[str, Any]]]:
    """특정 구독자의 키워드 목록과 알림 설정(일시정지/야간무음) 함께 조회."""
    if not is_click_tracker_enabled():
        return None
    cid = str(chat_id or "").strip()
    if not cid:
        return None
    base_url, api_key = _get_tracker_config()
    try:
        res = requests.get(
            f"{base_url}/api/alerts",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"chat_id": cid},
            timeout=timeout,
        )
        if res.status_code != 200:
            logger.warning(f"키워드 알림 조회 HTTP {res.status_code}")
            return None
        data = res.json()
        if isinstance(data, dict) and data.get("ok") and isinstance(data.get("alerts"), list):
            raw_prefs = data.get("prefs") or {}
            prefs = {
                "paused": int(raw_prefs.get("paused", 0) or 0),
                "quiet_night": int(raw_prefs.get("quiet_night", 1) if "quiet_night" in raw_prefs else 1),
            }
            return data["alerts"], prefs
    except Exception as e:
        logger.warning(f"키워드 알림+설정 조회 실패: {e}")
    return None


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


def add_keyword_alert_ex(
    chat_id: Any,
    raw_keyword: str,
    *,
    max_price: int = 0,
    min_discount: int = 0,
    exclude_terms: Optional[List[str]] = None,
    parse_natural: bool = True,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    """자연어 조건 포함 키워드 등록/업데이트 및 최근 매칭 미리보기 반환."""
    parsed = parse_alert_input(raw_keyword) if parse_natural else {
        "keyword": " ".join(str(raw_keyword or "").strip().split()),
        "keyword_norm": normalize_alert_keyword(raw_keyword),
        "max_price": 0,
        "min_discount": 0,
        "exclude_terms": [],
    }
    kw = parsed["keyword"]
    kw_norm = parsed["keyword_norm"]
    eff_max_price = int(max_price or parsed["max_price"] or 0)
    eff_min_disc = int(min_discount or parsed["min_discount"] or 0)
    eff_excl = _sanitize_exclude_terms(exclude_terms if exclude_terms is not None else parsed["exclude_terms"])

    if len(kw) < 2 or len(kw) > 24 or len(kw_norm) < 2:
        return {
            "ok": False,
            "message": "키워드는 2~24자로 입력해 주세요. (예: 에어팟 25만원이하 -케이스)",
            "keyword": kw,
            "recent_matches": [],
        }
    if not is_click_tracker_enabled():
        return {
            "ok": False,
            "message": "알림 서버가 아직 설정되지 않았습니다.",
            "keyword": kw,
            "recent_matches": [],
        }

    base_url, api_key = _get_tracker_config()
    payload = {
        "chat_id": str(chat_id),
        "keyword": kw,
        "max_price": eff_max_price,
        "min_discount": eff_min_disc,
        "exclude_terms": ", ".join(eff_excl),
    }
    try:
        res = requests.post(
            f"{base_url}/api/alerts",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        data = res.json() if res.content else {}
        if res.status_code == 200 and data.get("ok"):
            saved_row = {
                "keyword": str(data.get("keyword") or kw),
                "max_price": int(data.get("max_price", eff_max_price) or eff_max_price),
                "min_discount": int(data.get("min_discount", eff_min_disc) or eff_min_disc),
                "exclude_terms": data.get("exclude_terms", ", ".join(eff_excl)),
            }
            label = format_alert_condition_badge(saved_row)
            recent_matches = list(data.get("recent_matches") or [])
            if data.get("already_exists") and data.get("updated"):
                msg = f"조건을 업데이트했어요: '{label}'"
            elif data.get("already_exists"):
                msg = f"이미 등록된 키워드예요: '{label}'"
            else:
                msg = f"알림 등록 완료: '{label}' ({int(data.get('count', 1) or 1)}/10)"
            return {
                "ok": True,
                "message": msg,
                "keyword": saved_row["keyword"],
                "max_price": saved_row["max_price"],
                "min_discount": saved_row["min_discount"],
                "exclude_terms": eff_excl,
                "already_exists": bool(data.get("already_exists")),
                "updated": bool(data.get("updated")),
                "recent_matches": recent_matches,
            }
        err_code = str(data.get("error") or "")
        if err_code == "limit_exceeded":
            return {
                "ok": False,
                "message": "키워드는 최대 10개까지 등록할 수 있어요. 기존 키워드를 지운 뒤 다시 등록해 주세요.",
                "keyword": kw,
                "recent_matches": [],
            }
        if err_code == "invalid_keyword":
            return {
                "ok": False,
                "message": "키워드는 2~24자로 입력해 주세요.",
                "keyword": kw,
                "recent_matches": [],
            }
        logger.warning(f"키워드 알림 등록 HTTP {res.status_code}: {err_code}")
    except Exception as e:
        logger.warning(f"키워드 알림 등록 실패: {e}")
    return {
        "ok": False,
        "message": "일시적인 오류로 등록하지 못했습니다. 잠시 후 다시 시도해 주세요.",
        "keyword": kw,
        "recent_matches": [],
    }


def add_keyword_alert(
    chat_id: Any,
    keyword: str,
    timeout: float = 5.0,
) -> Tuple[bool, str]:
    """구독자 맞춤 키워드 등록 후 (성공 여부, 안내 문구) 반환."""
    res = add_keyword_alert_ex(chat_id, keyword, timeout=timeout)
    return bool(res.get("ok")), str(res.get("message") or "")


def remove_keyword_alert(
    chat_id: Any,
    keyword: str = "",
    remove_all: bool = False,
    *,
    alert_id: Optional[int] = None,
    purge_prefs: bool = False,
    timeout: float = 5.0,
) -> bool:
    """구독자 맞춤 키워드 단건(ID 또는 키워드) 또는 전체 삭제."""
    if not is_click_tracker_enabled():
        return False
    base_url, api_key = _get_tracker_config()
    params: Dict[str, str] = {"chat_id": str(chat_id)}
    if remove_all:
        params["all"] = "1"
        if purge_prefs:
            params["purge_prefs"] = "1"
    elif alert_id is not None and int(alert_id) > 0:
        params["id"] = str(int(alert_id))
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


def update_alert_prefs(
    chat_id: Any,
    *,
    paused: Optional[bool] = None,
    quiet_night: Optional[bool] = None,
    timeout: float = 5.0,
) -> Optional[Dict[str, Any]]:
    """구독자 알림 설정(일시정지 / 야간무음) 변경."""
    if not is_click_tracker_enabled():
        return None
    base_url, api_key = _get_tracker_config()
    payload: Dict[str, Any] = {"chat_id": str(chat_id)}
    if paused is not None:
        payload["paused"] = bool(paused)
    if quiet_night is not None:
        payload["quiet_night"] = bool(quiet_night)
    try:
        res = requests.post(
            f"{base_url}/api/alerts/prefs",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict) and data.get("ok"):
                return data.get("prefs") or {}
        logger.warning(f"알림 설정 변경 HTTP {res.status_code}")
    except Exception as e:
        logger.warning(f"알림 설정 변경 실패: {e}")
    return None


def fetch_recent_alert_matches(
    chat_id: Optional[Any] = None,
    keyword: str = "",
    *,
    days: int = 14,
    limit: int = 5,
    timeout: float = 5.0,
) -> Optional[List[Dict[str, Any]]]:
    """최근 채널에 올라온 키워드 매칭 핫딜 목록 조회."""
    if not is_click_tracker_enabled():
        return None
    base_url, api_key = _get_tracker_config()
    params: Dict[str, Any] = {"days": int(days), "limit": int(limit)}
    if chat_id is not None and str(chat_id).strip():
        params["chat_id"] = str(chat_id)
    elif keyword:
        params["keyword"] = str(keyword)
    else:
        return []
    try:
        res = requests.get(
            f"{base_url}/api/alerts/recent",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
            timeout=timeout,
        )
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict) and data.get("ok") and isinstance(data.get("matches"), list):
                return data["matches"]
    except Exception as e:
        logger.warning(f"최근 매칭 핫딜 조회 실패: {e}")
    return None


def fetch_alert_stats(timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """관리자용 전체 키워드 알림 구독자 통계 조회."""
    if not is_click_tracker_enabled():
        return None
    base_url, api_key = _get_tracker_config()
    try:
        res = requests.get(
            f"{base_url}/api/alerts/stats",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict) and data.get("ok"):
                return data
    except Exception as e:
        logger.warning(f"키워드 통계 조회 실패: {e}")
    return None


def format_alert_stats_telegram(stats: Dict[str, Any]) -> str:
    """구독자 키워드 알림 통계를 텔레그램 HTML로 포맷."""
    users = int(stats.get("total_users", 0) or 0)
    kws = int(stats.get("total_keywords", 0) or 0)
    paused = int(stats.get("paused_users", 0) or 0)
    hits = int(stats.get("total_hits", 0) or 0)
    lines = [
        "🔔 <b>맞춤 키워드 알림 현황</b>",
        f"• 구독자: <b>{users:,}명</b> (일시정지 {paused:,}명)",
        f"• 등록 키워드: <b>{kws:,}개</b> | 누적 DM 발송: <b>{hits:,}건</b>",
    ]
    top_rows = list(stats.get("top_keywords") or [])[:10]
    if top_rows:
        lines.append("\n🏆 <b>인기 등록 키워드 TOP</b>")
        for idx, r in enumerate(top_rows, 1):
            kw = str(r.get("keyword") or "").strip()
            subs = int(r.get("subscribers", 0) or 0)
            h_cnt = int(r.get("hits", 0) or 0)
            lines.append(f"  {idx}. <code>{html.escape(kw, quote=False)}</code> — {subs:,}명 (알림 {h_cnt:,}회)")
    return "\n".join(lines)


def record_alert_hits(
    hits: List[Tuple[str, str]],
    timeout: float = 3.5,
) -> bool:
    """키워드 DM 발송 적중 카운트 일괄 기록 (fail-open)."""
    if not hits or not is_click_tracker_enabled():
        return False
    base_url, api_key = _get_tracker_config()
    payload_hits = [
        {"chat_id": str(cid), "keyword_norm": str(kn)}
        for cid, kn in hits[:100]
        if str(cid).strip() and str(kn).strip()
    ]
    if not payload_hits:
        return False
    try:
        res = requests.post(
            f"{base_url}/api/alerts/hits",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"hits": payload_hits},
            timeout=timeout,
        )
        return res.status_code == 200
    except Exception as e:
        logger.warning(f"키워드 적중 로그 전송 실패 (무시): {e}")
        return False


def match_alerts_for_deal(
    title: str,
    alerts: Optional[List[Dict[str, Any]]],
    *,
    sale_price: int = 0,
    discount_rate: int = 0,
    product_id: str = "",
) -> Dict[str, Dict[str, Any]]:
    """상품 제목+상품ID+가격+할인율 기준으로 매칭되는 구독자별 상세 결과 반환."""
    title_norm = normalize_alert_keyword(title)
    pid_norm = str(product_id or "").strip().lower()
    if (not title_norm and not pid_norm) or not alerts:
        return {}

    matched: Dict[str, Dict[str, Any]] = {}
    for row in alerts:
        if int(row.get("paused", 0) or 0) == 1:
            continue
        cid = str(row.get("chat_id") or "").strip()
        kw = str(row.get("keyword") or "").strip()
        kw_norm = str(row.get("keyword_norm") or normalize_alert_keyword(kw)).strip()
        if not cid or not kw_norm or len(kw_norm) < 2:
            continue

        if kw_norm.startswith("pid:"):
            target_pid = kw_norm[4:].strip()
            if not pid_norm or not target_pid or pid_norm != target_pid:
                continue
        else:
            if not title_norm:
                continue
            raw_tokens = kw.split() if kw else [kw_norm]
            tokens = [normalize_alert_keyword(t) for t in raw_tokens if normalize_alert_keyword(t)]
            if not tokens:
                continue

            all_tokens_ok = True
            for tok in tokens:
                variants = _TOKEN_VARIANT_MAP.get(tok, (tok,))
                if not any(v in title_norm for v in variants):
                    all_tokens_ok = False
                    break
            if not all_tokens_ok:
                continue

            excl_terms = _sanitize_exclude_terms(row.get("exclude_terms") or "")
            if any(normalize_alert_keyword(ex) in title_norm for ex in excl_terms):
                continue

        max_p = int(row.get("max_price", 0) or 0)
        if max_p > 0 and (int(sale_price or 0) <= 0 or int(sale_price or 0) > max_p):
            continue

        min_d = int(row.get("min_discount", 0) or 0)
        if min_d > 0 and int(discount_rate or 0) < min_d:
            continue

        quiet_night = int(row.get("quiet_night", 1) if "quiet_night" in row else 1) == 1
        bucket = matched.setdefault(
            cid,
            {
                "keywords": [],
                "keyword_norms": [],
                "alert_ids": [],
                "quiet_night": quiet_night,
            },
        )
        disp_kw = f"상품가격하락(#{kw_norm[4:]})" if kw_norm.startswith("pid:") else (kw or kw_norm)
        if disp_kw not in bucket["keywords"]:
            bucket["keywords"].append(disp_kw)
        if kw_norm not in bucket["keyword_norms"]:
            bucket["keyword_norms"].append(kw_norm)
        aid = int(row.get("id", 0) or 0)
        if aid > 0 and aid not in bucket["alert_ids"]:
            bucket["alert_ids"].append(aid)
    return matched


def report_channel_member_count(count: int, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """채널 구독자 수 변화를 Cloudflare D1에 기록하고 delta/순증/마일스톤 반환 (fail-open)."""
    if not is_click_tracker_enabled():
        return None
    base_url, api_key = _get_tracker_config()
    try:
        res = requests.post(
            f"{base_url}/api/channel/members",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"count": int(count)},
            timeout=timeout,
        )
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict) and data.get("ok"):
                return data
        logger.warning(f"채널 구독자 수 기록 HTTP {res.status_code}")
    except Exception as e:
        logger.warning(f"채널 구독자 수 기록 실패: {e}")
    return None


def fetch_channel_member_stats(timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """현재 채널 구독자 수 및 오늘 순증·최근 7일 추이 조회."""
    if not is_click_tracker_enabled():
        return None
    base_url, api_key = _get_tracker_config()
    try:
        res = requests.get(
            f"{base_url}/api/channel/members",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict) and data.get("ok"):
                return data
    except Exception as e:
        logger.warning(f"채널 구독자 통계 조회 실패: {e}")
    return None


def format_member_stats_line(stats: Optional[Dict[str, Any]]) -> str:
    """리포트/클릭 현황용 채널 구독자 1줄 요약 포맷."""
    if not stats or int(stats.get("count", 0) or 0) <= 0:
        return ""
    cnt = int(stats.get("count", 0) or 0)
    net = int(stats.get("today_net", 0) or 0)
    net_str = f"+{net:,}명" if net > 0 else (f"{net:,}명" if net < 0 else "변동 없음")
    return f"👥 <b>채널 구독자</b>: <b>{cnt:,}명</b> (오늘 {net_str})"


def register_posted_message(
    message_id: int,
    chat_id: str,
    caption: str,
    items: List[Dict[str, Any]],
    inline_kb: List[List[Dict[str, Any]]],
    timeout: float = 4.0,
) -> bool:
    """송출된 채널 전단지의 message_id와 품목 목록을 품절 감시용 D1 테이블에 저장."""
    if not message_id or not chat_id or not is_click_tracker_enabled():
        return False
    base_url, api_key = _get_tracker_config()
    try:
        res = requests.post(
            f"{base_url}/api/posts",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "message_id": int(message_id),
                "chat_id": str(chat_id),
                "caption": str(caption or ""),
                "items": list(items or []),
                "inline_kb": list(inline_kb or []),
            },
            timeout=timeout,
        )
        return res.status_code == 200
    except Exception as e:
        logger.warning(f"송출 메시지 등록 실패 (무시): {e}")
        return False


def fetch_active_posted_messages(
    hours: int = 6,
    min_recheck_min: int = 20,
    limit: int = 4,
    timeout: float = 5.0,
) -> List[Dict[str, Any]]:
    """품절/가격정상화 감시 대상 최근 채널 게시물 목록 조회."""
    if not is_click_tracker_enabled():
        return []
    base_url, api_key = _get_tracker_config()
    try:
        res = requests.get(
            f"{base_url}/api/posts/active",
            headers={"Authorization": f"Bearer {api_key}"},
            params={
                "hours": int(hours),
                "min_recheck_min": int(min_recheck_min),
                "limit": int(limit),
            },
            timeout=timeout,
        )
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict) and data.get("ok") and isinstance(data.get("posts"), list):
                return data["posts"]
    except Exception as e:
        logger.warning(f"활성 게시물 조회 실패: {e}")
    return []


def update_posted_message(
    message_id: int,
    *,
    caption: Optional[str] = None,
    items: Optional[List[Dict[str, Any]]] = None,
    inline_kb: Optional[List[List[Dict[str, Any]]]] = None,
    all_closed: Optional[bool] = None,
    timeout: float = 4.0,
) -> bool:
    """품절 감지 후 수정된 캡션·버튼·종료 상태 D1 동기화."""
    if not message_id or not is_click_tracker_enabled():
        return False
    base_url, api_key = _get_tracker_config()
    payload: Dict[str, Any] = {"message_id": int(message_id)}
    if caption is not None:
        payload["caption"] = caption
    if items is not None:
        payload["items"] = items
    if inline_kb is not None:
        payload["inline_kb"] = inline_kb
    if all_closed is not None:
        payload["all_closed"] = bool(all_closed)
    try:
        res = requests.post(
            f"{base_url}/api/posts/update",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        return res.status_code == 200
    except Exception as e:
        logger.warning(f"송출 메시지 갱신 실패 (무시): {e}")
        return False


def match_alerts_for_title(
    title: str,
    alerts: Optional[List[Dict[str, Any]]],
) -> Dict[str, List[str]]:
    """상품 제목과 매칭되는 구독자별 키워드 목록 {chat_id: [keyword, ...]} 반환 (하위 호환)."""
    detailed = match_alerts_for_deal(title, alerts)
    return {cid: info["keywords"] for cid, info in detailed.items()}


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
