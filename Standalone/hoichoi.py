#!/usr/bin/env python3
"""
Hoichoi Stream Link Generator
Fetches H264 (HLS m3u8 / DASH mpd), H265/HEVC, VP9, and AV1 links for any Hoichoi movie, episode, or show.
"""

import sys
import re
import os
import json
import requests
from bs4 import BeautifulSoup

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

def safe_print(text=""):
    try:
        print(text)
    except Exception:
        try:
            enc = sys.stdout.encoding or "utf-8"
            sys.stdout.write(text.encode(enc, errors="replace").decode(enc) + "\n")
        except Exception:
            sys.stdout.write(text.encode("ascii", errors="replace").decode("ascii") + "\n")

def load_and_refresh_tokens():
    token_paths = [
        r"d:\temp\vinetrimmer\cache\Hoichoi\token_default.json",
        os.path.join(os.path.dirname(__file__), "vinetrimmer", "cache", "Hoichoi", "token_default.json"),
        os.path.join(os.path.dirname(__file__), "..", "vinetrimmer", "cache", "Hoichoi", "token_default.json")
    ]
    token_file = None
    tokens = {}
    for tp in token_paths:
        if os.path.isfile(tp):
            token_file = tp
            try:
                with open(tp, "r", encoding="utf-8") as f:
                    tokens = json.load(f)
                break
            except Exception:
                pass
                
    if not tokens:
        return {}
        
    access_token = tokens.get("sAccessToken") or tokens.get("accessToken") or ""
    refresh_token = tokens.get("sRefreshToken") or tokens.get("refreshToken") or ""
    
    # Test token validity
    test_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "x-hoichoi-siteid": "hoichoitv",
        "authorization": f"Bearer {access_token}"
    }
    try:
        t_res = requests.get("https://prod-contents-api.hoichoi.dev/contents/api/v1/contents?platform=WEB&contentId=test", headers=test_headers, timeout=5)
        if t_res.status_code != 401:
            return tokens
    except Exception:
        pass
        
    # Attempt token refresh
    if refresh_token:
        try:
            r_resp = requests.post(
                'https://prod-api.hoichoi.dev/core/api/v1/auth/session/refresh',
                headers={
                    'authorization': f'Bearer {refresh_token}',
                    'x-bypass-proxy': 'true',
                    'x-hoichoi-siteid': 'hoichoitv',
                    'rid': 'anti-csrf',
                    'content-type': 'application/json',
                    'origin': 'https://hoichoi.tv',
                    'referer': 'https://hoichoi.tv/',
                    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
                },
                timeout=10
            )
            if r_resp.status_code == 200:
                data = r_resp.json()
                if data.get("sAccessToken"):
                    tokens["sAccessToken"] = data["sAccessToken"]
                    if data.get("sRefreshToken"):
                        tokens["sRefreshToken"] = data["sRefreshToken"]
                    if token_file:
                        with open(token_file, "w", encoding="utf-8") as f:
                            json.dump(tokens, f, indent=4)
        except Exception:
            pass
            
    return tokens

def resolve_content_id(input_str, headers):
    s = input_str.strip()
    
    # 1. Direct UUID
    if re.match(r'^[a-f0-9-]{36}$', s, re.I):
        return s
        
    # 2. Extract slug / path from URL
    clean_path = s
    if "hoichoi.tv" in s.lower():
        clean_path = s.split("hoichoi.tv/")[-1].strip("/")
        
    parts = [p for p in clean_path.split("/") if p]
    slug = parts[-1] if parts else clean_path
    content_type = parts[0] if parts else "movies"
    if content_type in ["shows", "series"]:
        content_type = "shows"
    elif content_type in ["films", "movie"]:
        content_type = "films"
        
    # 3. Try permalink candidates via API
    permalinks = [slug, f"{content_type}/{slug}", f"films/title/{slug}"]
    for p in permalinks:
        try:
            r = requests.get(
                "https://prod-contents-api.hoichoi.dev/contents/api/v1/contents",
                params={"language": "bengali", "permalink": p, "platform": "WEB"},
                headers=headers,
                timeout=8
            )
            if r.status_code == 200:
                cid = r.json().get("contentId")
                if cid:
                    return cid
        except Exception:
            pass
            
    # 4. Scrape web page for embedded contentId / UUIDs
    web_urls = [f"https://www.hoichoi.tv/{clean_path}/", f"https://hoichoi.tv/{clean_path}/"]
    for w_url in web_urls:
        try:
            r = requests.get(w_url, headers={"User-Agent": headers["User-Agent"]}, timeout=10)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, 'html.parser')
                ld_json = soup.find('script', type='application/ld+json')
                if ld_json:
                    try:
                        cid = json.loads(ld_json.string).get('identifier', '')
                        if re.match(r'^[a-f0-9-]{36}$', cid):
                            return cid
                    except Exception:
                        pass
                next_data = soup.find('script', id='__NEXT_DATA__')
                if next_data:
                    try:
                        cid = json.loads(next_data.string).get('props', {}).get('pageProps', {}).get('content', {}).get('contentId', '')
                        if re.match(r'^[a-f0-9-]{36}$', cid):
                            return cid
                    except Exception:
                        pass
                all_uuids = re.findall(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', r.text)
                for candidate in all_uuids:
                    try:
                        c_chk = requests.get(
                            "https://prod-contents-api.hoichoi.dev/contents/api/v1/contents",
                            params={"platform": "WEB", "contentId": candidate},
                            headers=headers,
                            timeout=5
                        )
                        if c_chk.status_code == 200 and c_chk.json().get("contentType"):
                            return candidate
                    except Exception:
                        pass
        except Exception:
            pass
            
    return None

def fetch_renditions_for_content(content_id, headers):
    """Fetch renditions either from entitlement API or fallback to content metadata API."""
    renditions = []
    
    # 1. Try Entitlement API
    try:
        ent_url = "https://prod-api.hoichoi.dev/contents/api/v1/entitlement"
        ent_params = {"contentId": content_id, "action": "STREAMING", "platform": "WIDEVINE", "language": "bengali", "licenseKey": "false"}
        ent_json = {"action": "STREAMING", "channel": "web", "channelId": None, "contentId": content_id, "parentContentId": "", "platform": "web", "userId": ""}
        r = requests.post(ent_url, params=ent_params, json=ent_json, headers=headers, timeout=10)
        if r.status_code == 200:
            renditions = r.json().get("videoDetails", {}).get("renditions", [])
    except Exception:
        pass

    # 2. Fallback to Content Metadata API
    if not renditions:
        try:
            r = requests.get(
                "https://prod-contents-api.hoichoi.dev/contents/api/v1/contents",
                params={"platform": "WEB", "contentId": content_id},
                headers=headers,
                timeout=10
            )
            if r.status_code == 200:
                renditions = r.json().get("renditions", [])
        except Exception:
            pass

    # Process renditions into organized dict
    links = {}
    for rend in renditions:
        codec = (rend.get("codec") or rend.get("name") or "H264").upper()
        main_url = rend.get("mainManifestUrl")
        alt_urls = rend.get("alternateManifestUrls") or []
        
        all_urls = [main_url] + alt_urls if main_url else alt_urls
        for url in all_urls:
            if not url:
                continue
            if url.lower().endswith(".m3u8"):
                key = f"{codec} (HLS m3u8)"
                if key not in links:
                    links[key] = url
            elif url.lower().endswith(".mpd"):
                key = f"{codec} (DASH mpd)"
                if key not in links:
                    links[key] = url
                    
    return links

def main():
    init_ansi()
    setup_utf8_output()
    
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
            prompt_str = f"{COLOR_BOLD}{COLOR_CYAN}Enter Hoichoi Movie or Show ID/URL: {COLOR_RESET}"
            input_str = input(prompt_str).strip()
        except (KeyboardInterrupt, EOFError):
            safe_print()
            sys.exit(0)

    if not input_str:
        safe_print(f"{COLOR_RED}Error: No input provided.{COLOR_RESET}")
        sys.exit(1)

    tokens = load_and_refresh_tokens()
    access_token = tokens.get("sAccessToken") or tokens.get("accessToken") or ""

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "x-hoichoi-siteid": "hoichoitv",
        "x-bypass-proxy": "true",
        "origin": "https://hoichoi.tv",
        "referer": "https://hoichoi.tv/"
    }
    if access_token:
        headers["authorization"] = f"Bearer {access_token}"

    content_id = resolve_content_id(input_str, headers)
    if not content_id:
        safe_print(f"{COLOR_RED}Error: Could not resolve Hoichoi content ID for '{input_str}'.{COLOR_RESET}")
        sys.exit(1)

    # Fetch main content metadata
    resp = requests.get(
        "https://prod-contents-api.hoichoi.dev/contents/api/v1/contents",
        params={"platform": "WEB", "contentId": content_id},
        headers=headers,
        timeout=10
    )

    if resp.status_code != 200:
        safe_print(f"{COLOR_RED}Error: API returned status {resp.status_code} for content ID {content_id}.{COLOR_RESET}")
        sys.exit(1)

    meta = resp.json()
    content_type = meta.get("contentType", "").lower()
    title_name = meta.get("title") or "Hoichoi Content"

    if content_type in ["movie", "film", "episode"]:
        type_label = "Movie" if content_type in ["movie", "film"] else "Episode"
        safe_print(f"\n{COLOR_BOLD}{COLOR_YELLOW}{type_label}: {COLOR_RESET}{COLOR_BOLD}{title_name}{COLOR_RESET}")
        links = fetch_renditions_for_content(content_id, headers)
        if links:
            for label, url in links.items():
                safe_print(f"  {COLOR_BOLD}{COLOR_GREEN}{label}: {COLOR_RESET}{COLOR_WHITE}{url}{COLOR_RESET}")
        else:
            safe_print(f"  {COLOR_RED}No streaming renditions found.{COLOR_RESET}")
        safe_print()
        sys.exit(0)

    # Show / Series
    seasons = meta.get("seasons", [])
    total_episodes = sum(len(s.get("episodes") or []) for s in seasons)
    
    safe_print(f"\n{COLOR_BOLD}{COLOR_YELLOW}Show: {COLOR_RESET}{COLOR_BOLD}{title_name} ({len(seasons)} Seasons, {total_episodes} Episodes){COLOR_RESET}")
    safe_print(f"{COLOR_GRAY}{'=' * 65}{COLOR_RESET}")

    for s_idx, season in enumerate(seasons, start=1):
        episodes = season.get("episodes") or []
        for ep in episodes:
            ep_num = ep.get("episodeNumber") or 1
            ep_title = ep.get("title") or f"Episode {ep_num}"
            ep_cid = ep.get("contentId")
            
            safe_print(f"{COLOR_BOLD}{COLOR_CYAN}S{s_idx:02d}E{ep_num:02d}: {COLOR_RESET}{COLOR_BOLD}{ep_title}{COLOR_RESET}")
            if ep_cid:
                links = fetch_renditions_for_content(ep_cid, headers)
                if links:
                    for label, url in links.items():
                        safe_print(f"  {COLOR_BOLD}{COLOR_GREEN}{label}: {COLOR_RESET}{COLOR_WHITE}{url}{COLOR_RESET}")
                else:
                    safe_print(f"  {COLOR_RED}No streaming renditions found.{COLOR_RESET}")
            safe_print()

if __name__ == "__main__":
    main()
