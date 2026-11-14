import requests
import json
import re

url = "https://www.chorki.net/movie/the-university-of-chankharpul"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Origin": "https://www.chorki.net",
    "Referer": "https://www.chorki.net/"
}

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

concat_flight = "".join(chunks)

print("Concat flight length:", len(concat_flight))

# Search for m3u8 or video objects
for m in re.finditer(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', concat_flight):
    print("Found m3u8 in flight:", m.group(0)[:150])

# Search for title in flight
for m in re.finditer(r'"title"\s*:\s*"([^"]+)"', concat_flight):
    print("Found title:", m.group(1))

