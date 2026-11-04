import json
import logging
import os
import re
from typing import Any, Dict, List, Optional
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SEEN_FILE = os.path.join(os.path.dirname(__file__), "seen_deals.json")


def load_seen_deals() -> set:
    """이미 송출한 글 ID 목록 로드"""
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            logger.warning(f"seen_deals.json 읽기 오류: {e}")
    return set()


def save_seen_deals(seen: set):
    """송출한 글 ID 목록 저장 (최근 500개 유지)"""
    try:
        recent = list(seen)[-500:]
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(recent, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"seen_deals.json 저장 오류: {e}")


class DealCrawler:
    """커뮤니티 검증 쿠팡 핫딜 크롤러"""

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    @classmethod
    def fetch_ruliweb_deals(cls) -> List[Dict[str, Any]]:
        """루리웹 핫딜 게시판에서 쿠팡 핫딜 수집"""
        url = "https://bbs.ruliweb.com/market/board/1020"
        deals = []
        try:
            res = requests.get(url, headers=cls.HEADERS, timeout=8)
            if res.status_code != 200:
                logger.warning(f"루리웹 접근 실패: {res.status_code}")
                return deals

            soup = BeautifulSoup(res.text, "html.parser")
            rows = soup.select("tr.table_body")

            for row in rows:
                subject_tag = row.select_one("a.subject_link")
                if not subject_tag:
                    continue

                title = " ".join(subject_tag.text.split())
                # '쿠팡' 키워드가 들어간 핫딜만 필터링
                if "쿠팡" not in title and "coupang" not in title.lower():
                    continue

                link = subject_tag.get("href", "")
                if not link:
                    continue

                # 글 번호(ID) 추출
                match = re.search(r"/read/(\d+)", link)
                post_id = f"ruliweb_{match.group(1)}" if match else link

                deals.append({"id": post_id, "title": title, "link": link})

        except Exception as e:
            logger.error(f"루리웹 목록 크롤링 오류: {e}")

        return deals

    @classmethod
    def parse_deal_detail(cls, post_url: str) -> Optional[Dict[str, Any]]:
        """핫딜 글 상세 내용 파싱: 쿠팡 원본 URL 및 왜 특가인지 이유 추출"""
        try:
            res = requests.get(post_url, headers=cls.HEADERS, timeout=8)
            if res.status_code != 200:
                return None

            soup = BeautifulSoup(res.text, "html.parser")

            # 1. 쿠팡 원본 URL 추출
            coupang_url = None
            # 원문 출처 링크
            source_link = soup.select_one("div.source_url a")
            if source_link and "coupang.com" in source_link.get("href", ""):
                coupang_url = source_link["href"]
                # 루리웹 리다이렉트 url 정리 (ol= 뒤의 원본 링크 추출)
                ol_match = re.search(r"ol=([^&]+)", coupang_url)
                if ol_match:
                    import urllib.parse
                    coupang_url = urllib.parse.unquote(ol_match.group(1))

            if not coupang_url:
                # 본문 내 a 태그 및 텍스트에서 검색
                all_cp = re.findall(
                    r"https?://(?:www\.|m\.)?coupang\.com/vp/products/\d+[^\s\"\'<>]*",
                    res.text,
                )
                if all_cp:
                    coupang_url = all_cp[0]

            if not coupang_url:
                return None

            # 2. 본문 텍스트 요약 (왜 싼지, 쿠폰 정보 등)
            content_div = soup.select_one("div.view_content")
            reason_snippet = ""
            if content_div:
                lines = [
                    l.strip()
                    for l in content_div.text.splitlines()
                    if l.strip() and not l.strip().startswith("http")
                ]
                reason_snippet = " / ".join(lines[:3])
                # 너무 길면 자르기
                if len(reason_snippet) > 120:
                    reason_snippet = reason_snippet[:117] + "..."

            # 3. 본문 내 대표 이미지
            img_url = None
            if content_div:
                first_img = content_div.select_one("img")
                if first_img:
                    img_url = first_img.get("src")
                    if img_url and img_url.startswith("//"):
                        img_url = "https:" + img_url

            return {
                "coupang_url": coupang_url,
                "reason": reason_snippet or "커뮤니티 추천 초특가 핫딜",
                "image_url": img_url,
            }

        except Exception as e:
            logger.error(f"상세 파싱 오류 ({post_url}): {e}")
            return None
