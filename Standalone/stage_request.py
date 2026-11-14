#!/usr/bin/env python3
"""
STAGE Movies & Shows Request Link Fetcher
Searches STAGE.in across all languages/dialects for both Movies and Web Series/Shows, 
and retrieves verified H264, H265, AV1 (HLS m3u8) links and direct raw MP4 video links (Original Master 1080p/4K).
Outputs in formatted merged layout with audio track specs.
"""

import sys
import re
import argparse
import requests
import json
import os
import shutil
import random
import string
import subprocess
from concurrent.futures import ThreadPoolExecutor

ALL_DIALECTS = {
    "haryanvi": "har",
    "rajasthani": "raj",
    "bhojpuri": "bho",
    "gujarati": "guj",
    "marathi": "mar",
    "bengali": "ben",
    "hindi": "hin",
    "punjabi": "pun"
}

CODE_TO_NAME = {v: k.capitalize() for k, v in ALL_DIALECTS.items()}

LANG_AUDIO_SPECS = {
    "har": ("har", "Haryanvi"),
    "raj": ("raj", "Rajasthani"),
    "bho": ("bho", "Bhojpuri"),
    "guj": ("guj", "Gujarati"),
    "mar": ("mar", "Marathi"),
    "ben": ("ben", "বাংলা"),
    "hin": ("hin", "Hindi"),
    "pun": ("pun", "Punjabi")
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

def setup_utf8_output():
    """Ensure standard output handles UTF-8 characters safely on Windows."""
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass

def build_headers(dialect_code="har"):
    return {
        "app_id": "in.stage.web",
        "platform": "web",
        "os": "android",
        "api-version": "2",
        "dialect": dialect_code,
        "lang": "en"
    }

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

def fetch_signed_hls_links(watch_path):
    if not watch_path:
        return {}
    rsc_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    url = f"https://www.stage.in{watch_path}?_rsc={rsc_id}"
    try:
        session = requests.Session()
        cookies = load_cookies()
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

def verify_and_build_links(source_filename, check_subfolders=False):
    """
    Constructs direct raw MP4 URL. Master URL is 100% guaranteed working (1080p/4K Master).
    """
    base_cdn = "https://media.stage.in/episode/main-video"
    fn = source_filename.strip()
    master_url = f"{base_cdn}/{fn}"
    
    links = {
        "Master (Original 1080p/4K)": master_url
    }
    
    if check_subfolders:
        test_qualities = [("1080p", f"{base_cdn}/1080/{fn}"), ("720p", f"{base_cdn}/720/{fn}")]
        headers = {"User-Agent": "Mozilla/5.0"}
        
        def check_url(q_tuple):
            label, url = q_tuple
            try:
                r = requests.head(url, headers=headers, timeout=3)
                if r.status_code == 200:
                    return label, url
            except Exception:
                pass
            return None
            
        with ThreadPoolExecutor(max_workers=len(test_qualities)) as executor:
            res = list(executor.map(check_url, test_qualities))
            for item in res:
                if item:
                    links[item[0]] = item[1]
                    
    return links

def get_movie_details(movie_id, dialect_code, check_subfolders=False):
    """Fetch movie details using dialect header and extract link info."""
    url = "https://stageapi.stage.in/nest/contents/movies/details"
    headers = build_headers(dialect_code)
    try:
        r = requests.get(url, params={"movieId": str(movie_id)}, headers=headers, timeout=10)
        if r.status_code == 200:
            movie = r.json().get("movie", {})
            if isinstance(movie, dict) and (movie.get("_id") or movie.get("id")):
                src = movie.get("sourceLink") or movie.get("selectedPeripheral", {}).get("sourceLink")
                t_val = movie.get("title", "Movie")
                title = (t_val.get("en") or t_val.get("hi") or str(t_val)) if isinstance(t_val, dict) else str(t_val)
                slug = movie.get("slug", "")
                
                lang_code = movie.get("language") or dialect_code
                d_name = CODE_TO_NAME.get(lang_code, lang_code).lower()
                url_slug = f"{slug.rsplit('-', 1)[0]}-{movie_id}" if slug else str(movie_id)
                watch_path = f"/en/{d_name}/movie/watch/{url_slug}"
                hls_links = fetch_signed_hls_links(watch_path)

                links = []
                if src and src.strip() and not src.strip().endswith(".m3u8"):
                    res_links = hls_links.copy()
                    res_links.update(verify_and_build_links(src.strip(), check_subfolders))
                    links.append((title, res_links))
                return title, slug, links
    except Exception:
        pass
    return None, None, []

def get_show_details(show_id, dialect_code, check_subfolders=False):
    """Fetch show details and extract direct links for all episodes."""
    url = "https://stageapi.stage.in/nest/contents/shows/details"
    headers = build_headers(dialect_code)
    try:
        r = requests.get(url, params={"showId": str(show_id), "seasonId": ""}, headers=headers, timeout=10)
        if r.status_code == 200:
            show_data = r.json().get("data", {})
            if isinstance(show_data, dict) and (show_data.get("id") or show_data.get("_id")):
                title = show_data.get("title", "Show")
                slug = show_data.get("slug", "")
                dialect_mapping = show_data.get("dialactMapping") or {}
                
                lang_code = show_data.get("language") or dialect_code
                d_name = CODE_TO_NAME.get(lang_code, lang_code).lower()
                show_url_slug = f"{slug.rsplit('-', 1)[0]}-{show_id}" if slug else str(show_id)

                episodes = show_data.get("episodes", [])
                links = []
                for ep in episodes:
                    ep_num = ep.get("episodeOrder") or ep.get("order") or "?"
                    ep_title = (ep.get("title") or "").strip()
                    if not ep_title or ep_title.lower() in [f"episode {ep_num}".lower(), f"एपिसोड {ep_num}".lower()]:
                        display_title = f"Episode {ep_num}"
                    else:
                        display_title = f"Episode {ep_num}: {ep_title}"

                    full_source = ep.get("sourceLink")
                    peripheral_source = ep.get("selectedPeripheral", {}).get("sourceLink")
                    target_source = full_source if (full_source and full_source.strip()) else peripheral_source
                    
                    watch_path = f"/en/{d_name}/show/watch/{show_url_slug}/{ep_num}"
                    hls_links = fetch_signed_hls_links(watch_path)

                    if target_source and target_source.strip() and not target_source.strip().endswith(".m3u8"):
                        res_links = hls_links.copy()
                        res_links.update(verify_and_build_links(target_source.strip(), check_subfolders))
                        links.append((display_title, res_links))
                return title, slug, dialect_mapping, links
    except Exception:
        pass
    return None, None, {}, []

def resolve_all_languages_by_id(content_id, check_subfolders=False):
    """Given a single ID or URL ID, resolve cross-dialect mapped IDs using dialactMapping & base slug."""
    found_info = None
    
    for d_code in ALL_DIALECTS.values():
        title, slug, dialect_map, links = get_show_details(content_id, d_code, check_subfolders)
        if links:
            found_info = ("show", title, slug, dialect_map, links)
            break
        m_title, m_slug, m_links = get_movie_details(content_id, d_code, check_subfolders)
        if m_links:
            found_info = ("movie", m_title, m_slug, {}, m_links)
            break

    if not found_info:
        return []

    item_type, main_title, main_slug, dialect_map, initial_links = found_info
    
    if item_type == "show" and dialect_map:
        def show_map_worker(d_tuple):
            d_code, map_data = d_tuple
            d_show_id = map_data.get("showId")
            if d_show_id:
                s_title, _, _, s_links = get_show_details(d_show_id, d_code, check_subfolders)
                if s_links:
                    return CODE_TO_NAME.get(d_code, d_code), d_code, "show", str(d_show_id), s_title or main_title, s_links
            return None

        with ThreadPoolExecutor(max_workers=len(dialect_map)) as executor:
            map_res = list(executor.map(show_map_worker, dialect_map.items()))
        
        all_results = [r for r in map_res if r]
        if all_results:
            return all_results

    base_slug = re.sub(r'-[a-z]{3}$', '', main_slug) if main_slug else main_title.lower()
    search_query = base_slug if base_slug else main_title

    return search_all_dialects(search_query, check_subfolders)

def search_movies_in_dialect(d_name, d_code, query):
    """Search movie catalog for a dialect."""
    headers = build_headers(d_code)
    matches = []
    try:
        r = requests.get("https://stageapi.stage.in/nest/contents/movies/all", params={"page": 1, "perPage": 50}, headers=headers, timeout=10)
        if r.status_code == 200:
            for m in r.json().get("data", []):
                slug = m.get("slug", "")
                t_dict = m.get("title", {})
                t_en = t_dict.get("en", "") if isinstance(t_dict, dict) else str(t_dict)
                t_hi = t_dict.get("hi", "") if isinstance(t_dict, dict) else str(t_dict)
                if query.lower() in f"{slug} {t_en} {t_hi}".lower():
                    m_id = str(m.get("_id"))
                    matches.append((d_name.capitalize(), d_code, "movie", m_id, t_en or t_hi or "Movie"))
    except Exception:
        pass
    return matches

def search_shows_in_dialect(d_name, d_code, query):
    """Search show catalog for a dialect."""
    url = f"https://www.stage.in/en/{d_name}/shows"
    web_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    matches = []
    try:
        resp = requests.get(url, headers=web_headers, timeout=15)
        if resp.status_code == 200:
            next_f_calls = re.findall(r'self\.__next_f\.push\(\[(.*?)\n?\]\)', resp.text)
            for call in next_f_calls:
                m = re.match(r'^\d+,\s*"(.*)"$', call, re.DOTALL)
                if m:
                    val = m.group(1)
                    try:
                        val = json.loads('"' + val + '"')
                    except Exception:
                        pass
                    
                    for im in re.finditer(r'"items"\s*:\s*\[', val):
                        start = im.end() - 1
                        try:
                            arr, _ = json.JSONDecoder().raw_decode(val, start)
                            for item in arr:
                                if isinstance(item, dict) and item.get("type") == "show":
                                    slug = item.get("slug", "")
                                    title = item.get("title", "")
                                    if query.lower() in f"{slug} {title}".lower():
                                        s_id = str(item.get("_id"))
                                        matches.append((d_name.capitalize(), d_code, "show", s_id, title))
                        except Exception:
                            pass
    except Exception:
        pass
    return list({m[3]: m for m in matches}.values())

def search_all_dialects(query, check_subfolders=False):
    """Perform comprehensive search across all dialects for movies and shows."""
    matches = []

    with ThreadPoolExecutor(max_workers=len(ALL_DIALECTS) * 2) as executor:
        movie_futures = [executor.submit(search_movies_in_dialect, name, code, query) for name, code in ALL_DIALECTS.items()]
        show_futures = [executor.submit(search_shows_in_dialect, name, code, query) for name, code in ALL_DIALECTS.items()]

        for f in movie_futures:
            matches.extend(f.result())
        for f in show_futures:
            matches.extend(f.result())

    if not matches:
        def search_v20(d_tuple):
            d_name, d_code = d_tuple
            headers = build_headers(d_code)
            v20_res = []
            try:
                r = requests.get("https://stageapi.stage.in/v20/search/search", params={"keyword": query}, headers=headers, timeout=10)
                if r.status_code == 200:
                    items = r.json().get("data", {}).get("searchDataList", [])
                    for item in items:
                        item_type = item.get("type", "movie").lower()
                        if item_type in ["artist", "person", "genre"]:
                            continue
                        item_id = str(item.get("_id") or item.get("id"))
                        t_val = item.get("title", {})
                        title = (t_val.get("en") or t_val.get("hi") or str(t_val)) if isinstance(t_val, dict) else str(t_val)
                        v20_res.append((d_name.capitalize(), d_code, item_type, item_id, title))
            except Exception:
                pass
            return v20_res

        with ThreadPoolExecutor(max_workers=len(ALL_DIALECTS)) as executor:
            v20_futures = [executor.submit(search_v20, d) for d in ALL_DIALECTS.items()]
            for f in v20_futures:
                matches.extend(f.result())

    unique_matches = {}
    for d_name, d_code, item_type, item_id, title in matches:
        key = f"{d_code}:{item_type}:{item_id}"
        if key not in unique_matches:
            unique_matches[key] = (d_name, d_code, item_type, item_id, title)

    def resolve_worker(match_tuple):
        d_name, d_code, item_type, item_id, title = match_tuple
        if item_type == "show":
            _, _, _, links = get_show_details(item_id, d_code, check_subfolders)
        else:
            _, _, links = get_movie_details(item_id, d_code, check_subfolders)
            if not links:
                _, _, _, links = get_show_details(item_id, d_code, check_subfolders)
        return d_name, d_code, item_type, item_id, title, links

    with ThreadPoolExecutor(max_workers=20) as executor:
        resolved = list(executor.map(resolve_worker, unique_matches.values()))

    valid_results = [r for r in resolved if r[5]]
    return valid_results

def parse_input_id(input_str):
    """Accurately parse input to determine if it is a pure ID or STAGE URL vs search term."""
    s = input_str.strip()
    if s.isdigit():
        return True, s
    if "stage.in" in s.lower():
        m = re.search(r'[-/](\d+)(?:/|\?|$)', s)
        if m:
            return True, m.group(1)
    if s.lower().startswith(("movie:", "show:")):
        m = re.search(r'\d+', s)
        if m:
            return True, m.group(0)
    return False, None

def get_audio_specs(video_url, d_code):
    """
    Probes video_url using ffprobe to retrieve audio channel layout and bitrate.
    Falls back to default ("2.0", "319 kb/s") on failure.
    """
    default_res = ("2.0", "319 kb/s")
    binaries_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "binaries"))
    ffprobe_path = shutil.which("ffprobe") or os.path.join(binaries_dir, "ffprobe.exe")
    
    if not os.path.exists(ffprobe_path) and not shutil.which("ffprobe"):
        return default_res

    headers = build_headers(d_code)
    headers_str = "\r\n".join(f"{k}: {v}" for k, v in headers.items()) + "\r\n"
    
    cmd = [
        ffprobe_path,
        "-v", "quiet",
        "-headers", headers_str,
        "-probesize", "500000",
        "-analyzeduration", "500000",
        "-print_format", "json",
        "-show_streams",
        video_url
    ]
    
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            info = json.loads(res.stdout)
            for stream in info.get("streams", []):
                if stream.get("codec_type") == "audio":
                    channels = stream.get("channels")
                    bitrate = stream.get("bit_rate")
                    
                    if channels == 2:
                        channels_str = "2.0"
                    elif channels == 6:
                        channels_str = "5.1"
                    elif channels:
                        channels_str = f"{channels}.0"
                    else:
                        channels_str = "2.0"
                        
                    if bitrate:
                        try:
                            kbps = int(bitrate) // 1000
                            bitrate_str = f"{kbps} kb/s"
                        except Exception:
                            bitrate_str = "319 kb/s"
                    else:
                        bitrate_str = "319 kb/s"
                        
                    return channels_str, bitrate_str
    except Exception:
        pass
        
    return default_res

def print_results(results, COLOR_BOLD="", COLOR_CYAN="", COLOR_GREEN="", COLOR_YELLOW="", COLOR_RESET="", COLOR_WHITE=""):
    """Format and print verified results in merged format."""
    if not results:
        return

    avail_langs = [d_name for d_name, _, _, _, _, _ in results]
    main_title = results[0][4]

    print(f"\n{COLOR_BOLD}{COLOR_WHITE}{main_title} Available in {', '.join(avail_langs)}{COLOR_RESET}")
    print(f"{COLOR_WHITE}{'=' * 65}{COLOR_RESET}")

    for d_name, d_code, item_type, item_id, title, links in results:
        code, native_name = LANG_AUDIO_SPECS.get(d_code, (d_code, d_name))
        
        print(f"\n{COLOR_BOLD}{COLOR_CYAN}🔥 [Audio - {d_name}] {title} 👇🏻👇🏻{COLOR_RESET}\n")
        
        is_multi_ep = len(links) > 1 or (len(links) == 1 and links[0][0] and links[0][0].strip() != title.strip())

        for item_tuple in links:
            ep_title = item_tuple[0]
            res_dict = item_tuple[1]

            if is_multi_ep and ep_title and ep_title.strip() and ep_title.strip() != title.strip():
                print(f"{COLOR_BOLD}{COLOR_WHITE}{ep_title}:{COLOR_RESET}")

            for q_label, q_url in res_dict.items():
                if "master" in q_label.lower() or "original" in q_label.lower():
                    print(f"  {COLOR_BOLD}{COLOR_GREEN}Raw Link (MP4):{COLOR_RESET} {COLOR_WHITE}{q_url}{COLOR_RESET}")
                else:
                    print(f"  {COLOR_BOLD}{COLOR_GREEN}{q_label}:{COLOR_RESET} {COLOR_WHITE}{q_url}{COLOR_RESET}")
            print()

        audio_channels, audio_bitrate = "2.0", "319 kb/s"
        if links:
            first_ep_links = links[0][1]
            if first_ep_links:
                video_url = first_ep_links.get("Master (Original 1080p/4K)") or next(iter(first_ep_links.values()))
                audio_channels, audio_bitrate = get_audio_specs(video_url, d_code)

        print(f"{COLOR_BOLD}{COLOR_YELLOW}🗣 Audio Track:{COLOR_RESET}")
        print(f"{COLOR_WHITE} Tracks : AUD | AAC | {audio_channels} | {audio_bitrate} | {code} | {native_name} [Original]{COLOR_RESET}")

def main():
    init_ansi()
    setup_utf8_output()

    use_color = sys.stdout.isatty()
    COLOR_RESET = "\033[0m" if use_color else ""
    COLOR_BOLD = "\033[1m" if use_color else ""
    COLOR_CYAN = "\033[1;96m" if use_color else ""
    COLOR_GREEN = "\033[1;92m" if use_color else ""
    COLOR_YELLOW = "\033[1;93m" if use_color else ""
    COLOR_RED = "\033[1;91m" if use_color else ""
    COLOR_WHITE = "\033[97m" if use_color else ""

    parser = argparse.ArgumentParser(description="STAGE Movies & Shows Request Link Fetcher")
    parser.add_argument("request", nargs="?", help="Movie/Show title, search query, STAGE URL, or content ID")
    parser.add_argument("-v", "--verify-subfolders", action="store_true", help="Probe CDN for /1080/ & /720/ subfolder URLs")
    args = parser.parse_args()

    user_input = args.request

    if not user_input:
        print(f"{COLOR_BOLD}{COLOR_CYAN}=== STAGE Movies & Shows Link Generator ==={COLOR_RESET}\n")
        try:
            prompt_str = f"{COLOR_BOLD}Enter movie or show title, STAGE URL, or content ID: {COLOR_RESET}"
            user_input = input(prompt_str).strip()
        except (KeyboardInterrupt, EOFError):
            print()
            sys.exit(0)

    if not user_input:
        print(f"{COLOR_RED}Error: No request provided.{COLOR_RESET}", file=sys.stderr)
        sys.exit(1)

    is_url_or_id, content_id = parse_input_id(user_input)

    if is_url_or_id and content_id:
        results = resolve_all_languages_by_id(content_id, check_subfolders=args.verify_subfolders)

        if results:
            print_results(results, COLOR_BOLD, COLOR_CYAN, COLOR_GREEN, COLOR_YELLOW, COLOR_RESET, COLOR_WHITE)
            print()
            sys.exit(0)
        else:
            print(f"{COLOR_RED}Error: Could not retrieve link for ID {content_id}.{COLOR_RESET}", file=sys.stderr)
            sys.exit(1)

    results = search_all_dialects(user_input, check_subfolders=args.verify_subfolders)

    if not results:
        print(f"{COLOR_RED}No video links found for '{user_input}' across any STAGE language.{COLOR_RESET}", file=sys.stderr)
        sys.exit(1)

    print_results(results, COLOR_BOLD, COLOR_CYAN, COLOR_GREEN, COLOR_YELLOW, COLOR_RESET, COLOR_WHITE)
    print()

if __name__ == "__main__":
    main()
