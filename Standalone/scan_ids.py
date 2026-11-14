import urllib.request
import urllib.parse
import json

headers = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest"
}

for item_id in range(28370, 28385):
    url = "https://www.wavespb.com/web/dashboard/ajax_data_details"
    data = urllib.parse.urlencode({"id": str(item_id)}).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            res = json.loads(resp.read().decode('utf-8'))
            if res.get('status') and res.get('data'):
                d = res['data']
                print(f"ID {item_id}: Title='{d.get('title')}', CatTitle='{d.get('category_title')}', CatType='{d.get('category_type')}', Type='{d.get('type')}', Seasons={d.get('season_count')}")
    except Exception:
        pass
