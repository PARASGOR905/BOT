"""
NowTV UK Poster Downloader
===========================
Fetches high-resolution movie and TV show poster images, clean backdrops,
and logos from NOW TV UK (nowtv.com).

Features:
  - Fetches Portrait (3:4, 2:3), Landscape (16:9), Clean Backdrops, Cover Art, and Logos.
  - Supports Movies, TV Series, Seasons, and Episode stills.
  - Interactive prompts for URL and Cookies if not passed via command-line arguments.
  - Supports Cookies in Netscape format (.txt), JSON format, or raw Cookie header string.
  - Automatically requests maximum quality (uncompressed/HD) images.
  - Fallback to HTML og:image / JSON-LD scraping if needed.

Usage:
  Interactive Mode:
    python nowtvuk_poster_downloader.py

  CLI Arguments:
    python nowtvuk_poster_downloader.py --url "https://www.nowtv.com/watch/asset/movies/bad-boys-ride-or-die/A5EK6sKrAayekc4h5w6Vs"
    python nowtvuk_poster_downloader.py -u "https://www.nowtv.com/watch/asset/tv/house-of-the-dragon/iYEQZ2rcf32XRKvQ5gm2Aq" -c "cookies.txt"
    python nowtvuk_poster_downloader.py -u "/bad-boys-ride-or-die/A5EK6sKrAayekc4h5w6Vs" -o "my_posters"

Dependencies:
    pip install requests beautifulsoup4
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import sys
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

# Ensure UTF-8 output encoding and unbuffered output for Windows command prompts
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import functools
print = functools.partial(print, flush=True)


# ── Configuration & Constants ────────────────────────────────────────────────
NOWTV_API_NODE = "https://eu.api.atom.nowtv.com/adapter-calypso/v3/query/node"

DEFAULT_HEADERS = {
    "Accept": "*/*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "X-SkyOTT-Device": "TV",
    "X-SkyOTT-Platform": "ANDROIDTV",
    "X-SkyOTT-Proposition": "NOWOTT",
    "X-SkyOTT-Provider": "NOWTV",
    "X-SkyOTT-Territory": "GB",
}

DEFAULT_OUTPUT_DIR = "nowtv_posters"

# Friendly labels for image types
IMAGE_TYPE_LABELS: dict[str, str] = {
    "titleArt34": "Poster_Portrait_Cover_3x4",
    "nonTitleArt34": "Poster_Portrait_Clean_3x4",
    "scene34": "Poster_Portrait_Scene_3x4",
    "landscape": "Poster_Landscape_16x9",
    "titleArt169": "Poster_Landscape_Cover_16x9",
    "nonTitleArt169": "Poster_Landscape_Clean_16x9",
    "scene169": "Poster_Landscape_Scene_16x9",
    "titleLogo": "Logo_Title",
    "brandLogo": "Logo_Brand",
    "hero": "Banner_Hero",
    "banner": "Banner",
    "portrait": "Poster_Portrait",
    "square": "Poster_Square_1x1",
}


# ── Helper Functions ─────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    """Remove or replace characters that are invalid in Windows/Linux filenames."""
    if not name:
        return "untitled"
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(". ")
    return name or "untitled"


def optimize_image_url(image_url: str, high_quality: bool = True) -> str:
    """
    Ensure the NowTV / Sky disco pixel image URL requests maximum resolution/quality.
    """
    if not image_url or not high_quality:
        return image_url

    parsed = urlparse(image_url)
    if "pixel.disco.nowtv.com" in parsed.netloc or "sky.com" in parsed.netloc:
        query_params = parse_qs(parsed.query)
        # Request high quality jpeg rendering
        query_params["format"] = ["jpg"]
        query_params["quality"] = ["100"]
        # Remove any limiting dimensions if present
        query_params.pop("width", None)
        query_params.pop("height", None)

        new_query = urlencode(query_params, doseq=True)
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment
        ))

    return image_url


def extract_slug(input_str: str) -> str:
    """
    Extract the NowTV node slug from a full URL, partial path, or direct slug.
    Examples:
      - 'https://www.nowtv.com/watch/asset/movies/bad-boys-ride-or-die/A5EK6sKrAayekc4h5w6Vs'
         -> '/bad-boys-ride-or-die/A5EK6sKrAayekc4h5w6Vs'
      - 'https://www.nowtv.com/watch/asset/tv/house-of-the-dragon/iYEQZ2rcf32XRKvQ5gm2Aq'
         -> '/house-of-the-dragon/iYEQZ2rcf32XRKvQ5gm2Aq'
      - '/bad-boys-ride-or-die/A5EK6sKrAayekc4h5w6Vs'
         -> '/bad-boys-ride-or-die/A5EK6sKrAayekc4h5w6Vs'
    """
    cleaned = input_str.strip().strip("'\"")
    if not cleaned:
        return ""

    # If it's a URL
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        parsed = urlparse(cleaned)
        path = parsed.path.rstrip("/")

        # Match /watch/asset/(section)?/<slug>/<id>
        m = re.search(r"/watch/asset(?:/[a-zA-Z0-9_-]+)?(/[^/?#]+/[a-zA-Z0-9_-]+)$", path, re.I)
        if m:
            return m.group(1)

        # Match /watch/(programme|movie|tv|series|entertainment)/<slug>/<id>
        m = re.search(r"/watch/(?:programme|movie|tv|series|entertainment|movies|cinema)(/[^/?#]+/[a-zA-Z0-9_-]+)$", path, re.I)
        if m:
            return m.group(1)

        # Match general /<title-slug>/<id> at the end of path
        m = re.search(r"(/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]{5,})$", path)
        if m:
            return m.group(1)

        # Match /watch/asset/<id>
        m = re.search(r"/watch/asset(/[^/?#]+)$", path, re.I)
        if m:
            return m.group(1)

        return path

    # Not a full URL
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return cleaned


def load_cookies(session: requests.Session, cookie_input: str) -> bool:
    """
    Load cookies into a requests.Session from:
      1. Netscape cookies.txt file path
      2. JSON cookie file path (EditThisCookie / Cookie-Editor format)
      3. Raw cookie string (e.g. 'name=val; name2=val2')
    """
    cookie_input = cookie_input.strip().strip("'\"")
    if not cookie_input:
        return False

    # 1. Check if it is a file path
    if os.path.isfile(cookie_input):
        print(f"[*] Loading cookies from file: {cookie_input}")
        # Try Netscape format first
        try:
            cj = http.cookiejar.MozillaCookieJar(cookie_input)
            cj.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(cj)
            print(f"[+] Successfully loaded Netscape cookies ({len(session.cookies)} cookies).")
            return True
        except Exception:
            pass

        # Try JSON format
        try:
            with open(cookie_input, "r", encoding="utf-8") as f:
                data = json.load(f)
            count = 0
            if isinstance(data, list):
                for c in data:
                    if isinstance(c, dict) and "name" in c and "value" in c:
                        session.cookies.set(
                            c["name"],
                            c["value"],
                            domain=c.get("domain", ".nowtv.com"),
                            path=c.get("path", "/")
                        )
                        count += 1
            elif isinstance(data, dict):
                for k, v in data.items():
                    session.cookies.set(str(k), str(v), domain=".nowtv.com", path="/")
                    count += 1
            if count > 0:
                print(f"[+] Successfully loaded JSON cookies ({count} cookies).")
                return True
        except Exception:
            pass

        # Try reading raw lines / header from file
        try:
            with open(cookie_input, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                return load_cookies(session, content)
        except Exception as e:
            print(f"[-] Could not parse cookie file: {e}")
            return False

    # 2. Treat as raw cookie header string (name=value; name2=value2)
    if "=" in cookie_input:
        count = 0
        pairs = cookie_input.split(";")
        for pair in pairs:
            if "=" in pair:
                k, v = pair.strip().split("=", 1)
                session.cookies.set(k.strip(), v.strip(), domain=".nowtv.com", path="/")
                count += 1
        if count > 0:
            print(f"[+] Loaded {count} cookie(s) from raw string.")
            return True

    print("[-] Warning: Unrecognized cookie format or file not found.")
    return False


# ── Poster Fetching Logic ────────────────────────────────────────────────────

def fetch_posters_from_api(
    session: requests.Session,
    slug: str,
    include_episodes: bool = False
) -> tuple[str, str | None, list[dict]]:
    """
    Query NowTV Atom Calypso node API for high-resolution images.
    Returns (title, year, images_list).
    """
    params = {
        "slug": slug,
        "represent": "(items(items),recs[take=8],collections(items(items[take=8])),trailers)",
        "features": "upcoming",
        "contentSegments": (
            "ENTERTAINMENT,HAYU,KIDS,MOVIES,NEWS,SHORTFORM,"
            "SPORTS,SPORTS_CORE,SPORTS_ESSENTIALS,SPORTS_EVENTS,"
            "SPORTS_EVENTS_EXCLUSIVE,SPORTS_EXTRA,SPORTS_EXTRA_EXCLUSIVE,"
            "SSN,HBOMAX,CINEMA,PEACOCK"
        ),
    }

    resp = session.get(NOWTV_API_NODE, params=params, headers=DEFAULT_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    attrs = data.get("attributes", {})
    title = attrs.get("title") or attrs.get("titleLong") or attrs.get("titleMedium") or "NowTV_Content"
    year = attrs.get("year")
    if year:
        year = str(year)

    collected_images: list[dict] = []
    seen_urls: set[str] = set()

    # 1. Series / Movie level images
    for img in attrs.get("images", []):
        raw_url = img.get("url")
        if not raw_url:
            continue
        raw_type = img.get("type", "image")
        label = IMAGE_TYPE_LABELS.get(raw_type, f"Poster_{raw_type}")
        best_url = optimize_image_url(raw_url)

        if best_url not in seen_urls:
            seen_urls.add(best_url)
            collected_images.append({
                "label": label,
                "url": best_url,
                "type": raw_type,
                "category": "Main",
            })

    # 2. Check for Seasons / Episodes if TV show
    relationships = data.get("relationships", {})
    seasons = relationships.get("items", {}).get("data", [])

    for s_idx, season in enumerate(seasons, start=1):
        s_attrs = season.get("attributes", {})
        s_num = s_attrs.get("seasonNumber", s_idx)

        # Season-specific images
        for img in s_attrs.get("images", []):
            raw_url = img.get("url")
            if not raw_url:
                continue
            raw_type = img.get("type", "image")
            label = f"Season_{s_num:02d}_{IMAGE_TYPE_LABELS.get(raw_type, raw_type)}"
            best_url = optimize_image_url(raw_url)

            if best_url not in seen_urls:
                seen_urls.add(best_url)
                collected_images.append({
                    "label": label,
                    "url": best_url,
                    "type": raw_type,
                    "category": f"Season {s_num}",
                })

        # Episode-specific images (if enabled)
        if include_episodes:
            episodes = season.get("relationships", {}).get("items", {}).get("data", [])
            for e_idx, ep in enumerate(episodes, start=1):
                ep_attrs = ep.get("attributes", {})
                ep_num = ep_attrs.get("episodeNumber", e_idx)
                ep_title = ep_attrs.get("title") or f"Episode {ep_num}"
                ep_safe_title = sanitize_filename(ep_title)

                for img in ep_attrs.get("images", []):
                    raw_url = img.get("url")
                    if not raw_url:
                        continue
                    raw_type = img.get("type", "still")
                    label = f"S{s_num:02d}E{ep_num:02d}_{ep_safe_title}_{IMAGE_TYPE_LABELS.get(raw_type, raw_type)}"
                    best_url = optimize_image_url(raw_url)

                    if best_url not in seen_urls:
                        seen_urls.add(best_url)
                        collected_images.append({
                            "label": label,
                            "url": best_url,
                            "type": raw_type,
                            "category": f"Season {s_num} Episodes",
                        })

    return title, year, collected_images


def fetch_posters_from_webpage(
    session: requests.Session,
    url: str
) -> tuple[str, str | None, list[dict]]:
    """
    Fallback scraper: Fetches page HTML and extracts OpenGraph, JSON-LD, and image tags.
    """
    print("[*] Scraping webpage directly as fallback...")
    resp = session.get(url, headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]}, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Title extraction
    title = None
    title_tag = soup.find("title")
    if title_tag and title_tag.text:
        title = title_tag.text.strip()
        title = re.split(r" - | \| | Watch | on NOW", title)[0].strip()

    if not title:
        title = url.rstrip("/").split("/")[-1]

    collected_images: list[dict] = []
    seen_urls: set[str] = set()

    def add_img(img_url: str, label: str):
        if not img_url:
            return
        if img_url.startswith("//"):
            img_url = "https:" + img_url
        opt_url = optimize_image_url(img_url)
        if opt_url not in seen_urls:
            seen_urls.add(opt_url)
            collected_images.append({
                "label": label,
                "url": opt_url,
                "type": "web",
                "category": "Main",
            })

    # 1. OpenGraph image
    og_img = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
    if og_img and og_img.get("content"):
        add_img(og_img["content"], "Poster_OpenGraph")

    # 2. JSON-LD schema images
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            ld = json.loads(script.string or "{}")
            if isinstance(ld, dict):
                if "image" in ld:
                    if isinstance(ld["image"], str):
                        add_img(ld["image"], "Poster_JSONLD")
                    elif isinstance(ld["image"], list):
                        for idx, item in enumerate(ld["image"], start=1):
                            add_img(item if isinstance(item, str) else item.get("url"), f"Poster_JSONLD_{idx}")
                if "@graph" in ld:
                    for g in ld["@graph"]:
                        if "image" in g:
                            add_img(g["image"] if isinstance(g["image"], str) else g["image"].get("url"), "Poster_JSONLD")
        except Exception:
            pass

    # 3. High quality image tags
    for img_tag in soup.find_all("img"):
        src = img_tag.get("src") or img_tag.get("data-src")
        if src and ("pixel.disco.nowtv.com" in src or "images.ctfassets.net" in src):
            add_img(src, "Poster_PageAsset")

    return title, None, collected_images


def download_image(session: requests.Session, img_item: dict, target_folder: str) -> str | None:
    """Download a single image file to the destination folder."""
    url = img_item["url"]
    label = sanitize_filename(img_item["label"])

    try:
        r = session.get(url, timeout=25)
        r.raise_for_status()

        # Determine extension
        content_type = r.headers.get("content-type", "").lower()
        if "png" in content_type:
            ext = "png"
        elif "webp" in content_type:
            ext = "webp"
        else:
            ext = "jpg"

        filename = f"{label}.{ext}"
        filepath = os.path.join(target_folder, filename)

        # Avoid overwriting if duplicate name exists with different number
        counter = 1
        base_name = label
        while os.path.exists(filepath):
            filename = f"{base_name}_{counter}.{ext}"
            filepath = os.path.join(target_folder, filename)
            counter += 1

        with open(filepath, "wb") as f:
            f.write(r.content)

        file_size_kb = len(r.content) / 1024
        print(f"  [✓] Downloaded: {filename} ({file_size_kb:.1f} KB)")
        return filepath

    except Exception as e:
        print(f"  [✗] Failed to download {url}: {e}")
        return None


# ── Interactive CLI & Runner ─────────────────────────────────────────────────

def prompt_user_for_input() -> tuple[str, str | None, str, bool, bool]:
    """Interactive questions for the user when CLI arguments are omitted."""
    print("=" * 65)
    print("           NOW TV UK - POSTER & ARTWORK DOWNLOADER")
    print("=" * 65)

    # 1. URL input
    while True:
        url_input = input("\nEnter NowTV UK URL or Slug:\n> ").strip()
        if url_input:
            break
        print("[-] URL/Slug cannot be empty. Please try again.")

    # 2. Cookies input
    use_cookies_ans = input(
        "\nDo you want to use cookies? (y/n / path to cookies.txt / paste cookie string)\n[Press Enter to skip]: "
    ).strip()

    cookie_data = None
    if use_cookies_ans.lower() in ("y", "yes"):
        cookie_path = input("Enter path to cookie file (.txt / .json) or paste cookie string:\n> ").strip()
        if cookie_path:
            cookie_data = cookie_path
    elif use_cookies_ans.lower() not in ("n", "no", ""):
        cookie_data = use_cookies_ans

    # 3. Poster Mode Selection (Only Poster_Landscape_Cover_16x9 or All)
    print("\nPoster selection mode:")
    print("  [1] Only Poster_Landscape_Cover_16x9 (Default)")
    print("  [2] All Available Posters & Artwork (Landscape, Portrait, Backdrops, Logos)")
    mode_ans = input("Select mode [1/2] (Default: 1): ").strip()
    only_cover_169 = mode_ans != "2"

    # 4. Output directory
    out_dir_ans = input(f"\nEnter output directory [Default: {DEFAULT_OUTPUT_DIR}]: ").strip()
    out_dir = out_dir_ans if out_dir_ans else DEFAULT_OUTPUT_DIR

    # 5. Include episode stills? (only if downloading all)
    include_episodes = False
    if not only_cover_169:
        episodes_ans = input("\nInclude Season & Episode stills if TV show? (y/N) [Default: N]: ").strip().lower()
        include_episodes = episodes_ans in ("y", "yes")

    return url_input, cookie_data, out_dir, include_episodes, only_cover_169


def run(
    url_or_slug: str,
    cookies: str | None = None,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    include_episodes: bool = False,
    only_cover_169: bool = False,
    type_filter: str | None = None
) -> None:
    session = requests.Session()

    # Load cookies if provided
    if cookies:
        load_cookies(session, cookies)
    else:
        # Check if local cookies.txt exists in current directory as fallback convenience
        if os.path.isfile("nowtv.cookies.txt"):
            load_cookies(session, "nowtv.cookies.txt")
        elif os.path.isfile("cookies.txt"):
            load_cookies(session, "cookies.txt")

    slug = extract_slug(url_or_slug)
    print(f"\n[+] Processing Target: {url_or_slug}")
    print(f"[+] Extracted Slug: {slug}")

    title = None
    year = None
    images: list[dict] = []

    # Attempt 1: Calypso API
    try:
        print("[*] Fetching metadata and high-res poster links from NowTV UK API...")
        title, year, images = fetch_posters_from_api(session, slug, include_episodes=include_episodes)
    except Exception as e:
        print(f"[-] API fetch failed ({e}). Attempting webpage fallback...")
        if url_or_slug.startswith("http://") or url_or_slug.startswith("https://"):
            try:
                title, year, images = fetch_posters_from_webpage(session, url_or_slug)
            except Exception as web_err:
                print(f"[-] Webpage fallback also failed: {web_err}")
        else:
            full_url = f"https://www.nowtv.com/watch/asset{slug}"
            try:
                title, year, images = fetch_posters_from_webpage(session, full_url)
            except Exception as web_err:
                print(f"[-] Webpage fallback failed: {web_err}")

    if not images:
        print(f"\n[!] Error: No posters or artwork found for '{url_or_slug}'.")
        print("[!] Tips:")
        print("    - Make sure the URL or slug is valid, e.g.:")
        print("      https://www.nowtv.com/watch/asset/movies/bad-boys-ride-or-die/A5EK6sKrAayekc4h5w6Vs")
        print("      https://www.nowtv.com/watch/asset/tv/house-of-the-dragon/iYEQZ2rcf32XRKvQ5gm2Aq")
        print("    - If content requires authorization, provide a valid cookies.txt file.")
        return

    # Filter images if requested
    if only_cover_169:
        filtered = [
            img for img in images
            if img["label"] == "Poster_Landscape_Cover_16x9" or img.get("type") in ("titleArt169", "COVER_TITLE_WIDE")
        ]
        if not filtered:
            # Fallback to any landscape poster
            filtered = [img for img in images if "16x9" in img["label"] or img.get("type") == "landscape"]
        if filtered:
            images = filtered
            print(f"[+] Filtering mode: Selected only {images[0]['label']}")
    elif type_filter:
        filtered = [img for img in images if type_filter.lower() in img["label"].lower() or type_filter.lower() in img.get("type", "").lower()]
        if filtered:
            images = filtered
            print(f"[+] Filtering mode: Selected {len(images)} matching '{type_filter}'")

    # Folder naming: e.g. "Bad Boys - Ride Or Die (2024)"
    folder_name = title
    if year:
        folder_name += f" ({year})"
    folder_name = sanitize_filename(folder_name)

    target_folder = os.path.join(output_dir, folder_name)
    os.makedirs(target_folder, exist_ok=True)

    print(f"\n[+] Title: {title}" + (f" ({year})" if year else ""))
    print(f"[+] Found {len(images)} image(s) to download.")
    print(f"[+] Destination: {os.path.abspath(target_folder)}\n")

    print("[*] Downloading images:")
    downloaded_count = 0
    for idx, item in enumerate(images, start=1):
        print(f"[{idx}/{len(images)}] {item['category']} -> {item['label']}")
        res = download_image(session, item, target_folder)
        if res:
            downloaded_count += 1

    print("\n" + "=" * 65)
    print(f"[✓] Completed! Successfully downloaded {downloaded_count}/{len(images)} image(s).")
    print(f"[✓] Saved to: {os.path.abspath(target_folder)}")
    print("=" * 65)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch posters, backdrops, and artwork from NOW TV UK."
    )
    parser.add_argument(
        "-u", "--url",
        type=str,
        help="NOW TV UK URL or asset slug (e.g. https://www.nowtv.com/watch/asset/movies/...)"
    )
    parser.add_argument(
        "-c", "--cookies",
        type=str,
        help="Path to cookies file (.txt / .json) or raw cookie header string."
    )
    parser.add_argument(
        "-o", "--output-dir",
        type=str,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Target output directory for downloaded posters (Default: '{DEFAULT_OUTPUT_DIR}')."
    )
    parser.add_argument(
        "--cover-only", "--cover-169-only", "--only-cover-169",
        action="store_true",
        dest="cover_only",
        help="Download only Poster_Landscape_Cover_16x9."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Download all available posters and artwork (overrides default)."
    )
    parser.add_argument(
        "-t", "--type",
        type=str,
        default=None,
        help="Filter specific image type (e.g. Poster_Landscape_Cover_16x9, Poster_Portrait, Logo, etc.)."
    )
    parser.add_argument(
        "--all-episodes",
        action="store_true",
        help="Download artwork and stills for all seasons & episodes (for TV shows)."
    )

    args = parser.parse_args()

    if args.url:
        run(
            url_or_slug=args.url,
            cookies=args.cookies,
            output_dir=args.output_dir,
            include_episodes=args.all_episodes,
            only_cover_169=args.cover_only,
            type_filter=args.type
        )
    else:
        # Interactive mode
        url_input, cookie_data, out_dir, include_episodes, only_cover_169 = prompt_user_for_input()
        run(
            url_or_slug=url_input,
            cookies=cookie_data,
            output_dir=out_dir,
            include_episodes=include_episodes,
            only_cover_169=only_cover_169
        )


if __name__ == "__main__":
    main()
