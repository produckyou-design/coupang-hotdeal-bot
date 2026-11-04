import hmac
import hashlib
import json
import logging
import os
import time
import urllib.parse
from typing import Any, Dict, List, Optional
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
        self, start_date: str, end_date: str, page: int = 0
    ) -> Dict[str, Any]:
        """
        주문/실적 리포트 조회
        :param start_date: 'YYYYMMDD' 형식
        :param end_date: 'YYYYMMDD' 형식
        """
        path = "/v2/providers/affiliate_open_api/apis/openapi/v1/reports/orders"
        params = {"startDate": start_date, "endDate": end_date, "page": page}
        return self._request("GET", path, params=params)
