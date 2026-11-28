"""SQLite 기반 가격 관측 이력, 단위가격 파싱, 카테고리 분류, 대기 큐 및 출처 쿨다운 관리 모듈."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

KST = timezone(timedelta(hours=9))

_IT_STRICT_KEYWORDS = (
    "그래픽카드", "노트북", "모니터", "태블릿", "아이패드", "갤럭시탭", "갤럭시북",
    "맥북", "lg전자", "lg 그램", "그램 ai", "울트라피씨", "스마트폰", "자급제", "키보드", "마우스", "공유기", "이어폰", "헤드셋",
    "헤드폰", "스피커", "무선충전", "보조배터리", "충전기", "메모리카드", "외장하드",
    "외장ssd", "마이크로sd", "메인보드", "파워서플라이", "수랭쿨러", "공랭쿨러",
    "로봇청소기", "무선청소기", "물걸레청소기", "공기청정기", "가습기", "제습기",
    "에어프라이어", "전자레인지", "커피머신", "에스프레소", "식기세척기", "음식물처리기",
    "건조기", "세탁기", "냉장고", "김치냉장고", "빔프로젝터", "웹캠", "캡처보드",
    "닌텐도", "플레이스테이션", "엑스박스", "스팀덱", "게이밍", "전자책", "리더기",
    "스마트워치", "갤럭시워치", "애플워치", "에어팟", "버즈",
)
_IT_TOKEN_RE = re.compile(
    r"(?:^|[^a-z0-9가-힣])(?:ssd|hdd|nvme|ram|cpu|gpu|nas|usb|oled|qled|uhd|fhd|qhd|4k|8k|ps5|xbox|pc|tv|144hz|165hz|240hz|360hz|mini\s*led|sdxc)(?:[^a-z0-9가-힣]|$)",
    re.IGNORECASE,
)

_LIVING_KEYWORDS = (
    "휴지", "화장지", "롤휴지", "각티슈", "미용티슈", "물티슈", "키친타월", "핸드타월",
    "냅킨", "세제", "세탁세제", "주방세제", "섬유유연제", "다우니", "퍼실", "테크",
    "피지", "액체세제", "캡슐세제", "표백제", "산소계표백제", "과탄산소다", "베이킹소다",
    "구연산", "샴푸", "린스", "트리트먼트", "바디워시", "바디로션", "비누", "핸드워시",
    "치약", "칫솔", "가글", "치실", "면도기", "면도날", "질레트", "도루코", "쉬크",
    "로션", "수분크림", "선크림", "썬크림", "클렌징", "폼클렌징", "마스크팩", "화장솜",
    "생리대", "라이너", "탐폰", "기저귀", "하기스", "팸퍼스", "페넬로페", "배변패드",
    "고양이모래", "벤토나이트", "두부모래", "탈취제", "방향제", "페브리즈", "디퓨저",
    "락스", "곰팡이제거", "배수구", "변기세정", "청소포", "밀대", "행주", "수세미",
    "지퍼백", "위생백", "랩", "호일", "종이호일", "음식물봉투", "쓰레기봉투",
    "건전지", "알카라인", "멀티탭", "전구", "수건", "타월", "양말", "속옷", "팬티",
    "kf94", "kf80", "마스크", "밴드", "습윤밴드", "워셔액", "제습제", "물먹는하마",
)

_FOOD_KEYWORDS = (
    "생수", "먹는샘물", "삼다수", "백산수", "아이시스", "스파클", "에비앙", "탄산수",
    "트레비", "씨그램", "페리에", "아메리카노", "원두", "드립백", "캡슐커피", "믹스커피",
    "라떼", "콜드브루", "두유", "우유", "멸균우유", "요거트", "요구르트", "그릭요거트",
    "주스", "오렌지주스", "사과주스", "콜라", "사이다", "펩시", "코카콜라", "웰치스",
    "환타", "게토레이", "파워에이드", "포카리", "토레타", "몬스터", "레드불", "핫식스",
    "이온음료", "제로음료", "과자", "스낵", "초콜릿", "초코바", "트윅스", "스니커즈",
    "abc초콜릿", "에이비씨", "빅파이", "꼬북칩", "오감자", "포카칩", "프링글스",
    "젤리", "하리보", "마이구미", "사탕", "캔디", "쿠키", "비스킷", "크래커", "웨하스",
    "시리얼", "그래놀라", "오트밀", "프로틴", "단백질", "쉐이크", "닭가슴살",
    "라면", "컵라면", "신라면", "진라면", "안성탕면", "너구리", "짜파게티", "불닭",
    "우동", "소바", "파스타", "스파게티", "냉면", "쫄면", "비빔면", "쌀국수",
    "즉석밥", "햇반", "오뚜기밥", "현미밥", "백미", "잡곡", "찹쌀", "귀리",
    "김치", "포기김치", "묵은지", "만두", "비비고", "교자", "치킨", "너겟", "텐더",
    "돈까스", "탕수육", "소시지", "후랑크", "햄", "스팸", "리챔", "런천미트", "베이컨",
    "삼겹살", "목살", "항정살", "앞다리", "소고기", "한우", "차돌박이", "갈비", "부채살",
    "돼지고기", "오리고기", "훈제오리", "연어", "고등어", "갈치", "새우", "오징어",
    "참치", "동원참치", "사조참치", "골뱅이", "황도", "백도", "통조림",
    "죽", "스프", "카레", "짜장", "소스", "케첩", "마요네즈", "머스타드", "간장",
    "고추장", "된장", "쌈장", "올리브유", "아보카도오일", "식용유", "카놀라유", "참기름",
    "들기름", "설탕", "알룰로스", "스테비아", "소금", "후추", "아몬드", "호두", "캐슈넛",
    "피스타치오", "견과", "아이스크림", "하겐다즈", "나뚜루", "밀키트", "도시락", "샐러드",
    "사과", "귤", "천혜향", "레드향", "오렌지", "바나나", "포도", "샤인머스캣", "토마토",
    "방울토마토", "고구마", "감자", "양파", "마늘", "도시락김", "조미김", "김자반", "재래김",
    "구운김", "미역", "다시마", "멸치", "어묵", "떡볶이", "가래떡", "떡", "빵", "식빵",
    "모닝빵", "베이글", "카스테라", "케이크", "홍삼", "정관장", "비타민", "유산균",
    "락토핏", "오메가3", "루테인", "밀크씨슬", "마그네슘", "아연", "콜라겐", "영양제",
    "사료", "고양이사료", "강아지사료", "츄르", "개껌", "반려동물간식", "국밥", "설렁탕",
    "육개장", "곰탕", "갈비탕", "부대찌개", "된장찌개", "김치찌개", "핫도그", "피자",
    "오리온", "미쯔", "오뜨", "치즈", "황치즈", "블루베리", "퓨레", "즙", "케익",
    "보리차", "하늘보리", "계란과자", "당뇨식", "케어웰", "호밍스", "사골", "진곰탕",
    "멸균", "유기농", "크라운", "해태", "농심", "오뚜기", "삼양", "팔도", "빙그레",
    "매일", "남양", "서울우유", "동서", "맥심", "카누", "청정원", "풀무원", "하림",
    "사조", "동원", "칠성", "나랑드", "스프라이트", "닥터페퍼", "파인애플", "복숭아",
    "망고", "딸기", "초코파이", "몽쉘", "카스타드", "새우깡", "감자칩", "팝콘", "크런키",
    "로아커", "누텔라", "디저트", "음료", "녹차", "홍차", "콤부차", "유자차", "아몬드브리즈", "오트사이드",
)


def classify_deal_category(title: str, body_text: str = "") -> str:
    """제목과 본문을 기반으로 딜 카테고리를 분류('food' | 'living' | 'it' | 'general')."""
    title_l = (title or "").lower()
    body_l = (body_text or "").lower()
    combined = f"{title_l} {body_l}".strip()
    if not combined:
        return "general"

    # IT 가전/PC 부품은 우선 판별 (단, '커피' 등이 함께 있는 커피머신도 IT/가전으로)
    for kw in _IT_STRICT_KEYWORDS:
        if kw in title_l:
            return "it"
    if _IT_TOKEN_RE.search(title_l):
        return "it"

    # 생활용품은 식품보다 먼저 검사 ('물티슈', '주방세제', '핸드워시' 등의 오분류 방지)
    for kw in _LIVING_KEYWORDS:
        if kw in title_l:
            return "living"

    for kw in _FOOD_KEYWORDS:
        if kw in title_l:
            return "food"

    # 제목으로 판별되지 않은 경우 본문 힌트 보조 확인
    for kw in _IT_STRICT_KEYWORDS:
        if kw in body_l:
            return "it"
    if _IT_TOKEN_RE.search(body_l):
        return "it"
    for kw in _LIVING_KEYWORDS:
        if kw in body_l:
            return "living"
    for kw in _FOOD_KEYWORDS:
        if kw in body_l:
            return "food"

    return "general"


_COUNT_UNIT_TIER1 = {
    "개", "개입", "입", "병", "캔", "봉", "봉지", "포", "매", "롤", "정", "캡슐", "페트", "알", "통",
}
_COUNT_UNIT_TIER2 = {"팩", "박스", "상자", "묶음", "세트"}
_COUNT_UNIT_CANON = {"개입": "개", "입": "개", "봉지": "봉", "페트": "병"}
_ALL_COUNT_UNITS = _COUNT_UNIT_TIER1 | _COUNT_UNIT_TIER2
_ALL_COUNT_UNIT_ALT = "개입|개|봉지|봉|병|캔|팩|입|롤|매|정|캡슐|포|페트|알|통|박스|상자|묶음|세트"
_NUM_COMMAS_RE = r"(?:\d{1,3}(?:,\d{3})+|\d+)"

_PURCHASE_LIMIT_INLINE_RE = re.compile(
    rf"(?:"
    rf"(?:1\s*인(?:\s*당)?)[ \t]*[:：]?[ \t]*(?:(?:1\s*회|1\s*일|일일|당일|최대)\s*){{0,2}}{_NUM_COMMAS_RE}\s*(?:{_ALL_COUNT_UNIT_ALT})\s*(?:최대|구매|주문|수량)?\s*(?:제한|한정|가능|까지|이하)"
    rf"|(?:인당|계정당|아이디당|id\s*당|주문당|1\s*회당|회원당)[ \t]*[:：]?[ \t]*(?:(?:1\s*회|1\s*일|일일|당일|최대)\s*){{0,2}}{_NUM_COMMAS_RE}\s*(?:{_ALL_COUNT_UNIT_ALT})(?:\s*(?:구매|주문|수량)?\s*(?:제한|한정|가능|까지|이하))?"
    rf"|1\s*인(?:\s*당)?[ \t]*[:：]?[ \t]*최대[ \t]*{_NUM_COMMAS_RE}\s*(?:{_ALL_COUNT_UNIT_ALT})(?:\s*(?:구매|주문|수량)?\s*(?:제한|한정|가능|까지|이하))?"
    rf"|1\s*회[ \t]*최대[ \t]*{_NUM_COMMAS_RE}\s*(?:{_ALL_COUNT_UNIT_ALT})(?:\s*(?:구매|주문|수량)?\s*(?:제한|한정|가능|까지|이하))?"
    rf"|최대[ \t]*{_NUM_COMMAS_RE}\s*(?:{_ALL_COUNT_UNIT_ALT})\s*(?:구매|주문|수량)?\s*(?:제한|한정|가능|까지|이하)"
    rf"|(?:선착순[ \t]+)?{_NUM_COMMAS_RE}\s*(?:{_ALL_COUNT_UNIT_ALT})\s*(?:(?:구매|주문)\s*(?:제한|한정)|까지\s*(?:구매|주문)\s*(?:제한|한정)|구매\s*가능|제한)"
    rf")",
    re.IGNORECASE,
)


def strip_purchase_limit_phrases(text: str) -> str:
    """'1인 2박스 구매 제한', '계정당 최대 2개 한정' 등 구매 수량 제한 절만 제거(괄호 내 정상 구성·개행 보존)."""
    if not text:
        return ""
    s = _PURCHASE_LIMIT_INLINE_RE.sub(" ", text)
    s = re.sub(r"\(\s*[,/·\-]+\s*", "(", s)
    s = re.sub(r"\s*[,/·\-]+\s*\)", ")", s)
    s = re.sub(r"\[\s*[,/·\-]+\s*", "[", s)
    s = re.sub(r"\s*[,/·\-]+\s*\]", "]", s)
    s = re.sub(r"\(\s*\)|\[\s*\]", " ", s)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


_AMBIGUOUS_OPTION_PATTERNS = (
    re.compile(r"(?:택\s*\d|택일|중\s*택|선택\s*가능|옵션\s*선택|옵션별|구성별|종류별|골라담기)"),
    re.compile(
        rf"(?<!\d)(?<!\d,)(?<!\d\.){_NUM_COMMAS_RE}(?:\.\d+)?\s*(?:{_ALL_COUNT_UNIT_ALT}|kg|g|ml|l|리터|킬로)\s*(?:또는|혹은|\bor\b)\s*{_NUM_COMMAS_RE}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?<!\d)(?<!\d,)(?<!\d\.){_NUM_COMMAS_RE}\s*(?:{_ALL_COUNT_UNIT_ALT})?\s*(?:~|/)\s*{_NUM_COMMAS_RE}\s*(?:{_ALL_COUNT_UNIT_ALT})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?<!\d)(?<!\d,)(?<!\d\.){_NUM_COMMAS_RE}(?:\.\d+)?\s*(?:ml|l|리터)\s*(?:~|/)\s*{_NUM_COMMAS_RE}(?:\.\d+)?\s*(?:ml|l|리터)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?<!\d)(?<!\d,)(?<!\d\.){_NUM_COMMAS_RE}(?:\.\d+)?\s*(?:kg|g|킬로)\s*(?:~|/)\s*{_NUM_COMMAS_RE}(?:\.\d+)?\s*(?:kg|g|킬로)\b",
        re.IGNORECASE,
    ),
)

_AMBIGUOUS_PRICE_RE = re.compile(
    r"\d[\d,]*\s*원\s*(?:부터|~)|최대\s*할인|체감가"
    r"|(?:적립|캐시|포인트)\s*(?:포함|후|적용\s*시?)\s*[\d,]+\s*원"
    r"|카드\s*(?:할인|청구)\s*(?:포함|적용|시)\s*[\d,]+\s*원"
)

_PAIR_NOISE_THRESHOLD_RE = re.compile(
    r"\d[\d,]*\s*(?:원|만\s*원)\s*이상(?:\s*(?:구매|주문|결제)?\s*(?:시|일\s*때)?\s*)?(?:무료배송|무배|배송비\s*무료|무료)",
    re.IGNORECASE,
)
_PAIR_NOISE_UNIT_PRICE_RE = re.compile(
    rf"(?:(?:\d+(?:\.\d+)?\s*)?(?:kg|g|ml|l|리터|킬로|{_ALL_COUNT_UNIT_ALT})\s*당"
    rf"|(?<!\d)(?:1|10|100)\s*(?:kg|g|ml|l|리터|킬로|{_ALL_COUNT_UNIT_ALT})\s*에)"
    rf"\s*(?:약\s*)?\d[\d,]*\s*원(?:\s*꼴)?",
    re.IGNORECASE,
)
_PAIR_NOISE_FORWARD_SHIP_RE = re.compile(
    rf"(?:유료배송|배송비|택배비|배송료|운송비)[ \t]*(?:은|는|이|가)?[ \t]*(?:별도[ \t]*)?[:：\+\-\(]?[ \t]*(?:{_NUM_COMMAS_RE}[ \t]*원\)?|무료|0[ \t]*원|없음|별도|착불|포함|제외|있음)",
    re.IGNORECASE,
)
_PAIR_NOISE_PAREN_REVERSE_SHIP_RE = re.compile(
    rf"[\(\[][ \t]*{_NUM_COMMAS_RE}[ \t]*원[ \t]*(?:배송비|택배비|배송료|운송비)(?:[ \t]*(?:별도|착불))?[ \t]*[\)\]]",
    re.IGNORECASE,
)
_PAIR_NOISE_REVERSE_SHIP_RE = re.compile(
    rf"{_NUM_COMMAS_RE}[ \t]*원[ \t]*(?:배송비|택배비|배송료|운송비)(?:[ \t]*(?:별도|착불))?",
    re.IGNORECASE,
)

_OPTION_PRICE_PAIR_RE = re.compile(
    rf"(?:"
    rf"(?<!\d)(?<!\d,)(?<!\d\.)(?P<wv_num>{_NUM_COMMAS_RE}(?:\.\d+)?)\s*(?P<wv_unit>kg|g|ml|l|리터|킬로)(?![a-zA-Z])"
    rf"|(?<!\d)(?<!\d,)(?<!\d\.)(?P<cnt_num>{_NUM_COMMAS_RE})\s*(?P<cnt_unit>{_ALL_COUNT_UNIT_ALT})"
    rf")"
    rf"(?!\s*당)(?!\s*(?:무료|이상|미만|배송|택배|착불))"
    rf"(?:(?:은|는|이|가|에|의|짜리|들이)?"
    rf"(?:\s*(?:기준(?:으로)?|짜리|옵션|구성|가격|판매가|할인가|특가))?"
    rf"(?:\s*(?:은|는|이|가|에))?"
    rf"\s*(?:->|=>|→|➡️?|[:：=\-(\[~])?\s*)"
    rf"(?P<price>\d{{1,3}}(?:,\d{{3}})+|\d{{4,7}})\s*원(?!\s*(?:할인|쿠폰|절약|적립|캐시|이상|미만))",
    re.IGNORECASE,
)


def _canon_pair_spec_key(num_s: str, unit_s: str, is_wv: bool) -> Tuple[str, str, int]:
    ul = unit_s.lower()
    if is_wv:
        val = float(num_s.replace(",", ""))
        if ul in ("kg", "킬로"):
            return f"wt:{_fmt_spec_num(val * 1000.0)}g", "wt", int(round(val * 1000))
        if ul == "g":
            return f"wt:{_fmt_spec_num(val)}g", "wt", int(round(val))
        if ul in ("l", "리터"):
            return f"vol:{_fmt_spec_num(val * 1000.0)}ml", "vol", int(round(val * 1000))
        return f"vol:{_fmt_spec_num(val)}ml", "vol", int(round(val))
    canon_u = _COUNT_UNIT_CANON.get(ul, ul)
    n = int(num_s.replace(",", ""))
    return f"cnt:{canon_u}:{n}", f"cnt_t{_unit_tier(canon_u)}", n


def _extract_option_price_pairs(text: str) -> List[Tuple[str, str, int, int]]:
    """텍스트에서 '(구성키, 단위군, 수치, 가격원)' 목록 추출 (배송비·단가·쿠폰/할인 제외)."""
    if not text:
        return []
    cleaned = strip_purchase_limit_phrases(text)
    cleaned = _PAIR_NOISE_THRESHOLD_RE.sub(" ", cleaned)
    cleaned = _PAIR_NOISE_UNIT_PRICE_RE.sub(" ", cleaned)
    cleaned = _PAIR_NOISE_FORWARD_SHIP_RE.sub(" ", cleaned)
    cleaned = _PAIR_NOISE_PAREN_REVERSE_SHIP_RE.sub(" ", cleaned)
    cleaned = _PAIR_NOISE_REVERSE_SHIP_RE.sub(" ", cleaned)
    # Track A 할인 화살표 앞 정상가(예: '40병 25,000원 -> 17,900원')는 최종 할인가와 병합
    cleaned = re.sub(rf"{_NUM_COMMAS_RE}\s*원\s*(?:에서|->|=>|→|~>)\s*", "", cleaned)
    pairs: List[Tuple[str, str, int, int]] = []
    for m in _OPTION_PRICE_PAIR_RE.finditer(cleaned):
        price = int(m.group("price").replace(",", ""))
        if price < 1000:
            continue
        if m.group("wv_num"):
            spec_k, fam, mag = _canon_pair_spec_key(m.group("wv_num"), m.group("wv_unit"), is_wv=True)
        else:
            spec_k, fam, mag = _canon_pair_spec_key(m.group("cnt_num"), m.group("cnt_unit"), is_wv=False)
        pairs.append((spec_k, fam, mag, price))
    return pairs


def _extract_bundle_total_t1_count(clean_title: str) -> Optional[int]:
    s = _PAREN_TOTAL_SUMMARY_RE.sub(" ", clean_title or "")
    s = _DUP_PAREN_COUNT_RE.sub(r"\1\2", s)
    s = re.sub(r"[\[\]\(\)]", " ", s)
    toks = [
        (int(m.group(1).replace(",", "")), m.group(2), m.start(), m.end())
        for m in _COUNT_ONLY_TOKEN_RE.finditer(s)
        if int(m.group(1).replace(",", "")) >= 2
    ]
    if len(toks) == 2:
        (n1, u1, _s1, e1), (n2, u2, s2, _e2) = toks
        if _unit_tier(u1) == 1 and _unit_tier(u2) == 2:
            return n1 * n2
        if re.match(r"^\s*[x×*]\s*$", s[e1:s2]):
            return n1 * n2
    return None


def has_ambiguous_option_or_price(
    title: str,
    body_text: str = "",
    sale_price: int = 0,
) -> Tuple[bool, str]:
    """제목 및 본문에서 다중 옵션 선택(택1/수량별 가격 쌍/제목-본문 불일치)이나 비확정 가격 감지."""
    clean_title = strip_purchase_limit_phrases(title or "")
    clean_body = strip_purchase_limit_phrases(body_text or "")
    text = re.sub(r"\s+", " ", f"{clean_title} {clean_body}").strip()
    if not text:
        return False, ""
    if _AMBIGUOUS_PRICE_RE.search(text):
        return True, "ambiguous_price"
    for pat in _AMBIGUOUS_OPTION_PATTERNS:
        if pat.search(text):
            return True, "ambiguous_option"

    body_pairs = _extract_option_price_pairs(clean_body)
    distinct_body = {(sk, pr) for sk, _fam, _mag, pr in body_pairs}
    if len(distinct_body) >= 2:
        return True, "ambiguous_option_price_pairs"

    title_pairs = _extract_option_price_pairs(clean_title)
    distinct_title = {(sk, pr) for sk, _fam, _mag, pr in title_pairs}
    if len(distinct_title) >= 2:
        return True, "ambiguous_option_price_pairs"

    if not title_pairs and sale_price >= 1000:
        cleaned_t = re.sub(r"[\[\]\(\)]", " ", _PAREN_TOTAL_SUMMARY_RE.sub(" ", clean_title))
        for cm in _COUNT_ONLY_TOKEN_RE.finditer(cleaned_t):
            cnt = int(cm.group(1).replace(",", ""))
            if cnt >= 2:
                sk, fam, mag = _canon_pair_spec_key(cm.group(1), cm.group(2), is_wv=False)
                title_pairs.append((sk, fam, mag, int(sale_price)))
        for wm in _WEIGHT_VOL_SPEC_RE.finditer(cleaned_t):
            sk, fam, mag = _canon_pair_spec_key(wm.group(1), wm.group(2), is_wv=True)
            title_pairs.append((sk, fam, mag, int(sale_price)))

    bundle_total = _extract_bundle_total_t1_count(clean_title)
    if bundle_total is not None:
        ref_price = int(sale_price) if sale_price >= 1000 else (title_pairs[0][3] if title_pairs else 0)
        if ref_price >= 1000:
            title_pairs.append((f"cnt:total:{bundle_total}", "cnt_t1", bundle_total, ref_price))

    if body_pairs and title_pairs:
        for b_sk, b_fam, b_mag, b_pr in body_pairs:
            for t_sk, t_fam, t_mag, t_pr in title_pairs:
                if b_fam == t_fam and b_mag == t_mag and b_pr != t_pr:
                    return True, "title_body_price_conflict"
                if b_fam == t_fam and b_mag != t_mag and b_pr == t_pr:
                    return True, "title_body_option_conflict"

    return False, ""


def normalize_observed_shipping_type(
    shipping_type: str,
    shipping_fee: int = 0,
    shipping_condition: str = "",
) -> str:
    """배송 근거를 'free' | 'wow_free' | 'paid' | 'unknown'으로 엄격 정규화."""
    st = (shipping_type or "").strip().lower()
    fee = int(shipping_fee or 0)
    cond = (shipping_condition or "").strip()
    if st in ("free", "rocket_free") and fee == 0:
        return "free"
    if st == "paid" and fee > 0:
        return "paid"
    if st == "conditional" and fee == 0 and cond in ("와우 무료배송", "로켓와우 무료배송"):
        return "wow_free"
    return "unknown"


_PAREN_TOTAL_SUMMARY_RE = re.compile(
    rf"\(\s*(?:총|합계|전체|약)\s*(?:{_NUM_COMMAS_RE}(?:\.\d+)?)\s*(?:kg|g|ml|l|리터|킬로|{_ALL_COUNT_UNIT_ALT})\s*\)",
    re.IGNORECASE,
)
_DUP_PAREN_COUNT_RE = re.compile(
    rf"({_NUM_COMMAS_RE})\s*({_ALL_COUNT_UNIT_ALT})\s*\(\s*\1\s*({_ALL_COUNT_UNIT_ALT})\s*\)",
    re.IGNORECASE,
)
_WEIGHT_VOL_SPEC_RE = re.compile(
    rf"(?<!\d)(?<!\d,)(?<!\d\.)({_NUM_COMMAS_RE}(?:\.\d+)?)\s*(kg|g|l|ml|리터|킬로)(?![a-zA-Z])",
    re.IGNORECASE,
)
_PACK_MULTI_TOKEN_RE = re.compile(
    rf"(?:([x×*])\s*({_NUM_COMMAS_RE})(?:\s*({_ALL_COUNT_UNIT_ALT})(?![가-힣]))?"
    rf"|(?<!\d)(?<!\d,)(?<!\d\.)({_NUM_COMMAS_RE})\s*({_ALL_COUNT_UNIT_ALT})(?![가-힣]))",
    re.IGNORECASE,
)
_COUNT_ONLY_TOKEN_RE = re.compile(
    rf"(?<!\d)(?<!\d,)(?<!\d\.)({_NUM_COMMAS_RE})\s*({_ALL_COUNT_UNIT_ALT})(?![가-힣])",
    re.IGNORECASE,
)


def _fmt_spec_num(v: float) -> str:
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _unit_tier(u: str) -> int:
    ul = (u or "").lower()
    if ul in _COUNT_UNIT_TIER2:
        return 2
    if ul in _COUNT_UNIT_TIER1:
        return 1
    return 0


def _clean_title_for_unit_parse(title: str) -> Tuple[str, List[Tuple[int, int]]]:
    s = strip_purchase_limit_phrases(title or "")
    s = _PAREN_TOTAL_SUMMARY_RE.sub(" ", s)
    s = _DUP_PAREN_COUNT_RE.sub(r"\1\2", s)
    paren_spans = [(m.start(), m.end()) for m in re.finditer(r"\([^\)]*\)|\[[^\]]*\]", s)]
    spaced = re.sub(r"[\[\]\(\)]", " ", s)
    return spaced, paren_spans


def _in_any_span(pos: int, spans: List[Tuple[int, int]]) -> bool:
    return any(s <= pos < e for s, e in spans)


def compute_effective_unit_metrics(
    title: str,
    total_price: int,
    body_text: str = "",
) -> Dict[str, Any]:
    """제목의 단일 구성 중량/용량/수량과 총 결제금액(배송비 포함)으로 표준 단위가격 및 구성 시그니처 계산."""
    if not title or not isinstance(total_price, (int, float)) or total_price < 1000:
        return {}

    if classify_deal_category(title, body_text) == "it" or re.search(r"\d+\s*(?:gb|tb|mah|wh|hz)\b", title.lower()):
        return {}

    is_ambig, _ = has_ambiguous_option_or_price(title, body_text, sale_price=0)
    if is_ambig:
        return {}

    text, paren_spans = _clean_title_for_unit_parse(title)

    # 1) 중량/용량 스펙 탐색 — 서로 다른 규격이 2개 이상이면 복합 구성으로 간주해 단가 미산출
    specs: List[Tuple[str, float, re.Match]] = []
    seen_specs = set()
    for m in _WEIGHT_VOL_SPEC_RE.finditer(text):
        val = float(m.group(1).replace(",", ""))
        unit = m.group(2).lower()
        if unit in ("kg", "킬로"):
            fam, base_val = "wt", round(val * 1000.0, 3)
        elif unit == "g":
            fam, base_val = "wt", round(val, 3)
        elif unit in ("l", "리터"):
            fam, base_val = "vol", round(val * 1000.0, 3)
        else:
            fam, base_val = "vol", round(val, 3)
        if base_val <= 0:
            continue
        specs.append((fam, base_val, m))
        seen_specs.add((fam, base_val))

    if len(seen_specs) > 1:
        return {}

    if specs:
        fam, base_val, first_m = specs[0]
        tail_raw = text[first_m.end(): first_m.end() + 28]
        # 가격 토큰('9,900원') 이후는 수량 탐색 대상에서 제외
        m_price = re.search(r"\d[\d,]*\s*원", tail_raw)
        if m_price:
            tail_raw = tail_raw[: m_price.start()]

        tokens: List[Tuple[int, str, bool, bool]] = []
        for pm in _PACK_MULTI_TOKEN_RE.finditer(tail_raw):
            abs_pos = first_m.end() + pm.start()
            in_par = _in_any_span(abs_pos, paren_spans)
            if pm.group(1):
                n = int(pm.group(2).replace(",", ""))
                u = pm.group(3) or ""
                explicit_x = True
            else:
                n = int(pm.group(4).replace(",", ""))
                u = pm.group(5) or ""
                explicit_x = False
            if 1 <= n <= 5000:
                tokens.append((n, u, explicit_x, in_par))

        if len(tokens) > 2:
            # 괄호 안 동일 숫자 중복 토큰 정리 시도
            dedup: List[Tuple[int, str, bool, bool]] = []
            for tok in tokens:
                if tok[3] and any(d[0] == tok[0] for d in dedup):
                    continue
                dedup.append(tok)
            tokens = dedup
            if len(tokens) > 2:
                return {}

        mult = 1
        if len(tokens) == 1:
            mult = tokens[0][0]
        elif len(tokens) == 2:
            (n1, u1, _x1, _p1), (n2, u2, x2, p2) = tokens
            t1, t2 = _unit_tier(u1), _unit_tier(u2)
            if p2 and n2 == n1:
                mult = n1
            elif x2:
                mult = n1 * n2
            elif t1 == 1 and t2 == 2:
                mult = n1 * n2
            elif n1 == n2:
                mult = n1
            elif t1 == t2:
                return {}
            elif t1 == 2 and t2 == 1 and n2 == 1:
                mult = n1
            else:
                return {}

        total_spec = base_val * mult
        if fam == "vol":
            per_100ml = round(total_price * 100.0 / total_spec, 2)
            if total_spec >= 1000.0:
                per_1l = int(round(total_price * 1000.0 / total_spec))
                display_unit = f"1L당 {per_1l:,}원"
            else:
                display_unit = f"100ml당 {int(round(per_100ml)):,}원"
            return {
                "metric_type": "per_100ml",
                "metric_value": per_100ml,
                "per_100g": None,
                "per_100ml": per_100ml,
                "per_unit": None,
                "unit_count": mult if mult > 1 else None,
                "unit_label": "ml",
                "display_unit": display_unit,
                "config_sig": f"vol:{_fmt_spec_num(base_val)}ml:n{mult}",
            }

        per_100g = round(total_price * 100.0 / total_spec, 2)
        if total_spec >= 2000.0:
            per_1kg = int(round(total_price * 1000.0 / total_spec))
            display_unit = f"1kg당 {per_1kg:,}원"
        else:
            display_unit = f"100g당 {int(round(per_100g)):,}원"
        return {
            "metric_type": "per_100g",
            "metric_value": per_100g,
            "per_100g": per_100g,
            "per_100ml": None,
            "per_unit": None,
            "unit_count": mult if mult > 1 else None,
            "unit_label": "g",
            "display_unit": display_unit,
            "config_sig": f"wt:{_fmt_spec_num(base_val)}g:n{mult}",
        }

    # 2) 개수 기반 단위 (예: 30롤, 100매 10팩, 1,200포)
    cnt_tokens: List[Tuple[int, str, int, int, bool]] = []
    for cm in _COUNT_ONLY_TOKEN_RE.finditer(text):
        cnt = int(cm.group(1).replace(",", ""))
        lbl = cm.group(2)
        if 2 <= cnt <= 10000:
            cnt_tokens.append((cnt, lbl, cm.start(), cm.end(), _in_any_span(cm.start(), paren_spans)))

    if not cnt_tokens:
        return {}

    if len(cnt_tokens) > 2:
        dedup_c: List[Tuple[int, str, int, int, bool]] = []
        for tok in cnt_tokens:
            if tok[4] and any(d[0] == tok[0] for d in dedup_c):
                continue
            dedup_c.append(tok)
        cnt_tokens = dedup_c
        if len(cnt_tokens) > 2:
            return {}

    if len(cnt_tokens) == 1:
        total_cnt = cnt_tokens[0][0]
        raw_lbl = cnt_tokens[0][1]
    else:
        (n1, u1, _s1, e1, _p1), (n2, u2, s2, _e2, p2) = cnt_tokens
        t1, t2 = _unit_tier(u1), _unit_tier(u2)
        between = text[e1:s2]
        if p2 and n2 == n1:
            total_cnt, raw_lbl = n1, u1
        elif re.match(r"^\s*[x×*]\s*$", between):
            total_cnt, raw_lbl = n1 * n2, u1
        elif t1 == 1 and t2 == 2:
            total_cnt, raw_lbl = n1 * n2, u1
        elif n1 == n2:
            total_cnt, raw_lbl = n1, u1
        elif t1 == 2 and t2 == 1 and n2 == 1:
            total_cnt, raw_lbl = n1, u1
        else:
            return {}

    canon_lbl = _COUNT_UNIT_CANON.get(raw_lbl.lower(), raw_lbl)
    per_unit = round(total_price / total_cnt, 2)
    display_unit = f"1{canon_lbl}당 {int(round(per_unit)):,}원"
    return {
        "metric_type": f"per_{canon_lbl}",
        "metric_value": per_unit,
        "per_100g": None,
        "per_100ml": None,
        "per_unit": per_unit,
        "unit_count": total_cnt,
        "unit_label": canon_lbl,
        "display_unit": display_unit,
        "config_sig": f"cnt:{canon_lbl}:n{total_cnt}",
    }


def extract_config_signature(title: str, body_text: str = "") -> str:
    """제목/본문에서 단일 구성이 확정된 경우 정규화 구성 시그니처 반환."""
    res = compute_effective_unit_metrics(title, 10000, body_text=body_text)
    return str(res.get("config_sig") or "")


def is_comparable_option_key(option_key: str) -> bool:
    """옵션 ID(:vid:, :iid:) 또는 단일 구성(:cfg:)이 확정된 키인지 여부."""
    k = str(option_key or "")
    return (":vid:" in k) or (":iid:" in k) or (":cfg:" in k)


def build_option_key(
    product_id: str = "",
    item_id: str = "",
    vendor_item_id: str = "",
    title: str = "",
    url: str = "",
    body_text: str = "",
) -> str:
    """쿠팡 상품/옵션 식별자(vid > iid > 정규화 구성 시그니처 > 미확정) 기반 고유 키 생성."""
    pid = str(product_id or "").strip()
    iid = str(item_id or "").strip()
    vid = str(vendor_item_id or "").strip()

    if url:
        try:
            parsed = urlparse(url)
            if not pid:
                m_pid = re.search(r"/products/(\d+)", parsed.path or "")
                if m_pid:
                    pid = m_pid.group(1)
            qs = parse_qs(parsed.query or "")
            if not iid and qs.get("itemId"):
                iid = str(qs["itemId"][0]).strip()
            if not vid and qs.get("vendorItemId"):
                vid = str(qs["vendorItemId"][0]).strip()
        except Exception:
            pass

    if pid:
        if vid:
            return f"pid:{pid}:vid:{vid}"
        if iid:
            return f"pid:{pid}:iid:{iid}"
        cfg = extract_config_signature(title, body_text=body_text)
        if cfg:
            return f"pid:{pid}:cfg:{cfg}"
        return f"pid:{pid}:unspec"

    norm = re.sub(r"\s+", "", (title or url or "unknown").lower())
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]
    return f"title:{digest}"


def _now_kst(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(KST)
    if now.tzinfo is None:
        return now.replace(tzinfo=KST)
    return now.astimezone(KST)


class PriceHistoryDB:
    """옵션별 30일 가격 관측, 기발행 최저가, 대기열, 출처 쿨다운 SQLite 저장소."""

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS price_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    option_key TEXT NOT NULL,
                    product_id TEXT NOT NULL DEFAULT '',
                    obs_date TEXT NOT NULL,
                    shipping_type TEXT NOT NULL DEFAULT 'free',
                    total_price INTEGER NOT NULL,
                    sale_price INTEGER NOT NULL,
                    shipping_fee INTEGER NOT NULL DEFAULT 0,
                    orig_price INTEGER NOT NULL DEFAULT 0,
                    category TEXT NOT NULL DEFAULT 'general',
                    source TEXT NOT NULL DEFAULT '',
                    metric_type TEXT NOT NULL DEFAULT '',
                    metric_value REAL,
                    created_at TEXT NOT NULL,
                    UNIQUE(option_key, obs_date, shipping_type)
                );
                CREATE INDEX IF NOT EXISTS idx_obs_option_date
                    ON price_observations(option_key, obs_date DESC);

                CREATE TABLE IF NOT EXISTS published_prices (
                    option_key TEXT PRIMARY KEY,
                    product_id TEXT NOT NULL DEFAULT '',
                    total_price INTEGER NOT NULL,
                    sale_price INTEGER NOT NULL,
                    track TEXT NOT NULL DEFAULT 'A',
                    published_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_wait_deals (
                    wait_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    post_id TEXT NOT NULL,
                    option_key TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    reason TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    recheck_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS source_cooldowns (
                    source TEXT PRIMARY KEY,
                    status_code INTEGER NOT NULL,
                    cooldown_until TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tracked_watchlist (
                    option_key TEXT PRIMARY KEY,
                    product_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    search_query TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT 'general',
                    shipping_type TEXT NOT NULL DEFAULT 'wow_free',
                    source TEXT NOT NULL DEFAULT '',
                    priority INTEGER NOT NULL DEFAULT 0,
                    last_price INTEGER NOT NULL DEFAULT 0,
                    last_checked_at TEXT NOT NULL DEFAULT '',
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_watchlist_due
                    ON tracked_watchlist(priority DESC, last_checked_at ASC);
                """
            )

    def record_observation(
        self,
        option_key: str,
        product_id: str,
        total_price: int,
        sale_price: int,
        shipping_fee: int = 0,
        orig_price: int = 0,
        shipping_type: str = "unknown",
        category: str = "general",
        source: str = "",
        metric_type: str = "",
        metric_value: Optional[float] = None,
        obs_date: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> None:
        stype = (shipping_type or "").strip().lower()
        if not is_comparable_option_key(option_key) or stype not in ("free", "paid", "wow_free"):
            return
        if total_price <= 0 or sale_price <= 0:
            return
        dt = _now_kst(now)
        day_str = obs_date or dt.strftime("%Y-%m-%d")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO price_observations (
                    option_key, product_id, obs_date, shipping_type,
                    total_price, sale_price, shipping_fee, orig_price,
                    category, source, metric_type, metric_value, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(option_key, obs_date, shipping_type) DO UPDATE SET
                    total_price = MIN(price_observations.total_price, excluded.total_price),
                    sale_price = CASE
                        WHEN excluded.total_price < price_observations.total_price THEN excluded.sale_price
                        ELSE price_observations.sale_price
                    END,
                    shipping_fee = CASE
                        WHEN excluded.total_price < price_observations.total_price THEN excluded.shipping_fee
                        ELSE price_observations.shipping_fee
                    END,
                    orig_price = MAX(price_observations.orig_price, excluded.orig_price),
                    metric_type = CASE
                        WHEN excluded.metric_type != '' THEN excluded.metric_type
                        ELSE price_observations.metric_type
                    END,
                    metric_value = CASE
                        WHEN excluded.total_price < price_observations.total_price AND excluded.metric_value IS NOT NULL THEN excluded.metric_value
                        ELSE COALESCE(price_observations.metric_value, excluded.metric_value)
                    END
                """,
                (
                    option_key,
                    str(product_id or ""),
                    day_str,
                    stype,
                    int(total_price),
                    int(sale_price),
                    int(shipping_fee or 0),
                    int(orig_price or 0),
                    category or "general",
                    source or "",
                    metric_type or "",
                    metric_value,
                    dt.isoformat(),
                ),
            )

    def get_observed_baseline(
        self,
        option_key: str,
        shipping_type: Optional[str] = None,
        days: int = 30,
        min_days: int = 5,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        stype = (shipping_type or "").strip().lower()
        if not is_comparable_option_key(option_key) or stype not in ("free", "paid", "wow_free"):
            return None
        dt = _now_kst(now)
        since_date = (dt - timedelta(days=days)).strftime("%Y-%m-%d")

        query = """
            SELECT obs_date, MIN(total_price) AS day_low
            FROM price_observations
            WHERE option_key = ? AND obs_date >= ? AND shipping_type = ?
            GROUP BY obs_date ORDER BY obs_date DESC
        """
        params: List[Any] = [option_key, since_date, stype]

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        distinct_days = len(rows)
        if distinct_days < min_days:
            return None

        prices = [int(r["day_low"]) for r in rows if int(r["day_low"]) > 0]
        if len(prices) < min_days:
            return None

        med = int(round(statistics.median(prices)))
        return {
            "median_price": med,
            "distinct_days": len(prices),
            "min_price": min(prices),
            "max_price": max(prices),
        }

    def get_prior_price_stats(
        self,
        option_key: str,
        shipping_type: Optional[str] = None,
        days: int = 30,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """오늘 이전(obs_date < today) 동일 옵션·배송 조건의 직전 관측가·중앙값 통계 반환."""
        stype = (shipping_type or "").strip().lower()
        if not is_comparable_option_key(option_key) or stype not in ("free", "paid", "wow_free"):
            return None
        dt = _now_kst(now)
        today_date = dt.strftime("%Y-%m-%d")
        since_date = (dt - timedelta(days=days)).strftime("%Y-%m-%d")

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT obs_date, MIN(total_price) AS day_low
                FROM price_observations
                WHERE option_key = ? AND obs_date >= ? AND obs_date < ? AND shipping_type = ?
                GROUP BY obs_date
                ORDER BY obs_date DESC
                """,
                (option_key, since_date, today_date, stype),
            ).fetchall()

        valid_rows = [(str(r["obs_date"]), int(r["day_low"])) for r in rows if int(r["day_low"]) > 0]
        if not valid_rows:
            return None
        prices = [p for _, p in valid_rows]
        return {
            "prev_date": valid_rows[0][0],
            "prev_price": valid_rows[0][1],
            "median_price": int(round(statistics.median(prices))),
            "min_price": min(prices),
            "distinct_days": len(prices),
        }

    def upsert_tracked_item(
        self,
        option_key: str,
        product_id: str,
        title: str,
        search_query: str,
        *,
        category: str = "general",
        shipping_type: str = "wow_free",
        source: str = "",
        priority: int = 0,
        last_price: int = 0,
        ttl_days: int = 14,
        max_items: int = 40,
        now: Optional[datetime] = None,
    ) -> bool:
        """랭킹 밖 재조회 대상 옵션(최대 max_items개, TTL 14일)을 추적 목록에 등록/갱신."""
        ok_key = str(option_key or "").strip()
        q = " ".join(str(search_query or "").split()).strip()
        if not is_comparable_option_key(ok_key) or not q:
            return False
        dt = _now_kst(now)
        exp_iso = (dt + timedelta(days=max(1, int(ttl_days or 14)))).isoformat()
        now_iso = dt.isoformat()
        stype = (shipping_type or "wow_free").strip().lower()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tracked_watchlist (
                    option_key, product_id, title, search_query, category,
                    shipping_type, source, priority, last_price, last_checked_at,
                    expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                ON CONFLICT(option_key) DO UPDATE SET
                    product_id = CASE WHEN excluded.product_id != '' THEN excluded.product_id ELSE tracked_watchlist.product_id END,
                    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE tracked_watchlist.title END,
                    search_query = excluded.search_query,
                    category = excluded.category,
                    shipping_type = excluded.shipping_type,
                    source = CASE WHEN excluded.source != '' THEN excluded.source ELSE tracked_watchlist.source END,
                    priority = MAX(tracked_watchlist.priority, excluded.priority),
                    last_price = CASE WHEN excluded.last_price > 0 THEN excluded.last_price ELSE tracked_watchlist.last_price END,
                    expires_at = excluded.expires_at
                """,
                (
                    ok_key,
                    str(product_id or ""),
                    str(title or "")[:120],
                    q[:60],
                    str(category or "general"),
                    stype,
                    str(source or ""),
                    int(priority or 0),
                    max(0, int(last_price or 0)),
                    exp_iso,
                    now_iso,
                ),
            )
            conn.execute("DELETE FROM tracked_watchlist WHERE expires_at <= ?", (now_iso,))
            cap = max(1, int(max_items or 40))
            cnt_row = conn.execute("SELECT COUNT(*) AS c FROM tracked_watchlist").fetchone()
            total = int(cnt_row["c"] if cnt_row else 0)
            if total > cap:
                excess = total - cap
                conn.execute(
                    """
                    DELETE FROM tracked_watchlist
                    WHERE option_key IN (
                        SELECT option_key FROM tracked_watchlist
                        ORDER BY priority ASC, created_at ASC
                        LIMIT ?
                    )
                    """,
                    (excess,),
                )
        return True

    def list_due_tracked_items(
        self,
        limit: int = 6,
        min_recheck_hours: int = 3,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """재조회 주기가 지난 활성 추적 항목을 우선순위 순으로 반환."""
        dt = _now_kst(now)
        now_iso = dt.isoformat()
        stale_iso = (dt - timedelta(hours=max(1, int(min_recheck_hours or 3)))).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tracked_watchlist
                WHERE expires_at > ?
                  AND (last_checked_at = '' OR last_checked_at <= ?)
                ORDER BY priority DESC, last_checked_at ASC
                LIMIT ?
                """,
                (now_iso, stale_iso, max(1, int(limit or 6))),
            ).fetchall()
        return [dict(r) for r in rows]

    def touch_tracked_item(
        self,
        option_key: str,
        last_price: int = 0,
        now: Optional[datetime] = None,
    ) -> None:
        if not option_key:
            return
        dt = _now_kst(now)
        now_iso = dt.isoformat()
        with self._connect() as conn:
            if int(last_price or 0) > 0:
                conn.execute(
                    "UPDATE tracked_watchlist SET last_checked_at = ?, last_price = ? WHERE option_key = ?",
                    (now_iso, int(last_price), option_key),
                )
            else:
                conn.execute(
                    "UPDATE tracked_watchlist SET last_checked_at = ? WHERE option_key = ?",
                    (now_iso, option_key),
                )

    def record_published(
        self,
        option_key: str,
        product_id: str,
        total_price: int,
        sale_price: int,
        track: str = "A",
        now: Optional[datetime] = None,
    ) -> None:
        if not option_key or total_price <= 0:
            return
        dt = _now_kst(now)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO published_prices (option_key, product_id, total_price, sale_price, track, published_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(option_key) DO UPDATE SET
                    product_id = excluded.product_id,
                    total_price = excluded.total_price,
                    sale_price = excluded.sale_price,
                    track = excluded.track,
                    published_at = excluded.published_at
                """,
                (option_key, str(product_id or ""), int(total_price), int(sale_price), track, dt.isoformat()),
            )

    def get_last_published(self, option_key: str) -> Optional[Dict[str, Any]]:
        if not option_key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM published_prices WHERE option_key = ?",
                (option_key,),
            ).fetchone()
        return dict(row) if row else None

    def should_reenter_price_drop(
        self,
        option_key: str,
        current_total_price: int,
        drop_pct: float = 5.0,
        window_days: int = 3,
        now: Optional[datetime] = None,
    ) -> Tuple[bool, float]:
        if not option_key or current_total_price <= 0:
            return False, 0.0
        row = self.get_last_published(option_key)
        if not row:
            return False, 0.0
        prev_total = int(row.get("total_price", 0) or 0)
        if prev_total <= 0 or current_total_price >= prev_total:
            return False, 0.0
        try:
            pub_dt = datetime.fromisoformat(row["published_at"])
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=KST)
        except Exception:
            return False, 0.0

        dt = _now_kst(now)
        if (dt - pub_dt).total_seconds() > window_days * 86400:
            return False, 0.0

        actual_drop = (prev_total - current_total_price) * 100.0 / prev_total
        if actual_drop >= drop_pct:
            return True, round(actual_drop, 1)
        return False, round(actual_drop, 1)

    def get_pending_wait(self, wait_key: str) -> Optional[Dict[str, Any]]:
        if not wait_key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_wait_deals WHERE wait_key = ?",
                (wait_key,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_pending_wait(
        self,
        wait_key: str,
        source: str,
        post_id: str,
        option_key: str,
        url: str,
        title: str,
        category: str,
        reason: str,
        max_wait_minutes: int = 120,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        dt = _now_kst(now)
        expires_dt = dt + timedelta(minutes=max_wait_minutes)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM pending_wait_deals WHERE wait_key = ?",
                (wait_key,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE pending_wait_deals
                    SET option_key = ?, title = ?, category = ?, reason = ?,
                        last_checked_at = ?, recheck_count = recheck_count + 1
                    WHERE wait_key = ?
                    """,
                    (option_key, title, category, reason, dt.isoformat(), wait_key),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO pending_wait_deals (
                        wait_key, source, post_id, option_key, url, title,
                        category, reason, first_seen_at, last_checked_at, expires_at, recheck_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        wait_key, source, str(post_id), option_key, url, title,
                        category, reason, dt.isoformat(), dt.isoformat(), expires_dt.isoformat(),
                    ),
                )
            row = conn.execute(
                "SELECT * FROM pending_wait_deals WHERE wait_key = ?",
                (wait_key,),
            ).fetchone()
        return dict(row) if row else {}

    def should_skip_wait_recheck(
        self,
        wait_key: str,
        recheck_min_minutes: int = 8,
        now: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        row = self.get_pending_wait(wait_key)
        if not row:
            return False, ""
        dt = _now_kst(now)
        try:
            exp_dt = datetime.fromisoformat(row["expires_at"])
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=KST)
            if dt >= exp_dt:
                return True, "expired"
        except Exception:
            pass
        try:
            last_dt = datetime.fromisoformat(row["last_checked_at"])
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=KST)
            elapsed = (dt - last_dt).total_seconds()
            if elapsed < recheck_min_minutes * 60:
                return True, "cooldown"
        except Exception:
            pass
        return False, ""

    def delete_pending_wait(self, wait_key: str) -> None:
        if not wait_key:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_wait_deals WHERE wait_key = ?", (wait_key,))

    def pop_expired_waits(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        dt = _now_kst(now)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_wait_deals WHERE expires_at <= ?",
                (dt.isoformat(),),
            ).fetchall()
            if rows:
                conn.execute(
                    "DELETE FROM pending_wait_deals WHERE expires_at <= ?",
                    (dt.isoformat(),),
                )
            return [dict(r) for r in rows]

    def cleanup_expired_waits(self, now: Optional[datetime] = None) -> int:
        return len(self.pop_expired_waits(now=now))

    def set_source_cooldown(
        self,
        source: str,
        status_code: int,
        cooldown_minutes: int,
        reason: str = "",
        now: Optional[datetime] = None,
    ) -> None:
        if not source:
            return
        dt = _now_kst(now)
        until = dt + timedelta(minutes=cooldown_minutes)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO source_cooldowns (source, status_code, cooldown_until, reason, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    status_code = excluded.status_code,
                    cooldown_until = excluded.cooldown_until,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (source, int(status_code), until.isoformat(), reason or "", dt.isoformat()),
            )

    def get_source_cooldown(
        self,
        source: str,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        if not source:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM source_cooldowns WHERE source = ?",
                (source,),
            ).fetchone()
        if not row:
            return None
        dt = _now_kst(now)
        try:
            until_dt = datetime.fromisoformat(row["cooldown_until"])
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=KST)
            if dt < until_dt:
                return dict(row)
        except Exception:
            pass
        return None

    def clear_source_cooldown(self, source: str) -> None:
        if not source:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM source_cooldowns WHERE source = ?", (source,))

    def prune_old_records(self, days: int = 120, now: Optional[datetime] = None) -> Dict[str, int]:
        """오래된 관측 가격·게시 이력·만료된 출처 쿨다운을 정리해 DB 비대화를 방지한다."""
        dt = _now_kst(now)
        keep_days = max(30, int(days or 120))
        cutoff_dt = dt - timedelta(days=keep_days)
        cutoff_date = cutoff_dt.strftime("%Y-%m-%d")
        cutoff_iso = cutoff_dt.isoformat()
        with self._connect() as conn:
            c1 = conn.execute("DELETE FROM price_observations WHERE obs_date < ?", (cutoff_date,)).rowcount
            c2 = conn.execute("DELETE FROM published_prices WHERE published_at < ?", (cutoff_iso,)).rowcount
            c3 = conn.execute("DELETE FROM source_cooldowns WHERE cooldown_until <= ?", (dt.isoformat(),)).rowcount
            c4 = conn.execute("DELETE FROM tracked_watchlist WHERE expires_at <= ?", (dt.isoformat(),)).rowcount
        return {
            "observations": max(0, int(c1 or 0)),
            "published": max(0, int(c2 or 0)),
            "cooldowns": max(0, int(c3 or 0)),
            "watchlist": max(0, int(c4 or 0)),
        }
