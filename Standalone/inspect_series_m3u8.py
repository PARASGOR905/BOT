import requests
import re
import m3u8

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

all_m3u8 = set(re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', concat))

for u in all_m3u8:
    u_clean = u.rstrip('\\/\"\', \t\r\n')
    try:
        mr = requests.get(u_clean, headers=headers, timeout=5)
        if mr.ok:
            playlist = m3u8.loads(mr.text)
            if playlist.is_variant:
                # check first variant
                p = playlist.playlists[0]
                var_url = p.uri if p.uri.startswith('http') else requests.compat.urljoin(u_clean, p.uri)
                vr = requests.get(var_url, headers=headers, timeout=5)
                if vr.ok:
                    vp = m3u8.loads(vr.text)
                    dur = sum(s.duration for s in vp.segments)
                    print(f"URL: {u_clean} | Dur: {dur:.1f} sec ({dur/60:.1f} min) | Segs: {len(vp.segments)}")
            else:
                dur = sum(s.duration for s in playlist.segments)
                print(f"URL: {u_clean} | Dur: {dur:.1f} sec ({dur/60:.1f} min) | Segs: {len(playlist.segments)}")
    except Exception as e:
        print(f"Error {u_clean}: {e}")
