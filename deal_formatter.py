import re
from typing import Optional, Dict, Any

def extract_spec_hints(name: str) -> Optional[str]:
    """상품명에서 수량, 용량, 규격 힌트 추출"""
    patterns = [
        r'(\d+[\s]*(?:매|개|팩|입|캔|병|포|세트|box|박스|롤|봉|단|통|구|줄|p|P|ea|EA))',
        r'(\d+[\s]*(?:ml|ML|l|L|g|G|kg|KG|리터|그램|킬로))',
        r'(\d+[\s]*(?:cm|CM|mm|MM|인치|형|호))',
    ]
    found = []
    for pat in patterns:
        matches = re.findall(pat, name, re.IGNORECASE)
        for m in matches:
            if m not in found:
                found.append(m.strip())
    if found:
        return ", ".join(found[:3])
    return None

def format_rich_deal_message(item: Dict[str, Any], badge: str = "🍯 [오늘의 핫특가]") -> str:
    """풍성한 상품 정보를 담은 텔레그램 메시지 생성"""
    name = item.get("productName", "")
    price = int(item.get("productPrice", 0))
    cat = item.get("categoryName")
    rank = item.get("rank")
    is_rocket = item.get("isRocket", False)
    is_free_ship = item.get("isFreeShipping", False)

    # 1. 상단 타이틀 & 랭킹/카테고리
    header_parts = [badge]
    if rank:
        header_parts.append(f"🏆 랭킹 {rank}위")
    if cat:
        header_parts.append(f"📂 {cat}")
    header_str = " | ".join(header_parts)

    # 2. 수량/규격 힌트 추출
    spec_hint = extract_spec_hints(name)
    spec_line = f"📦 **구성/규격**: `{spec_hint}`\n" if spec_hint else ""

    # 3. 배송 정보
    shipping_badges = []
    if is_rocket:
        shipping_badges.append("🚀 **로켓배송** (내일 도착 보장)")
    if is_free_ship:
        shipping_badges.append("📦 **무료배송**")
    if not shipping_badges:
        shipping_badges.append("🚚 **일반배송**")
    shipping_str = " + ".join(shipping_badges)

    msg = (
        f"{header_str}\n\n"
        f"📌 **{name}**\n\n"
        f"{spec_line}"
        f"💰 **특가가격**: **{price:,}원**\n"
        f"🚚 **배송혜택**: {shipping_str}\n"
        f"⏰ **한정특가**: 오늘 자정(24:00)까지 또는 한정수량 마감\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💡 *정확한 옵션별 잔여수량과 추가 쿠폰은 아래 링크에서 확인하세요.*\n"
        f"※ 이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
    )
    return msg
