"""
KableOne Poster Downloader
===========================
Fetches all movie/show poster images (both Portrait and Landscape/Backdrop)
from any given kableone.com page URL.

Supports:
  • Individual content pages (e.g., https://www.kableone.com/Watch/Content/592)
  • Category & listing pages (e.g., /Movies/Index, /Webseries/Index)
  • Homepage (e.g., /Home/Index)

Usage:
    python kableone_poster_downloader.py

Dependencies:
    pip install requests beautifulsoup4
"""

import os
import re
import sys
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

# Ensure UTF-8 output encoding for Windows command prompts
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ── Configuration ────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DOWNLOAD_DIR = "kableone_posters"

# Ignored image patterns (static UI elements, logos, tracking pixels)
IGNORED_IMAGE_PATTERNS = [
    r"logo",
    r"fab_black",
    r"whatsapp",
    r"facebook\.com",
    r"googletagmanager",
    r"unsplash\.com",
]


def sanitize_filename(name: str) -> str:
    """Remove or replace characters that are invalid in filenames."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(". ")
    return name or "untitled"


def is_ignored_image(url: str) -> bool:
    """Check if the image URL is a static UI asset or tracking pixel."""
    if not url:
        return True
    url_lower = url.lower()
    for pattern in IGNORED_IMAGE_PATTERNS:
        if re.search(pattern, url_lower):
            return True
    return False


def clean_page_title(title: str) -> str:
    """Clean a page title string (e.g., 'Watch Shera only on KableOne' -> 'Shera')."""
    if not title:
        return ""
    cleaned = title
    cleaned = re.sub(r"^Watch\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+only\s+on\s+KableOne.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*\|\s*KableOne.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*\|\s*Home of Punjabi OTT.*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def fetch_page(url: str) -> BeautifulSoup:
    """Download the page HTML and return a BeautifulSoup object."""
    print(f"\n[+] Fetching page: {url}")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def extract_posters(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """
    Extract all poster items (both Landscape & Portrait) from any KableOne page.

    Returns a list of dicts:
      {
        "title": str,
        "url": str,
        "orientation": "Landscape" | "Portrait" | "Banner",
        "genre": str,
        "year": str,
      }
    """
    posters = []
    seen_urls = set()

    def add_poster(title: str, img_url: str, orientation: str, genre: str = "", year: str = ""):
        if not img_url or is_ignored_image(img_url):
            return
        full_url = urljoin(page_url, img_url)
        if full_url in seen_urls:
            return
        seen_urls.add(full_url)
        posters.append({
            "title": title,
            "url": full_url,
            "orientation": orientation,
            "genre": genre,
            "year": year,
        })

    # ── 1. Movie cards (<article class="movie-card">) on Listing/Home pages
    articles = soup.find_all("article", class_="movie-card")
    for article in articles:
        title = article.get("data-title", "").strip() or f"movie_{len(posters) + 1}"
        genre = article.get("data-genre", "").strip()
        year = article.get("data-year", "").strip()

        # Landscape / Backdrop (1920x1080)
        backdrop = article.get("data-image", "").strip()
        if backdrop:
            add_poster(title, backdrop, orientation="Landscape", genre=genre, year=year)

        # Portrait Thumbnail (303x430 / 500x1006)
        img = article.find("img", class_="movie-card__image") or article.find("img")
        if img:
            src = img.get("src", "").strip() or img.get("data-src", "").strip()
            add_poster(title, src, orientation="Portrait", genre=genre, year=year)

    # ── 2. Single Content / Movie Pages (/Watch/Content/...) ─────────────
    # Extract Title
    content_title = ""
    og_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "og:title"})
    if og_title and og_title.get("content"):
        content_title = clean_page_title(og_title.get("content", "").strip())
    elif soup.title and soup.title.string:
        content_title = clean_page_title(soup.title.string.strip())

    if not content_title:
        content_title = "KableOne_Content"

    # A. Search for Landscape / Backdrop data attributes (e.g. data-image on figure/a/button)
    elements_with_data_image = soup.find_all(lambda t: t.has_attr("data-image") and t.get("data-image"))
    for el in elements_with_data_image:
        img_src = el.get("data-image", "").strip()
        el_title = el.get("data-title", "").strip() or content_title
        add_poster(el_title, img_src, orientation="Landscape")

    # B. Search for Portrait in OpenGraph / Twitter meta tags
    og_image = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
    twitter_image = soup.find("meta", attrs={"name": "twitter:image"})

    if og_image and og_image.get("content"):
        add_poster(content_title, og_image.get("content", "").strip(), orientation="Portrait")
    elif twitter_image and twitter_image.get("content"):
        add_poster(content_title, twitter_image.get("content", "").strip(), orientation="Portrait")

    # C. Search for Poster img tags inside content pages
    for img in soup.find_all("img"):
        src = img.get("src", "").strip() or img.get("data-src", "").strip()
        if not src or is_ignored_image(src):
            continue
        if "/Media/" in src:
            alt_text = img.get("alt", "").strip()
            title = clean_page_title(alt_text) if alt_text else content_title
            # Check if filename indicates 1920 / landscape or 303/500 / portrait
            if "1920" in src or "1080" in src or "banner" in src.lower():
                add_poster(title, src, orientation="Landscape")
            else:
                add_poster(title, src, orientation="Portrait")

    # ── 3. Hero Banner images (Homepage/Movies page banners) ─────────────
    banners = soup.find_all("img", attrs={"data-banner-image": True})
    for img in banners:
        src = img.get("src", "").strip() or img.get("data-src", "").strip()
        if not src or is_ignored_image(src):
            continue
        fname = os.path.splitext(os.path.basename(urlparse(src).path))[0]
        add_poster(f"banner_{fname}", src, orientation="Banner", genre="Banner")

    # ── 4. Fallback: Any other Media images ──────────────────────────────
    if not posters:
        for img in soup.find_all("img"):
            src = img.get("src", "").strip() or img.get("data-src", "").strip()
            if not src or is_ignored_image(src):
                continue
            if "images.kableone.com" in src or "/Media/" in src or "/Banner/" in src:
                fname = os.path.splitext(os.path.basename(urlparse(src).path))[0]
                add_poster(fname, src, orientation="Landscape" if "1920" in src else "Portrait")

    return posters


def download_image(url: str, filepath: str) -> bool:
    """Download a single image to the given filepath. Returns True on success."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30, stream=True)
        resp.raise_for_status()

        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True

    except requests.RequestException as exc:
        print(f"   [!] Failed: {exc}")
        return False


def download_posters(posters: list[dict], output_dir: str, selected_orientations: set[str]) -> tuple[int, int]:
    """
    Download poster images into `output_dir` filtered by orientation.
    Returns (success_count, failure_count).
    """
    os.makedirs(output_dir, exist_ok=True)

    items_to_download = [p for p in posters if p["orientation"] in selected_orientations]
    if not items_to_download:
        print("\n[-] No posters match the selected filter.")
        return 0, 0

    success = 0
    failed = 0
    total = len(items_to_download)

    for i, item in enumerate(items_to_download, 1):
        title = item["title"]
        orientation = item["orientation"]
        safe_title = sanitize_filename(title)

        poster_url = item["url"]
        ext = os.path.splitext(urlparse(poster_url).path)[1] or ".jpg"

        # Suffix with orientation for clear file naming
        tag = f"_{orientation.lower()}" if orientation in ("Landscape", "Portrait") else ""
        filename = f"{safe_title}{tag}{ext}"
        filepath = os.path.join(output_dir, filename)

        counter = 1
        while os.path.exists(filepath):
            filename = f"{safe_title}{tag}_{counter}{ext}"
            filepath = os.path.join(output_dir, filename)
            counter += 1

        year_str = f" ({item['year']})" if item.get("year") else ""
        genre_str = f" [{item['genre']}]" if item.get("genre") and orientation != "Banner" else ""

        print(f"\n[{i}/{total}] [{orientation}] {title}{year_str}{genre_str}")
        print(f"   Link: {poster_url}")
        print(f"   Save: {filepath}")

        if download_image(poster_url, filepath):
            size_kb = os.path.getsize(filepath) / 1024
            print(f"   [OK] Downloaded ({size_kb:.1f} KB)")
            success += 1
        else:
            failed += 1

        time.sleep(0.2)

    return success, failed


def print_poster_list(posters: list[dict]) -> None:
    """Pretty-print the extracted poster list categorized by orientation."""
    print(f"\n{'-' * 70}")
    print(f"  Found {len(posters)} poster item(s)")
    print(f"{'-' * 70}")

    landscapes = [p for p in posters if p["orientation"] == "Landscape"]
    portraits = [p for p in posters if p["orientation"] == "Portrait"]
    banners = [p for p in posters if p["orientation"] == "Banner"]

    if landscapes:
        print(f"\n  [Landscape Posters / Backdrops 16:9]: {len(landscapes)}")
        for i, p in enumerate(landscapes, 1):
            year = f" ({p['year']})" if p.get("year") else ""
            print(f"     {i:3}. {p['title']}{year}")
            print(f"          {p['url']}")

    if portraits:
        print(f"\n  [Portrait Posters / Vertical 2:3]: {len(portraits)}")
        for i, p in enumerate(portraits, 1):
            year = f" ({p['year']})" if p.get("year") else ""
            print(f"     {i:3}. {p['title']}{year}")
            print(f"          {p['url']}")

    if banners:
        print(f"\n  [Hero Banners]: {len(banners)}")
        for i, p in enumerate(banners, 1):
            print(f"     {i:3}. {p['title']}")
            print(f"          {p['url']}")

    print(f"{'-' * 70}")


def main():
    print("=" * 60)
    print("  KableOne Poster Downloader (Portrait + Landscape)")
    print("=" * 60)
    print()
    print("Enter any URL from kableone.com to download posters.")
    print("Examples:")
    print("  • Single Content: https://www.kableone.com/Watch/Content/592")
    print("  • Movies Page:   https://www.kableone.com/Movies/Index")
    print("  • TV Shows Page: https://www.kableone.com/Webseries/Index")
    print("  • Homepage:      https://www.kableone.com/Home/Index")
    print()

    url = input("Enter KableOne URL: ").strip()

    if not url:
        print("[-] No URL provided. Exiting.")
        sys.exit(1)

    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)

    if "kableone.com" not in parsed.netloc:
        print("[!] Warning: This doesn't look like a kableone.com URL.")
        proceed = input("   Continue anyway? (y/n): ").strip().lower()
        if proceed != "y":
            print("Exiting.")
            sys.exit(0)

    try:
        soup = fetch_page(url)
    except requests.RequestException as exc:
        print(f"\n[-] Failed to fetch page: {exc}")
        sys.exit(1)

    posters = extract_posters(soup, url)

    if not posters:
        print("\n[-] No posters found on this page.")
        sys.exit(0)

    print_poster_list(posters)

    # ── Choose which formats to download ─────────────────────────────────
    has_landscape = any(p["orientation"] == "Landscape" for p in posters)
    has_portrait = any(p["orientation"] == "Portrait" for p in posters)
    has_banner = any(p["orientation"] == "Banner" for p in posters)

    print("\nSelect which posters to download:")
    print("  1. All (Landscape + Portrait + Banners) [Default]")
    print("  2. Landscape only (16:9 backdrops/posters)")
    print("  3. Portrait only (Vertical posters)")
    
    choice = input("Enter choice (1/2/3) [1]: ").strip()
    if choice == "2":
        selected_orientations = {"Landscape"}
    elif choice == "3":
        selected_orientations = {"Portrait"}
    else:
        selected_orientations = {"Landscape", "Portrait", "Banner"}

    # ── Choose output directory ──────────────────────────────────────────
    page_name = parsed.path.strip("/").replace("/", "_") or "home"
    default_dir = os.path.join(DOWNLOAD_DIR, sanitize_filename(page_name))

    dir_input = input(f"\nOutput directory [{default_dir}]: ").strip()
    output_dir = dir_input if dir_input else default_dir

    # ── Download ─────────────────────────────────────────────────────────
    print(f"\n[+] Starting download to: {output_dir}")

    success, failed = download_posters(posters, output_dir, selected_orientations)

    # ── Summary ──────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"  [OK] Download complete!")
    print(f"  Summary: Success: {success} | Failed: {failed}")
    print(f"  Saved to: {os.path.abspath(output_dir)}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
