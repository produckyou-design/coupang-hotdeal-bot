import requests
import re
import sys
import io
from bs4 import BeautifulSoup

sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8')

headers = {'User-Agent': 'Mozilla/5.0'}
url = 'https://bbs.ruliweb.com/market/board/1020/read/108668'
r = requests.get(url, headers=headers, timeout=5)
soup = BeautifulSoup(r.text, 'html.parser')

# 본문 또는 원본 출처 링크 추출
source_link = soup.select_one('div.source_url a')
if source_link:
    print('Source URL:', source_link['href'])

# 본문 텍스트
view_content = soup.select_one('div.view_content')
if view_content:
    print('Content text snippet:', view_content.text.strip()[:200])

cp_links = re.findall(r'https?://[^\s"\'<>]+coupang\.com[^\s"\'<>]+', r.text)
print('Found coupang links:', set(cp_links))
