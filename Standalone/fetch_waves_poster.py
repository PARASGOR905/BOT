#!/usr/bin/env python3
"""
Wavespb Poster & Artwork Downloader

Downloads and categorizes movie and series posters/artworks from Wavespb.com into organized folders:
    posters/
    ├── movies/
    │   └── Yamuna_Putra_Angad/
    │       ├── poster.webp
    │       ├── thumbnail.webp
    │       ├── detail_banner.webp
    │       └── metadata.json
    └── series/
        └── Series_Name/
            ├── poster.webp
            └── ...

Usage:
    python fetch_waves_poster.py [URL_OR_ID ...] [OPTIONS]

Examples:
    python fetch_waves_poster.py https://www.wavespb.com/movies/yamuna-putra-angad/28378
    python fetch_waves_poster.py 28378 28373 28384 --all-artwork
    python fetch_waves_poster.py --batch urls.txt --root-dir my_posters
"""

import sys
import os
import re
import json
import argparse
import urllib.request
import urllib.parse
from pathlib import Path

API_URL = "https://www.wavespb.com/web/dashboard/ajax_data_details"
DEFAULT_URL = "https://www.wavespb.com/movies/yamuna-putra-angad/28378"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def extract_movie_id(url_or_id: str) -> str:
    """Extracts numeric content ID from a Wavespb URL or ID string."""
    url_or_id = url_or_id.strip()
    if url_or_id.isdigit():
        return url_or_id
    
    # Match trailing numeric ID in path (e.g., /movies/yamuna-putra-angad/28378)
    match = re.search(r'/(\d+)(?:[/?#]|$)', url_or_id)
    if match:
        return match.group(1)
    
    # Fallback search for any digits in string
    numbers = re.findall(r'\d+', url_or_id)
    if numbers:
        return numbers[-1]
    
    raise ValueError(f"Could not extract numeric content ID from '{url_or_id}'")


def sanitize_name(name: str) -> str:
    """Sanitizes a string to be used safely as folder or file name."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r'\s+', "_", name.strip())
    return name or "unnamed_content"


def determine_category_folder(details: dict, url_or_id: str = "") -> str:
    """Determines subfolder category name ('movies', 'series', 'shorts', etc.)."""
    cat_title = details.get("category_title", "").strip().lower()
    
    if "movie" in cat_title or "/movies/" in url_or_id.lower():
        return "movies"
    elif "series" in cat_title or "show" in cat_title or "tv" in cat_title or details.get("season") or (details.get("season_count", 0) > 1):
        return "series"
    elif "short" in cat_title:
        return "shorts"
    elif cat_title:
        return sanitize_name(cat_title.lower())
    else:
        return "general"


def fetch_metadata(content_id: str) -> dict:
    """Fetches metadata from Wavespb API endpoint."""
    payload = urllib.parse.urlencode({"id": content_id}).encode('utf-8')
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.wavespb.com/movies/content/{content_id}"
    }

    req = urllib.request.Request(API_URL, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if data.get("status") and data.get("data"):
                return data["data"]
            else:
                msg = data.get("message") or "API returned empty response or invalid status"
                raise RuntimeError(f"API error for ID {content_id}: {msg}")
    except Exception as e:
        raise RuntimeError(f"Failed to fetch metadata for ID {content_id}: {e}")


def download_file(image_url: str, save_path: Path) -> bool:
    """Downloads an image file from URL and saves it locally."""
    headers = {"User-Agent": USER_AGENT}
    req = urllib.request.Request(image_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp, open(save_path, "wb") as f:
            f.write(resp.read())
        return True
    except Exception as e:
        print(f"    [-] Error downloading {image_url}: {e}")
        return False


def process_item(url_or_id: str, root_dir: Path, download_all: bool = True, save_meta: bool = True) -> bool:
    """Fetches details and downloads posters into organized subfolders."""
    try:
        content_id = extract_movie_id(url_or_id)
        print(f"\n[*] Processing ID: {content_id} ({url_or_id})")
        
        details = fetch_metadata(content_id)
        title = details.get("title") or f"Item_{content_id}"
        cat_folder = determine_category_folder(details, url_or_id)
        
        item_folder_name = sanitize_name(title)
        target_dir = root_dir / cat_folder / item_folder_name
        target_dir.mkdir(parents=True, exist_ok=True)
        
        print(f"    Title: '{title}'")
        print(f"    Category: '{cat_folder.upper()}' -> Directory: {target_dir}")
        if details.get("genres"):
            print(f"    Genres: {details['genres']}")
        if details.get("language_title"):
            print(f"    Language: {details['language_title']}")

        # Save metadata JSON file
        if save_meta:
            meta_path = target_dir / "metadata.json"
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(details, f, indent=2, ensure_ascii=False)
            print(f"    [+] Saved metadata to {meta_path.name}")

        artwork_map = {
            "poster": details.get("poster_url"),
            "thumbnail": details.get("thumbnail"),
            "detail_banner": details.get("detail_banner"),
            "banner_icon": details.get("banner_icon")
        }

        downloaded_count = 0

        if download_all:
            for art_type, art_url in artwork_map.items():
                if art_url:
                    ext = Path(urllib.parse.urlparse(art_url).path).suffix or ".webp"
                    save_path = target_dir / f"{art_type}{ext}"
                    print(f"    [*] Downloading {art_type}...")
                    if download_file(art_url, save_path):
                        print(f"    [+] Saved {art_type} -> {save_path.name}")
                        downloaded_count += 1
        else:
            main_url = details.get("poster_url") or details.get("thumbnail") or details.get("detail_banner")
            if main_url:
                ext = Path(urllib.parse.urlparse(main_url).path).suffix or ".webp"
                save_path = target_dir / f"poster{ext}"
                print(f"    [*] Downloading poster...")
                if download_file(main_url, save_path):
                    print(f"    [+] Saved poster -> {save_path.name}")
                    downloaded_count += 1
            else:
                print("    [-] No poster URL found.")

        return downloaded_count > 0

    except Exception as err:
        print(f"    [!] Error: {err}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Download and categorize Wavespb movie and series posters into subfolders."
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="Wavespb URLs or numeric Content IDs (Defaults to Yamuna Putra Angad if empty)"
    )
    parser.add_argument(
        "-b", "--batch",
        help="Path to a text file containing URLs or IDs (one per line)"
    )
    parser.add_argument(
        "-r", "--root-dir",
        default="posters",
        help="Root output directory (Default: 'posters')"
    )
    parser.add_argument(
        "--poster-only",
        action="store_true",
        help="Download main poster only (by default, downloads poster, thumbnail, banner, etc.)"
    )
    parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="Do not save metadata.json file in each subfolder"
    )

    args = parser.parse_args()

    targets = list(args.targets)
    if args.batch:
        batch_path = Path(args.batch)
        if batch_path.exists():
            with open(batch_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        targets.append(line)
        else:
            print(f"[!] Batch file not found: {args.batch}")
            sys.exit(1)

    if not targets:
        targets = [DEFAULT_URL]

    root_dir = Path(args.root_dir)
    print(f"==================================================")
    print(f" Wavespb Poster Downloader")
    print(f" Target Directory: {root_dir.resolve()}")
    print(f" Total items to process: {len(targets)}")
    print(f"==================================================")

    success_count = 0
    for target in targets:
        if process_item(
            url_or_id=target,
            root_dir=root_dir,
            download_all=not args.poster_only,
            save_meta=not args.no_metadata
        ):
            success_count += 1

    print(f"\n==================================================")
    print(f" Done! Successfully processed {success_count}/{len(targets)} item(s).")
    print(f" Posters saved in: {root_dir.resolve()}")
    print(f"==================================================")


if __name__ == "__main__":
    main()
