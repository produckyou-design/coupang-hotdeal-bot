import json
import sys
import io

# Windows 콘솔 utf-8 출력 설정
sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding='utf-8', errors='replace')

from coupang_api import CoupangPartnersAPI

def main():
    try:
        api = CoupangPartnersAPI()
        print("🔑 쿠팡 파트너스 API 키 로드 성공!")
        print("📡 골드박스(오늘의 특가) API 테스트 호출 중...")
        
        res = api.get_goldbox_products()
        print(f"응답 코드: {res.get('rCode', 'N/A')}")
        print(f"메시지: {res.get('rMessage', 'N/A')}")
        
        data = res.get("data", [])
        print(f"조회된 상품 수: {len(data)}개")
        if data:
            print("\n[첫 번째 상품 예시]")
            first = data[0]
            print(f"- 상품명: {first.get('productName')}")
            print(f"- 가격: {first.get('productPrice')}원")
            print(f"- 링크: {first.get('productUrl')}")
        else:
            print(json.dumps(res, indent=2, ensure_ascii=False))
            
    except ValueError as e:
        print(f"❌ 설정 오류: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ 호출 실패: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
