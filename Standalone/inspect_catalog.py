import urllib.request
import urllib.parse
import json

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest"
}

# Test fetching master content / flat list / nav data
endpoints = [
    ("https://www.wavespb.com/home/getFlatListData", {"page": 1}),
    ("https://www.wavespb.com/web/dashboard/get_navbar_data", {}),
    ("https://www.wavespb.com/web/dashboard/ajax_data_details", {"id": "28378"})
]

for ep_url, payload in endpoints:
    print(f"\n=== Testing {ep_url} ===")
    try:
        data = urllib.parse.urlencode(payload).encode('utf-8')
        req = urllib.request.Request(ep_url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode('utf-8')
            print("Response length:", len(text))
            try:
                js = json.loads(text)
                print("JSON keys:", js.keys() if isinstance(js, dict) else f"List len {len(js)}")
                if isinstance(js, dict):
                    for k in list(js.keys())[:5]:
                        val = js[k]
                        print(f"  {k}: {str(val)[:150]}")
            except Exception:
                print("Text snippet:", text[:200])
    except Exception as e:
        print("Error:", e)
