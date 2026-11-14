import urllib.request
import re

url = "https://www.wavespb.com/assets/js/cache.js"
headers = {"User-Agent": "Mozilla/5.0"}
content = urllib.request.urlopen(urllib.request.Request(url, headers=headers)).read().decode('utf-8')

print("--- Searching base_url + ---")
matches = re.findall(r'base_url\s*\+\s*["\']([^"\']+)["\']', content)
for m in set(matches):
    print("  ", m)

print("\n--- Searching $.ajax urls ---")
ajax_urls = re.findall(r'url\s*:\s*(?:base_url\s*\+\s*)?["\']([^"\']+)["\']', content)
for a in set(ajax_urls):
    print("  ", a)
