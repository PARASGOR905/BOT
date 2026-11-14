import urllib.request
import urllib.parse
import json

url = "https://www.wavespb.com/web/dashboard/ajax_data_details"
data = urllib.parse.urlencode({"id": "28378"}).encode('utf-8')
headers = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest"
}

req = urllib.request.Request(url, data=data, headers=headers)
with urllib.request.urlopen(req) as resp:
    res = json.loads(resp.read().decode('utf-8'))
    m = res['data']
    print("category_id:", m.get('category_id'))
    print("category_title:", m.get('category_title'))
    print("category_type:", m.get('category_type'))
    print("type:", m.get('type'))
    print("content_type:", m.get('content_type'))
    print("layout_type:", m.get('layout_type'))
    print("season:", m.get('season'))
    print("season_count:", m.get('season_count'))
