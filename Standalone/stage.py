import sys
import re
import os
import random
import string
import json
import requests

_DIALECT_CODE_TO_NAME = {
    "mar": "marathi", "hin": "hindi", "bho": "bhojpuri", "pun": "punjabi",
    "har": "haryanvi", "raj": "rajasthani", "ben": "bengali", "tam": "tamil",
    "tel": "telugu", "guj": "gujarati", "kan": "kannada", "mal": "malayalam",
    "ori": "odia", "en": "english",
}

def init_ansi():
    """Enables ANSI escape sequence parsing on Windows consoles."""
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            hOut = kernel32.GetStdHandle(-11)
            dwMode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(hOut, ctypes.byref(dwMode)):
                kernel32.SetConsoleMode(hOut, dwMode.value | 0x0004)
        except Exception:
            pass

def load_cookies():
    cookie_paths = [
        r"d:\temp\vinetrimmer\cookies\stagein\default.txt",
        os.path.join(os.path.dirname(__file__), "vinetrimmer", "cookies", "stagein", "default.txt"),
        os.path.join(os.path.dirname(__file__), "..", "vinetrimmer", "cookies", "stagein", "default.txt"),
    ]
    cookies = {}
    for cp in cookie_paths:
        if os.path.isfile(cp):
            try:
                from http.cookiejar import MozillaCookieJar
                cj = MozillaCookieJar(cp)
                cj.load(ignore_discard=True, ignore_expires=True)
                for c in cj:
                    cookies[c.name] = c.value
                if cookies:
                    break
            except Exception:
                pass
    return cookies

def fetch_signed_hls_links(watch_path, cookies):
    if not watch_path:
        return {}
    rsc_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    url = f"https://www.stage.in{watch_path}?_rsc={rsc_id}"
    try:
        session = requests.Session()
        if cookies:
            session.cookies.update(cookies)
        r = session.get(url, headers={"rsc": "1", "Accept": "*/*", "next-url": "/"}, timeout=10)
        key = '"signedURLDetails":'
        idx = r.text.find(key)
        if idx != -1:
            start = r.text.index("{", idx + len(key))
            obj, _ = json.JSONDecoder().raw_decode(r.text, start)
            
            h264_url = (obj.get("signedObject") or {}).get("signedUrl") or obj.get("signedUrl")
            h265_url = (obj.get("signedObjectH265") or {}).get("signedUrl") or obj.get("signedUrlH265")
            av1_url = (
                (obj.get("signedObjectAv1") or obj.get("signedObjectAV1") or {}).get("signedUrl") or 
                obj.get("signedUrlAv1") or obj.get("signedUrlAV1")
            )
            
            res = {}
            if h264_url:
                res["H264 (HLS m3u8)"] = h264_url
            if h265_url:
                res["H265 (HLS m3u8)"] = h265_url
            if av1_url:
                res["AV1 (HLS m3u8)"] = av1_url
            return res
    except Exception:
        pass
    return {}

def build_url_slug(api_slug, content_id):
    if not api_slug:
        return str(content_id)
    base = api_slug.rsplit("-", 1)[0] if "-" in api_slug else api_slug
    return f"{base}-{content_id}"

def main():
    init_ansi()
    
    use_color = sys.stdout.isatty()
    COLOR_RESET = "\033[0m" if use_color else ""
    COLOR_BOLD = "\033[1m" if use_color else ""
    COLOR_CYAN = "\033[96m" if use_color else ""
    COLOR_GREEN = "\033[92m" if use_color else ""
    COLOR_YELLOW = "\033[93m" if use_color else ""
    COLOR_RED = "\033[91m" if use_color else ""
    COLOR_GRAY = "\033[90m" if use_color else ""
    COLOR_WHITE = "\033[97m" if use_color else ""
    
    if len(sys.argv) >= 2:
        input_str = sys.argv[1]
    else:
        try:
            prompt_str = f"{COLOR_BOLD}{COLOR_CYAN}Enter Movie or Show ID/URL: {COLOR_RESET}"
            input_str = input(prompt_str).strip()
        except (KeyboardInterrupt, EOFError):
            print()
            sys.exit(0)

    if not input_str:
        print(f"{COLOR_RED}Error: No input provided.{COLOR_RESET}", file=sys.stderr)
        sys.exit(1)

    match = re.search(r'(\d+)(?:/|\?|$)', input_str)
    if match:
        content_id = match.group(1)
    else:
        cleaned = "".join(filter(str.isdigit, input_str))
        if cleaned:
            content_id = cleaned
        else:
            print(f"{COLOR_RED}Error: Could not extract a valid numeric ID from input: '{input_str}'{COLOR_RESET}", file=sys.stderr)
            sys.exit(1)

    dialect_map = {
        'haryanvi': 'har', 'har': 'har',
        'rajasthani': 'raj', 'raj': 'raj',
        'bhojpuri': 'bho', 'bho': 'bho',
        'gujarati': 'guj', 'guj': 'guj',
        'marathi': 'mar', 'mar': 'mar',
        'bengali': 'ben', 'ben': 'ben',
        'hindi': 'hin', 'hin': 'hin',
        'punjabi': 'pun', 'pun': 'pun'
    }

    if len(sys.argv) > 2:
        d_arg = sys.argv[2].lower()
        dialect = dialect_map.get(d_arg, d_arg)
    else:
        dialect = "har"
        input_lower = input_str.lower()
        for d_key, d_code in dialect_map.items():
            if d_key in input_lower:
                dialect = d_code
                break

    headers = {
        "app_id": "in.stage.web",
        "platform": "web",
        "os": "android",
        "api-version": "2",
        "dialect": dialect,
        "lang": "en"
    }

    cookies = load_cookies()

    # 1. Try Movie Details API
    movie_url = "https://stageapi.stage.in/nest/contents/movies/details"
    try:
        resp = requests.get(movie_url, params={"movieId": content_id}, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            movie = data.get("movie", {})
            if isinstance(movie, dict) and (movie.get("_id") or movie.get("id")):
                source_link = movie.get("sourceLink") or movie.get("selectedPeripheral", {}).get("sourceLink")
                if source_link and source_link.strip():
                    lang_code = movie.get("language") or dialect
                    dialect_name = _DIALECT_CODE_TO_NAME.get(lang_code, "marathi")
                    url_slug = build_url_slug(movie.get("slug") or "", content_id)
                    watch_path = f"/en/{dialect_name}/movie/watch/{url_slug}"
                    
                    hls_links = fetch_signed_hls_links(watch_path, cookies)
                    
                    print(f"\n{COLOR_BOLD}{COLOR_YELLOW}Movie: {COLOR_RESET}{COLOR_BOLD}{movie.get('title')}{COLOR_RESET}")
                    
                    if hls_links:
                        for codec_label, hls_url in hls_links.items():
                            print(f"{COLOR_BOLD}{COLOR_GREEN}{codec_label}: {COLOR_RESET}{COLOR_WHITE}{hls_url}{COLOR_RESET}")
                    
                    raw_link = f"https://media.stage.in/episode/main-video/{source_link.strip()}"
                    print(f"{COLOR_BOLD}{COLOR_CYAN}Raw Link (MP4): {COLOR_RESET}{COLOR_GRAY}{raw_link}{COLOR_RESET}")
                    sys.exit(0)
    except Exception:
        pass

    # 2. Try Show Details API
    show_url = "https://stageapi.stage.in/nest/contents/shows/details"
    try:
        resp = requests.get(show_url, params={"showId": content_id, "seasonId": ""}, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            show_data = data.get("data", {})
            if isinstance(show_data, dict) and (show_data.get("id") or show_data.get("_id")):
                episodes = show_data.get("episodes", [])
                if episodes:
                    lang_code = show_data.get("language") or dialect
                    dialect_name = _DIALECT_CODE_TO_NAME.get(lang_code, "marathi")
                    show_url_slug = build_url_slug(show_data.get("slug") or "", content_id)
                    
                    print(f"\n{COLOR_BOLD}{COLOR_YELLOW}Show: {COLOR_RESET}{COLOR_BOLD}{show_data.get('title')} ({len(episodes)} Episodes){COLOR_RESET}")
                    print(f"{COLOR_GRAY}{'=' * 60}{COLOR_RESET}")
                    for ep in episodes:
                        ep_num = ep.get("episodeOrder") or ep.get("order") or "?"
                        ep_title = ep.get("title") or f"Episode {ep_num}"
                        
                        full_source = ep.get("sourceLink")
                        peripheral_source = ep.get("selectedPeripheral", {}).get("sourceLink")
                        
                        watch_path = f"/en/{dialect_name}/show/watch/{show_url_slug}/{ep_num}"
                        hls_links = fetch_signed_hls_links(watch_path, cookies)
                        
                        print(f"{COLOR_BOLD}{COLOR_CYAN}Episode {ep_num}: {COLOR_RESET}{COLOR_BOLD}{ep_title}{COLOR_RESET}")
                        if hls_links:
                            for codec_label, hls_url in hls_links.items():
                                print(f"  {COLOR_BOLD}{COLOR_GREEN}{codec_label}: {COLOR_RESET}{COLOR_WHITE}{hls_url}{COLOR_RESET}")
                                
                        if full_source and full_source.strip():
                            full_link = f"https://media.stage.in/episode/main-video/{full_source.strip()}"
                            print(f"  {COLOR_BOLD}{COLOR_YELLOW}Raw Link (MP4): {COLOR_RESET}{COLOR_GRAY}{full_link}{COLOR_RESET}\n")
                        elif peripheral_source and peripheral_source.strip():
                            full_link = f"https://media.stage.in/show/main-video/{peripheral_source.strip()}"
                            print(f"  {COLOR_BOLD}{COLOR_YELLOW}Raw Link (MP4 Preview): {COLOR_RESET}{COLOR_GRAY}{full_link}{COLOR_RESET}\n")
                        else:
                            print()
                    sys.exit(0)
    except Exception:
        pass

    print(f"\n{COLOR_RED}Error: Could not retrieve link for ID {content_id}.{COLOR_RESET}", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    main()