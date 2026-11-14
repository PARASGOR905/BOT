import requests
import re
import json

url = "https://www.chorki.net/series/myself-allen-swapan"
headers = {"User-Agent": "Mozilla/5.0"}
r = requests.get(url, headers=headers)
html_text = r.text

chunks = []
for m in re.finditer(r'self\.__next_f\.push\(\[\s*\d+\s*,\s*"(.*?)"\s*\]\)', html_text):
    escaped_str = m.group(1)
    try:
        decoded = escaped_str.encode("latin-1", "backslashreplace").decode("unicode-escape")
        chunks.append(decoded)
    except Exception:
        decoded = escaped_str.replace('\\"', '"').replace('\\\\', '\\')
        chunks.append(decoded)
concat = "".join(chunks)

print("Searching for m3u8 in series flight data...")
all_m3u8 = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', concat)
print(f"Total m3u8 URLs found: {len(all_m3u8)}")
for u in set(all_m3u8):
    print(" -", u)

print("\nSearching for episode or video JSONs...")
for m in re.finditer(r'\{[^{}]*?"title"[^{}]*?\}', concat):
    raw = m.group(0)
    if "episode" in raw.lower() or "season" in raw.lower() or "swapan" in raw.lower():
        print("JSON snippet:", raw[:300])

