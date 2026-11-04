import requests
import re
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8')

headers = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1',
    'Accept-Language': 'ko-KR,ko;q=0.9',
}
r = requests.get('https://m.coupang.com/vm/products/8548508229', headers=headers, timeout=5)
print('Status:', r.status_code)
og_title = re.search(r'property="og:title" content="([^"]+)"', r.text)
og_desc = re.search(r'property="og:description" content="([^"]+)"', r.text)
if og_title:
    print('OG Title:', og_title.group(1))
if og_desc:
    print('OG Desc:', og_desc.group(1))
