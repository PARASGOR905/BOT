import urllib.request
import re

urls = [
    "https://www.wavespb.com/assets/js/sw_production.js",
    "https://www.wavespb.com/assets/js/annotator-full-bsdfj3454nmvbvn3.js"
]

for url in urls:
    print("=== Fetching:", url)
    try:
        content = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})).read().decode('utf-8')
        print("Length:", len(content))
        matches = re.findall(r'https?://[^\s"\'<>]+', content)
        for m in set(matches):
            if any(k in m.lower() for k in ['api', 'waves', 'prod', 'pb', 'master', 'content', 'home', 'json']):
                print(" ", m[:120])
    except Exception as e:
        print("Error:", e)
