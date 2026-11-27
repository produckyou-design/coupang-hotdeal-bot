import hmac
import hashlib
import json
import logging
import os
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple
import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()


class CoupangPartnersAPI:
    """쿠팡 파트너스 OpenAPI 클라이언트"""

    DOMAIN = "https://api-gateway.coupang.com"

    def __init__(self, access_key: Optional[str] = None, secret_key: Optional[str] = None):
        self.access_key = access_key or os.getenv("COUPANG_ACCESS_KEY", "")
        self.secret_key = secret_key or os.getenv("COUPANG_SECRET_KEY", "")

        if not self.access_key or not self.secret_key:
            raise ValueError(
                "쿠팡 ACCESS_KEY와 SECRET_KEY가 필요합니다. "
                ".env 파일에 등록하거나 생성자 매개변수로 전달하세요."
            )

    def _generate_auth_header(self, method: str, path: str, query: str = "") -> str:
        """
        HMAC-SHA256 서명 Authorization 헤더 생성
        주의: 쿠팡 서명 메시지 포맷은 datetime + method + path + query (query 앞에 '?' 없음!)
        """
        datetime_str = (
            time.strftime("%y%m%d", time.gmtime())
            + "T"
            + time.strftime("%H%M%S", time.gmtime())
            + "Z"
        )

        # 서명 메시지: datetime + method + path + query (물음표 미포함)
        message = f"{datetime_str}{method.upper()}{path}{query}"

        # HMAC SHA256 서명 생성
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return f"CEA algorithm=HmacSHA256, access-key={self.access_key}, signed-date={datetime_str}, signature={signature}"

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """API 요청 전송 헬퍼 (429/5xx 지수 백오프 최대 3회, 401은 즉시 예외)"""
        query_string = urllib.parse.urlencode(params) if params else ""
        url = f"{self.DOMAIN}{path}"
        if query_string:
            url = f"{url}?{query_string}"
        json_body = json.dumps(data) if data else None

        last_err: Optional[Exception] = None
        for attempt in range(3):
            auth_header = self._generate_auth_header(method, path, query_string)
            headers = {
                "Authorization": auth_header,
                "Content-Type": "application/json;charset=UTF-8",
            }
            try:
                response = requests.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    data=json_body,
                    timeout=15,
                )
            except requests.RequestException as e:
                last_err = e
                logger.warning(f"쿠팡 API 네트워크 오류 ({path}) {attempt+1}/3: {e}")
                time.sleep([1, 2, 4][attempt])
                continue

            # 401은 키 문제 → 재시도 없이 즉시 예외
            if response.status_code == 401:
                raise RuntimeError(f"쿠팡 API 401 (키 확인 필요) {path}: {response.text[:200]}")
            # 429/5xx는 백오프 후 재시도
            if response.status_code == 429 or 500 <= response.status_code < 600:
                last_err = RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
                logger.warning(f"쿠팡 API {response.status_code} ({path}) {attempt+1}/3 — 백오프")
                time.sleep([1, 2, 4][attempt])
                continue

            try:
                return response.json()
            except Exception:
                response.raise_for_status()
                return {"rCode": str(response.status_code), "rMessage": response.text}

        raise RuntimeError(f"쿠팡 API 실패 ({path}) 3회 재시도 초과: {last_err}")

    def get_goldbox_products(self, sub_id: Optional[str] = None) -> Dict[str, Any]:
        """오늘의 골드박스 상품 조회"""
        path = "/v2/providers/affiliate_open_api/apis/openapi/v1/products/goldbox"
        params = {}
        if sub_id:
            params["subId"] = sub_id
        return self._request("GET", path, params=params if params else None)

    def create_deeplink(
        self, coupang_urls: List[str], sub_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """일반 쿠팡 상품 URL을 파트너스 트래킹 딥링크로 변환"""
        path = "/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink"
        payload: Dict[str, Any] = {"coupangUrls": coupang_urls}
        if sub_id:
            payload["subId"] = sub_id
        return self._request("POST", path, data=payload)

    def search_products(
        self, keyword: str, limit: int = 10, sub_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """키워드로 상품 검색"""
        path = "/v2/providers/affiliate_open_api/apis/openapi/products/search"
        params: Dict[str, Any] = {"keyword": keyword, "limit": limit}
        if sub_id:
            params["subId"] = sub_id
        return self._request("GET", path, params=params)

    def get_best_category_products(
        self, category_id: int, limit: int = 10, sub_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """카테고리별 베스트 상품 조회"""
        path = f"/v2/providers/affiliate_open_api/apis/openapi/products/bestcategories/{category_id}"
        params: Dict[str, Any] = {"limit": limit}
        if sub_id:
            params["subId"] = sub_id
        return self._request("GET", path, params=params)

    def get_order_report(
        self, start_date: str, end_date: str, page: int = 0, sub_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        주문/실적 리포트 조회
        :param start_date: 'YYYYMMDD' 형식
        :param end_date: 'YYYYMMDD' 형식
        """
        path = "/v2/providers/affiliate_open_api/apis/openapi/v1/reports/orders"
        params: Dict[str, Any] = {"startDate": start_date, "endDate": end_date, "page": page}
        if sub_id:
            params["subId"] = sub_id
        return self._request("GET", path, params=params)

    def get_click_report(
        self, start_date: str, end_date: str, sub_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        일별/채널별 클릭 리포트 조회
        :param start_date: 'YYYYMMDD' 형식
        :param end_date: 'YYYYMMDD' 형식
        """
        path = "/v2/providers/affiliate_open_api/apis/openapi/v1/reports/clicks"
        params: Dict[str, Any] = {"startDate": start_date, "endDate": end_date}
        if sub_id:
            params["subId"] = sub_id
        return self._request("GET", path, params=params)

    def get_commission_report(
        self, start_date: str, end_date: str, sub_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        일별 클릭/주문/취소/확정 수익 리포트 조회
        :param start_date: 'YYYYMMDD' 형식
        :param end_date: 'YYYYMMDD' 형식
        """
        path = "/v2/providers/affiliate_open_api/apis/openapi/v1/reports/commission"
        params: Dict[str, Any] = {"startDate": start_date, "endDate": end_date}
        if sub_id:
            params["subId"] = sub_id
        return self._request("GET", path, params=params)

    def get_cancel_report(
        self, start_date: str, end_date: str, sub_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        취소 내역 리포트 조회
        :param start_date: 'YYYYMMDD' 형식
        :param end_date: 'YYYYMMDD' 형식
        """
        path = "/v2/providers/affiliate_open_api/apis/openapi/v1/reports/cancels"
        params: Dict[str, Any] = {"startDate": start_date, "endDate": end_date}
        if sub_id:
            params["subId"] = sub_id
        return self._request("GET", path, params=params)


def classify_sub_id(sub_id: Optional[str]) -> Tuple[str, str]:
    """쿠팡 파트너스 subId를 유입 채널 그룹('telegram' | 'poliwiki' | 'other')과 세부 라벨로 분류"""
    s = (sub_id or "").strip().lower()
    if not s:
        return "poliwiki", "폴리위키/미지정"
    if s.startswith(("tg", "auto_", "hotpang")):
        if "arca" in s:
            return "telegram", "아카라이브"
        if "ruli" in s:
            return "telegram", "루리웹"
        if "ppom" in s:
            return "telegram", "뽐뿌"
        if "dealbada" in s:
            return "telegram", "딜바다"
        if "clien" in s:
            return "telegram", "클리앙"
        if "gold" in s:
            return "telegram", "골드박스"
        if "manual" in s:
            return "telegram", "수동링크"
        if "search" in s:
            return "telegram", "봇검색"
        return "telegram", "기타"
    return "other", s


def _empty_stat_bucket() -> Dict[str, int]:
    return {"clicks": 0, "orders": 0, "cancels": 0, "gmv": 0, "commission": 0}


def _add_comm_row(bucket: Dict[str, int], row: Dict[str, Any]) -> None:
    bucket["clicks"] += int(row.get("click", 0) or 0)
    bucket["orders"] += int(row.get("order", 0) or 0)
    bucket["cancels"] += int(row.get("cancel", 0) or 0)
    bucket["gmv"] += int(round(float(row.get("gmv", 0) or 0)))
    bucket["commission"] += int(round(float(row.get("commission", 0) or 0)))


def summarize_partner_stats(
    comm_rows: List[Dict[str, Any]],
    order_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """commission 리포트 행과 주문 행을 채널 그룹(telegram/poliwiki/other) 및 텔레그램 소스별로 집계"""
    total = _empty_stat_bucket()
    telegram = _empty_stat_bucket()
    poliwiki = _empty_stat_bucket()
    other = _empty_stat_bucket()
    tg_sources: Dict[str, Dict[str, int]] = {}

    groups = {"telegram": telegram, "poliwiki": poliwiki, "other": other}
    for row in comm_rows or []:
        _add_comm_row(total, row)
        grp, src_label = classify_sub_id(row.get("subId"))
        _add_comm_row(groups[grp], row)
        if grp == "telegram":
            src_bucket = tg_sources.setdefault(src_label, _empty_stat_bucket())
            _add_comm_row(src_bucket, row)

    active_tg_sources = {
        k: {key: val[key] for key in ("clicks", "orders", "gmv", "commission")}
        for k, val in tg_sources.items()
        if any(val[key] > 0 for key in ("clicks", "orders", "cancels", "gmv", "commission"))
    }

    latest_date = max((str(r.get("date") or "") for r in (comm_rows or [])), default="")
    latest_total = _empty_stat_bucket()
    latest_telegram = _empty_stat_bucket()
    latest_poliwiki = _empty_stat_bucket()
    if latest_date:
        for row in comm_rows or []:
            if str(row.get("date") or "") != latest_date:
                continue
            _add_comm_row(latest_total, row)
            grp, _ = classify_sub_id(row.get("subId"))
            if grp == "telegram":
                _add_comm_row(latest_telegram, row)
            elif grp == "poliwiki":
                _add_comm_row(latest_poliwiki, row)

    tagged_orders: List[Dict[str, Any]] = []
    for it in order_rows or []:
        grp, src_label = classify_sub_id(it.get("subId"))
        tagged = dict(it)
        tagged["channel_group"] = grp
        tagged["source_label"] = src_label
        tagged_orders.append(tagged)

    return {
        "total": total,
        "telegram": telegram,
        "poliwiki": poliwiki,
        "other": other,
        "tg_sources": active_tg_sources,
        "latest_date": latest_date,
        "latest_total": latest_total,
        "latest_telegram": latest_telegram,
        "latest_poliwiki": latest_poliwiki,
        "tagged_orders": tagged_orders,
    }


def format_order_source_badge(sub_id: Optional[str]) -> str:
    """주문 항목 한 줄 표기용 유입 출처 배지"""
    grp, src_label = classify_sub_id(sub_id)
    if grp == "telegram":
        return f"✈️텔레그램·{src_label}"
    if grp == "poliwiki":
        return "🌐폴리위키"
    return f"🔖{src_label}"


def format_channel_split_block(stats: Dict[str, Any], heading: str) -> str:
    """텔레그램 vs 폴리위키 분리 집계 HTML 블록 생성"""
    tg = stats["telegram"]
    pw = stats["poliwiki"]
    ot = stats["other"]
    lines = [
        f"📡 <b>{heading}</b>",
        f"• ✈️ 텔레그램(@HOTpang911): 클릭 <b>{tg['clicks']:,}회</b> / 주문 <b>{tg['orders']:,}건</b> / {tg['gmv']:,}원 (수익 <b>{tg['commission']:,}원</b>)",
    ]
    tg_sources = stats.get("tg_sources") or {}
    if tg_sources:
        src_parts = [
            f"{src}: {v['clicks']:,}클릭·{v['orders']:,}건"
            for src, v in tg_sources.items()
        ]
        lines.append(f"  └ {', '.join(src_parts)}")
    lines.append(
        f"• 🌐 폴리위키/미지정: 클릭 <b>{pw['clicks']:,}회</b> / 주문 <b>{pw['orders']:,}건</b> / {pw['gmv']:,}원 (수익 <b>{pw['commission']:,}원</b>)"
    )
    if any(ot[k] > 0 for k in ("clicks", "orders", "gmv", "commission")):
        lines.append(
            f"• 🔖 기타 채널: 클릭 <b>{ot['clicks']:,}회</b> / 주문 <b>{ot['orders']:,}건</b> / {ot['gmv']:,}원 (수익 <b>{ot['commission']:,}원</b>)"
        )
    return "\n".join(lines)
