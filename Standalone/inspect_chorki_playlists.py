import requests
import re
import json
import m3u8

url = "https://www.chorki.net/movie/the-university-of-chankharpul"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Origin": "https://www.chorki.net",
    "Referer": "https://www.chorki.net/"
}

r = requests.get(url, headers=headers)
html_text = r.text

print("=== Scanning Next.js flight data ===")
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

# Search for all URLs with .m3u8
m3u8_matches = list(set(re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', concat_flight)))
print(f"Found {len(m3u8_matches)} unique m3u8 URLs in flight data:")

for u in m3u8_matches:
    u_clean = u.rstrip('\\/\"\', \t\r\n')
    try:
        mr = requests.get(u_clean, headers=headers, timeout=5)
        if mr.ok:
            playlist = m3u8.loads(mr.text)
            if playlist.is_variant:
                print(f"\n[VARIANT MASTER] {u_clean}")
                for p in playlist.playlists:
                    print(f"   Bandwidth: {p.stream_info.bandwidth}, Res: {p.stream_info.resolution}, Codecs: {p.stream_info.codecs}")
                    # Check target playlist segment count / duration
                    var_url = p.uri if p.uri.startswith('http') else requests.compat.urljoin(u_clean, p.uri)
                    vr = requests.get(var_url, headers=headers, timeout=5)
                    if vr.ok:
                        vp = m3u8.loads(vr.text)
                        dur = sum(s.duration for s in vp.segments)
                        print(f"      Segments: {len(vp.segments)}, Total Duration: {dur:.1f} sec ({dur/60:.1f} min)")
                        break
            else:
                dur = sum(s.duration for s in playlist.segments)
                print(f"[MEDIA PLAYLIST] Segments: {len(playlist.segments)}, Duration: {dur:.1f} s ({dur/60:.1f} min) | {u_clean}")
    except Exception as e:
        print(f"[ERROR] {u_clean}: {e}")
