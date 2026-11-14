#!/usr/bin/env python3
"""
STAGE.in New Releases Checker
Retrieves the latest movie and show releases on STAGE.in across Haryanvi, Rajasthani, Bhojpuri, Gujarati, Marathi, and Bengali.
Fetches direct MP4 raw master video links by default.
"""

import argparse
import csv
import json
import re
import sys
import requests
import os
import time
import datetime
from concurrent.futures import ThreadPoolExecutor

DIALECTS = {
    "haryanvi": "har",
    "rajasthani": "raj",
    "bhojpuri": "bho",
    "gujarati": "guj",
    "marathi": "mar",
    "bengali": "ben",
    "hindi": "hin",
    "punjabi": "pun"
}

API_URL_MOVIES = "https://stageapi.stage.in/nest/contents/movies/all"
API_URL_MOVIE_DETAILS = "https://stageapi.stage.in/nest/contents/movies/details"
API_URL_SHOW_DETAILS = "https://stageapi.stage.in/nest/contents/shows/details"

def init_ansi():
    """Enables ANSI escape sequence parsing on Windows consoles."""
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # STD_OUTPUT_HANDLE = -11
            hOut = kernel32.GetStdHandle(-11)
            dwMode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(hOut, ctypes.byref(dwMode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                kernel32.SetConsoleMode(hOut, dwMode.value | 0x0004)
        except Exception:
            pass

def safe_print(text):
    """Safely print Unicode text to stdout, falling back to replacement characters if needed."""
    try:
        enc = sys.stdout.encoding or "utf-8"
        sys.stdout.write(text.encode(enc, errors="replace").decode(enc) + "\n")
    except Exception:
        sys.stdout.write(text.encode("ascii", errors="replace").decode("ascii") + "\n")

def fetch_latest_movies(dialect_name, dialect_code, limit=10):
    """Fetches the latest movies for a given dialect using the STAGE.in API."""
    headers = {
        "app_id": "in.stage.web",
        "platform": "web",
        "os": "android",
        "api-version": "2",
        "dialect": dialect_code,
        "lang": "en"
    }
    
    movies = []
    page = 1
    per_page = min(50, limit)
    
    try:
        while len(movies) < limit:
            params = {
                "page": page,
                "perPage": per_page
            }
            resp = requests.get(API_URL_MOVIES, params=params, headers=headers, timeout=30)
            if resp.status_code != 200:
                print(f"Error fetching movies for {dialect_name} (status {resp.status_code}): {resp.text}", file=sys.stderr)
                break
                
            data = resp.json()
            page_data = data.get("data", [])
            if not page_data:
                break
                
            for m in page_data:
                if len(movies) >= limit:
                    break
                
                movie_id = m.get("_id")
                slug = m.get("slug", "")
                title_dict = m.get("title", {})
                title = title_dict.get("en") or title_dict.get("hi") or str(title_dict)
                
                # Construct default watch/details page URL (will be replaced by direct link)
                web_link = f"https://www.stage.in/en/{dialect_name}/movie/{slug}-{movie_id}"
                
                movies.append({
                    "id": movie_id,
                    "title": title,
                    "slug": slug,
                    "link": web_link,
                    "web_link": web_link,
                    "type": "movie",
                    "thumbnail": m.get("thumbnail")
                })
                
            if not data.get("nextPageAvailable"):
                break
                
            page += 1
            
    except Exception as e:
        print(f"Error connecting to movies API for {dialect_name}: {e}", file=sys.stderr)
        
    return movies

def fetch_latest_shows(dialect_name, limit=10):
    """Fetches the latest shows for a given dialect by scraping the STAGE.in shows page."""
    url = f"https://www.stage.in/en/{dialect_name}/shows"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    shows = []
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f"Error fetching shows page for {dialect_name} (status {resp.status_code})", file=sys.stderr)
            return []
            
        html = resp.text
        # Find all self.__next_f.push calls
        next_f_calls = re.findall(r'self\.__next_f\.push\(\[(.*?)\n?\]\)', html)
        
        for call in next_f_calls:
            m = re.match(r'^\d+,\s*"(.*)"$', call, re.DOTALL)
            if m:
                val = m.group(1)
                try:
                    val = json.loads('"' + val + '"')
                except Exception:
                    pass
                
                # Search for "items" list in this chunk
                items_matches = re.finditer(r'"items"\s*:\s*\[', val)
                for im in items_matches:
                    start = im.end() - 1
                    try:
                        arr, _ = json.JSONDecoder().raw_decode(val, start)
                        for item in arr:
                            if isinstance(item, dict) and item.get("type") == "show":
                                movie_id = item.get("_id")
                                slug = item.get("slug", "")
                                title = item.get("title", "")
                                
                                # Format watch/details page URL (will be replaced by direct link)
                                web_link = f"https://www.stage.in/en/{dialect_name}/show/{slug}"
                                
                                shows.append({
                                    "id": movie_id,
                                    "title": title,
                                    "slug": slug,
                                    "link": web_link,
                                    "web_link": web_link,
                                    "type": "show",
                                    "thumbnail": item.get("thumbnail"),
                                    "horizontalThumbnail": item.get("horizontalThumbnail"),
                                    "verticalThumbnail": item.get("verticalThumbnail")
                                })
                    except Exception:
                        pass
                        
        # Deduplicate shows
        unique_shows = {s["id"]: s for s in shows}.values()
        sorted_shows = sorted(unique_shows, key=lambda x: x["id"], reverse=True)
        return sorted_shows[:limit]
        
    except Exception as e:
        print(f"Error connecting to shows page for {dialect_name}: {e}", file=sys.stderr)
        
    return []

def format_duration(seconds):
    if not seconds:
        return ""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m:02d}m"
    elif m > 0:
        return f"{m}m {s:02d}s"
    else:
        return f"{s}s"

def fetch_direct_metadata(item, dialect_code, web_links_mode=False):
    """Queries Stage.in details API to extract direct video link and duration/length metadata."""
    headers = {
        "app_id": "in.stage.web",
        "platform": "web",
        "os": "android",
        "api-version": "2",
        "dialect": dialect_code,
        "lang": "en"
    }
    
    item_id = item["id"]
    item_type = item["type"]
    item["length"] = "N/A"
    
    try:
        if item_type == "movie":
            resp = requests.get(API_URL_MOVIE_DETAILS, params={"movieId": str(item_id)}, headers=headers, timeout=30)
            if resp.status_code == 200:
                movie = resp.json().get("movie", {})
                sp = movie.get("selectedPeripheral", {})
                
                # Extract duration
                duration = movie.get("duration")
                if duration:
                    item["duration"] = int(duration)
                    item["length"] = format_duration(int(duration))
                
                # Check top-level sourceLink first (full movie content)
                full_source = movie.get("sourceLink")
                if full_source and full_source.strip():
                    video_url = f"https://media.stage.in/episode/main-video/{full_source.strip()}"
                    if not web_links_mode:
                        item["link"] = video_url
                    return
                
                # Fallback to selectedPeripheral (preview/trailer)
                peripheral_source = (
                    sp.get("sourceLink") or 
                    sp.get("visionularHls", {}).get("sourceLink")
                )
                if peripheral_source and peripheral_source.strip() and not peripheral_source.strip().endswith(".m3u8"):
                    video_url = f"https://media.stage.in/episode/main-video/{peripheral_source.strip()}"
                    if not web_links_mode:
                        item["link"] = video_url
                    return
                    
        else:
            resp = requests.get(API_URL_SHOW_DETAILS, params={"showId": str(item_id), "seasonId": ""}, headers=headers, timeout=30)
            if resp.status_code == 200:
                show_data = resp.json().get("data", {})
                episodes = show_data.get("episodes", [])
                
                # Extract show duration and episode count
                season_duration = show_data.get("seasonDuration")
                total_episodes = show_data.get("totalEpisodes")
                eps_count = int(total_episodes) if total_episodes else len(episodes)
                
                if season_duration:
                    formatted_dur = format_duration(int(season_duration))
                    item["duration"] = int(season_duration)
                    item["length"] = f"{eps_count} Eps ({formatted_dur})"
                else:
                    item["length"] = f"{eps_count} Eps"
                
                if episodes:
                    first_ep = episodes[0]
                    sp = first_ep.get("selectedPeripheral", {})
                    
                    # Check top-level sourceLink first (full episode content)
                    full_source = first_ep.get("sourceLink")
                    if full_source and full_source.strip():
                        video_url = f"https://media.stage.in/episode/main-video/{full_source.strip()}"
                        if not web_links_mode:
                            item["link"] = video_url
                        return
                    
                    # Fallback to selectedPeripheral (preview/trailer)
                    peripheral_source = (
                        sp.get("sourceLink") or 
                        sp.get("visionularHls", {}).get("sourceLink")
                    )
                    if peripheral_source and peripheral_source.strip() and not peripheral_source.strip().endswith(".m3u8"):
                        video_url = f"https://media.stage.in/show/main-video/{peripheral_source.strip()}"
                        if not web_links_mode:
                            item["link"] = video_url
                        return
    except Exception:
        pass

def fetch_direct_metadata_concurrently(items, dialect_code, web_links_mode=False):
    """Fetches direct video links and metadata for a list of items concurrently to keep program fast."""
    def worker(item):
        fetch_direct_metadata(item, dialect_code, web_links_mode)
        return item
        
    with ThreadPoolExecutor(max_workers=20) as executor:
        list(executor.map(worker, items))

def find_timestamp_in_dict(d):
    if isinstance(d, dict):
        for v in d.values():
            ts = find_timestamp_in_dict(v)
            if ts:
                return ts
    elif isinstance(d, list):
        for v in d:
            ts = find_timestamp_in_dict(v)
            if ts:
                return ts
    elif isinstance(d, str):
        match = re.search(r'-(\d{13})\.', d) or re.search(r'-(\d{13})$', d)
        if match:
            return int(match.group(1)) // 1000
    return None

def extract_release_date(item):
    # 1. Try from direct video link
    video_url = item.get("link")
    if video_url and "media.stage.in" in video_url:
        filename = video_url.split("/")[-1].rsplit(".", 1)[0]
        match = re.search(r'-(\d{13})$', filename)
        if match:
            return int(match.group(1)) // 1000
        if len(filename) == 24 and all(c in "0123456789abcdefABCDEF" for c in filename):
            try:
                return int(filename[:8], 16)
            except ValueError:
                pass

    # 2. Fallback: Search the entire item metadata (like thumbnails) for a 13-digit timestamp
    ts = find_timestamp_in_dict(item)
    if ts:
        return ts
        
    return None
def extract_poster_url(item):
    thumbnail = item.get("thumbnail")
    if not thumbnail or not isinstance(thumbnail, dict):
        return None
    horizontal = thumbnail.get("horizontal")
    if not horizontal or not isinstance(horizontal, dict):
        return None
    ratio1 = horizontal.get("ratio1")
    if not ratio1 or not isinstance(ratio1, dict):
        return None
    source_link = ratio1.get("sourceLink")
    if not source_link or not isinstance(source_link, str) or not source_link.strip():
        return None
        
    source_link = source_link.strip()
    if source_link.startswith("episode-") or source_link.startswith("episodeImage-"):
        return f"https://media.stage.in/episode/horizontal/{source_link}"
    elif source_link.startswith("show-") or source_link.startswith("showImage-"):
        return f"https://media.stage.in/show/horizontal/{source_link}"
    
    if item.get("type") == "movie":
        return f"https://media.stage.in/episode/horizontal/{source_link}"
    else:
        return f"https://media.stage.in/show/horizontal/{source_link}"

def make_thumbnail_urls_absolute(thumbnail, item_type):
    if not isinstance(thumbnail, dict):
        return
    for key, value in thumbnail.items():
        if isinstance(value, dict):
            for ratio_key, ratio_val in value.items():
                if isinstance(ratio_val, dict) and "sourceLink" in ratio_val:
                    sl = ratio_val["sourceLink"]
                    if sl and isinstance(sl, str) and sl.strip() and not sl.strip().startswith(("http://", "https://")):
                        sl = sl.strip()
                        sub_path = key
                        entity = "episode" if item_type == "movie" else "show"
                        if sl.startswith(("episode-", "episodeImage-")):
                            entity = "episode"
                        elif sl.startswith(("show-", "showImage-")):
                            entity = "show"
                        ratio_val["sourceLink"] = f"https://media.stage.in/{entity}/{sub_path}/{sl}"

def print_table(results, use_color=True, web_links_mode=False):
    """Prints a beautiful formatted text table to the console."""
    if not results:
        safe_print("No content found.")
        return
        
    link_header = "Web Link" if web_links_mode else "Direct Video Link"
    headers = ["Dialect", "Type", "#", "Title", "ID", "Release Date", "Length", "Poster Link", link_header]
    col_widths = [12, 5, 3, 30, 8, 12, 10, 65, 65]
    
    # Calculate widths based on content to make it fit nicely
    for dialect, items in results.items():
        col_widths[0] = max(col_widths[0], len(dialect))
        for i, m in enumerate(items):
            col_widths[1] = max(col_widths[1], len(str(m.get("type", "movie"))))
            col_widths[3] = max(col_widths[3], len(str(m["title"])))
            col_widths[4] = max(col_widths[4], len(str(m["id"])))
            col_widths[5] = max(col_widths[5], len(str(m.get("release_date", "N/A"))))
            col_widths[6] = max(col_widths[6], len(str(m.get("length", "N/A"))))
            col_widths[7] = max(col_widths[7], len(str(m.get("poster", "N/A"))))
            col_widths[8] = max(col_widths[8], len(str(m["link"])))
            
    # ANSI escape codes
    COLOR_RESET = "\033[0m" if use_color else ""
    COLOR_BOLD = "\033[1m" if use_color else ""
    COLOR_GRAY = "\033[90m" if use_color else ""
    COLOR_WHITE = "\033[97m" if use_color else ""
    COLOR_YELLOW = "\033[93m" if use_color else ""
    COLOR_CYAN = "\033[96m" if use_color else ""
    COLOR_LINK = COLOR_WHITE
    
    DIALECT_COLORS = {
        "haryanvi": "\033[96m" if use_color else "",   # Cyan
        "rajasthani": "\033[93m" if use_color else "",  # Yellow
        "bhojpuri": "\033[92m" if use_color else "",    # Green
        "gujarati": "\033[95m" if use_color else "",    # Magenta
        "marathi": "\033[91m" if use_color else "",     # Red
        "bengali": "\033[94m" if use_color else "",     # Blue
        "hindi": "\033[97m" if use_color else "",       # White/Bright
        "punjabi": "\033[33m" if use_color else ""      # Bright Yellow/Orange
    }
    
    # Print header separator (ASCII format for cross-platform compatibility)
    border_top = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    border_mid = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    border_bot = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    
    # Apply gray color to borders
    colored_border_top = COLOR_GRAY + border_top + COLOR_RESET
    colored_border_mid = COLOR_GRAY + border_mid + COLOR_RESET
    colored_border_bot = COLOR_GRAY + border_bot + COLOR_RESET
    
    safe_print(colored_border_top)
    
    # Header row
    colored_headers = []
    for i, h in enumerate(headers):
        padded_h = f" {h.ljust(col_widths[i])} "
        colored_headers.append(COLOR_BOLD + COLOR_WHITE + padded_h + COLOR_RESET)
    header_line = COLOR_GRAY + "|" + COLOR_GRAY + "|".join(colored_headers) + COLOR_GRAY + "|" + COLOR_RESET
    safe_print(header_line)
    safe_print(colored_border_mid)
    
    first = True
    for dialect, items in results.items():
        if not first:
            # Add a visual separator between dialects
            safe_print(colored_border_mid)
        first = False
        
        dialect_color = DIALECT_COLORS.get(dialect, "")
        
        for idx, m in enumerate(items):
            d_cell = dialect if idx == 0 else ""
            type_cell = str(m.get("type", "movie"))
            idx_cell = str(idx + 1)
            
            # Format and pad content first, then apply color
            cell_d = f" {d_cell.ljust(col_widths[0])} "
            cell_type = f" {type_cell.ljust(col_widths[1])} "
            cell_idx = f" {idx_cell.rjust(col_widths[2])} "
            cell_title = f" {str(m['title']).ljust(col_widths[3])} "
            cell_id = f" {str(m['id']).ljust(col_widths[4])} "
            cell_release_date = f" {str(m.get('release_date', 'N/A')).ljust(col_widths[5])} "
            cell_length = f" {str(m.get('length', 'N/A')).ljust(col_widths[6])} "
            cell_poster = f" {str(m.get('poster', 'N/A')).ljust(col_widths[7])} "
            cell_link = f" {str(m['link']).ljust(col_widths[8])} "
            
            # Colors
            type_color = COLOR_CYAN if type_cell == "show" else COLOR_YELLOW
            
            colored_d = dialect_color + cell_d + COLOR_RESET
            colored_type = type_color + cell_type + COLOR_RESET
            colored_idx = COLOR_CYAN + cell_idx + COLOR_RESET
            colored_title = COLOR_WHITE + COLOR_BOLD + cell_title + COLOR_RESET
            colored_id = COLOR_YELLOW + cell_id + COLOR_RESET
            colored_release_date = COLOR_CYAN + cell_release_date + COLOR_RESET
            colored_length = COLOR_YELLOW + cell_length + COLOR_RESET
            colored_poster = COLOR_LINK + cell_poster + COLOR_RESET
            colored_link = COLOR_LINK + cell_link + COLOR_RESET
            
            line = (
                COLOR_GRAY + "|" + COLOR_RESET +
                colored_d +
                COLOR_GRAY + "|" + COLOR_RESET +
                colored_type +
                COLOR_GRAY + "|" + COLOR_RESET +
                colored_idx +
                COLOR_GRAY + "|" + COLOR_RESET +
                colored_title +
                COLOR_GRAY + "|" + COLOR_RESET +
                colored_id +
                COLOR_GRAY + "|" + COLOR_RESET +
                colored_release_date +
                COLOR_GRAY + "|" + COLOR_RESET +
                colored_length +
                COLOR_GRAY + "|" + COLOR_RESET +
                colored_poster +
                COLOR_GRAY + "|" + COLOR_RESET +
                colored_link +
                COLOR_GRAY + "|" + COLOR_RESET
            )
            safe_print(line)
            
    safe_print(colored_border_bot)

def save_csv(results, file_path):
    """Saves the results to a CSV file."""
    try:
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Dialect", "Type", "Rank", "Title", "ID", "Release Date", "Length", "Poster Link", "Video Link"])
            for dialect, items in results.items():
                for idx, m in enumerate(items):
                    writer.writerow([dialect, m.get("type", "movie"), idx + 1, m["title"], m["id"], m.get("release_date", "N/A"), m.get("length", "N/A"), m.get("poster", "N/A"), m["link"]])
        print(f"Results successfully saved to CSV: {file_path}")
    except Exception as e:
        print(f"Error saving to CSV: {e}", file=sys.stderr)

def save_json(results, file_path):
    """Saves the results to a JSON file."""
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"Results successfully saved to JSON: {file_path}")
    except Exception as e:
        print(f"Error saving to JSON: {e}", file=sys.stderr)

def main():
    init_ansi()
    
    parser = argparse.ArgumentParser(
        description="Check and list new movie and show releases from STAGE.in across multiple dialects with direct master links."
    )
    parser.add_argument(
        "-l", "--limit",
        type=int,
        default=10,
        help="Number of latest releases to fetch per dialect (default: 10)"
    )
    parser.add_argument(
        "-d", "--dialects",
        type=str,
        help=(
            f"Comma-separated list of dialects to check. "
            f"Available: {', '.join(DIALECTS.keys())} (default: all)"
        )
    )
    parser.add_argument(
        "-t", "--type",
        choices=["all", "movie", "show"],
        default="all",
        help="Type of content to query (default: all)"
    )
    parser.add_argument(
        "-w", "--web-links",
        action="store_true",
        help="Show standard web page browser links instead of direct video links"
    )
    parser.add_argument(
        "-f", "--format",
        choices=["table", "json", "csv"],
        default="table",
        help="Output format on console (default: table)"
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        help="Path to file to save the output results"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colorized terminal output"
    )
    parser.add_argument(
        "--days",
        type=int,
        help="Filter content to only show items released on STAGE in the last N days"
    )
    
    args = parser.parse_args()
    
    # Filter dialects based on arguments
    target_dialects = {}
    if args.dialects:
        selected = [d.strip().lower() for d in args.dialects.split(",")]
        for s in selected:
            if s in DIALECTS:
                target_dialects[s] = DIALECTS[s]
            else:
                print(f"Warning: Unknown dialect '{s}' ignored.", file=sys.stderr)
    else:
        target_dialects = DIALECTS
        
    if not target_dialects:
        print("Error: No valid dialects selected.", file=sys.stderr)
        sys.exit(1)
        
    # Check if we should use color
    use_color = sys.stdout.isatty() and not args.no_color
    
    current_time = int(time.time())
        
    print(f"Fetching latest releases from STAGE.in (type: {args.type}, limit: {args.limit} per dialect)...")
    results = {}
    for name, code in target_dialects.items():
        print(f"  Querying {name}...")
        combined = []
        
        # If filtering by days, we query more items (up to 100) to find matching release dates
        fetch_limit = max(args.limit, 100) if args.days else args.limit
        
        if args.type in ["all", "movie"]:
            movies = fetch_latest_movies(name, code, limit=fetch_limit)
            combined.extend(movies)
        if args.type in ["all", "show"]:
            shows = fetch_latest_shows(name, limit=fetch_limit)
            combined.extend(shows)
            
        # Sort combined by ID descending
        combined_sorted = sorted(combined, key=lambda x: x["id"], reverse=True)
        
        # Fetch direct video playback links and metadata concurrently for all candidates
        fetch_direct_metadata_concurrently(combined_sorted, code, args.web_links)
            
        # Extract precise release date (prioritizes direct video URL timestamp/ObjectId) and poster URL
        for item in combined_sorted:
            ts = extract_release_date(item)
            if ts:
                item["release_date"] = datetime.date.fromtimestamp(ts).isoformat()
                item["release_ts"] = ts
            else:
                item["release_date"] = "N/A"
                item["release_ts"] = None
                
            poster_url = extract_poster_url(item)
            item["poster"] = poster_url if poster_url else "N/A"
            
            if "thumbnail" in item:
                make_thumbnail_urls_absolute(item["thumbnail"], item["type"])
            
        # Filter by days if specified
        filtered = []
        for item in combined_sorted:
            ts = item.get("release_ts")
            if args.days:
                if ts:
                    age_seconds = current_time - ts
                    if age_seconds <= args.days * 86400:
                        filtered.append(item)
            else:
                filtered.append(item)
                
        # Sort by release timestamp descending, then by ID descending
        filtered_sorted = sorted(
            filtered,
            key=lambda x: (
                x.get("release_ts") or 0,
                int(x.get("id")) if str(x.get("id")).isdigit() else 0
            ),
            reverse=True
        )
        
        # Limit to the requested output limit
        filtered = filtered_sorted[:args.limit]
            
        results[name] = filtered
        
    print("\n--- STAGE.in Latest Movie and Show Releases ---")
    if args.format == "table":
        print_table(results, use_color=use_color, web_links_mode=args.web_links)
    elif args.format == "json":
        print(json.dumps(results, indent=2, ensure_ascii=False))
    elif args.format == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(["Dialect", "Type", "Rank", "Title", "ID", "Release Date", "Length", "Poster Link", "Video Link"])
        for dialect, items in results.items():
            for idx, m in enumerate(items):
                writer.writerow([dialect, m.get("type", "movie"), idx + 1, m["title"], m["id"], m.get("release_date", "N/A"), m.get("length", "N/A"), m.get("poster", "N/A"), m["link"]])
                
    # Save output to file if requested
    if args.output:
        if args.output.lower().endswith(".json"):
            save_json(results, args.output)
        elif args.output.lower().endswith(".csv"):
            save_csv(results, args.output)
        else:
            # Default to text representation of table (never use colors in files)
            try:
                import io
                f_io = io.StringIO()
                old_stdout = sys.stdout
                sys.stdout = f_io
                print_table(results, use_color=False, web_links_mode=args.web_links)
                sys.stdout = old_stdout
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(f_io.getvalue())
                print(f"Results successfully saved to text file: {args.output}")
            except Exception as e:
                print(f"Error saving to text file: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
