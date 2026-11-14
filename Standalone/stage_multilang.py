#!/usr/bin/env python3
"""
STAGE Cross-Dialect Multi-Language Audio Finder & Muxer
Discovers all available dialect/language versions (Haryanvi, Rajasthani, Hindi, Bhojpuri, Gujarati, Marathi, Bengali, Punjabi)
for any STAGE movie or show, displays all available languages, and generates ffmpeg muxing instructions / direct links.
"""

import sys
import re
import json
import requests
from concurrent.futures import ThreadPoolExecutor

ALL_DIALECTS = {
    'haryanvi': 'har',
    'rajasthani': 'raj',
    'bhojpuri': 'bho',
    'gujarati': 'guj',
    'marathi': 'mar',
    'bengali': 'ben',
    'hindi': 'hin',
    'punjabi': 'pun'
}

DIALECT_NAMES = {
    'har': 'Haryanvi',
    'raj': 'Rajasthani',
    'bho': 'Bhojpuri',
    'guj': 'Gujarati',
    'mar': 'Marathi',
    'ben': 'Bengali',
    'hin': 'Hindi',
    'pun': 'Punjabi'
}

ISO_LANG_CODES = {
    'har': 'bgc',
    'raj': 'raj',
    'bho': 'bho',
    'guj': 'guj',
    'mar': 'mar',
    'ben': 'ben',
    'hin': 'hin',
    'pun': 'pan'
}

MEDIA_BASE = "https://media.stage.in/episode/main-video"

def get_headers(dialect_code: str) -> dict:
    return {
        'app_id': 'in.stage.web',
        'platform': 'web',
        'os': 'android',
        'api-version': '2',
        'dialect': dialect_code,
        'lang': 'en',
        'User-Agent': 'Mozilla/5.0'
    }

def fetch_details_by_id(content_id: str, content_type: str, dialect_code: str):
    headers = get_headers(dialect_code)
    try:
        if content_type == 'movie':
            url = "https://stageapi.stage.in/nest/contents/movies/details"
            resp = requests.get(url, params={'movieId': content_id}, headers=headers, timeout=10)
            if resp.status_code == 200:
                movie = resp.json().get('movie') or {}
                if movie.get('title'):
                    return movie
        else:
            url = "https://stageapi.stage.in/nest/contents/shows/details"
            resp = requests.get(url, params={'showId': content_id, 'seasonId': ''}, headers=headers, timeout=10)
            if resp.status_code == 200:
                show = resp.json().get('data') or {}
                if show.get('title'):
                    return show
    except Exception:
        pass
    return None

def search_dialect_by_query(query: str, dialect_name: str, dialect_code: str, content_type: str):
    headers = get_headers(dialect_code)
    clean_query = query.lower().replace('-', ' ').strip()
    try:
        url = "https://stageapi.stage.in/nest/contents/movies/all" if content_type == 'movie' else f"https://www.stage.in/en/{dialect_name}"
        params = {'page': 1, 'perPage': 50}
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code == 200:
            items = resp.json().get('data', [])
            for item in items:
                title_obj = item.get('title', {})
                title_str = (title_obj.get('en') or title_obj.get('hi') or str(title_obj)).lower()
                slug = (item.get('slug') or '').lower()
                if clean_query in title_str or clean_query in slug or slug.startswith(clean_query):
                    return item
    except Exception:
        pass
    return None

def extract_video_url(item: dict) -> str:
    source = item.get('sourceLink')
    if not source:
        sp = item.get('selectedPeripheral') or {}
        source = sp.get('sourceLink') or (sp.get('visionularHls') or {}).get('sourceLink')
    if source and str(source).strip():
        s = str(source).strip()
        return s if s.startswith('http') else f"{MEDIA_BASE}/{s}"
    return ""

def find_all_language_versions(input_str: str):
    print("=" * 80)
    print("      STAGE CROSS-DIALECT MULTI-LANGUAGE AUDIO DISCOVERY")
    print("=" * 80)

    # Extract ID and dialect from input
    dialect_found = "har"
    for d_name, d_code in ALL_DIALECTS.items():
        if d_name in input_str.lower() or d_code == input_str.lower():
            dialect_found = d_code
            break

    match = re.search(r'(\d+)(?:/|\?|$)', input_str)
    content_id = match.group(1) if match else "".join(filter(str.isdigit, input_str))
    content_type = "show" if "show" in input_str.lower() else "movie"

    print(f"\n[*] Processing Input: '{input_str}'")
    print(f"[*] Primary Dialect: {DIALECT_NAMES.get(dialect_found, dialect_found)} ({dialect_found}) | ID: {content_id}")

    primary_data = fetch_details_by_id(content_id, content_type, dialect_found) if content_id else None
    
    title_name = ""
    base_slug = ""
    if primary_data:
        t_obj = primary_data.get('title')
        title_name = t_obj.get('en') if isinstance(t_obj, dict) else str(t_obj or '')
        base_slug = primary_data.get('slug', '').rsplit('-', 1)[0]
    
    if not title_name:
        title_name = input_str

    print(f"[+] Title Identified: '{title_name}'")

    available_versions = {}

    # Check 1: API dialectMapping
    mapping = {}
    if primary_data:
        mapping = primary_data.get('dialectMapping') or primary_data.get('dialactMapping') or {}

    if mapping:
        print(f"[+] Found direct dialect mapping in API response: {list(mapping.keys())}")
        for code, info in mapping.items():
            ep_id = str(info.get('episodeId') or info.get('movieId') or info.get('id') or '')
            if ep_id:
                details = fetch_details_by_id(ep_id, content_type, code)
                if details:
                    v_url = extract_video_url(details)
                    if v_url:
                        available_versions[code] = {
                            'dialect_code': code,
                            'language_name': DIALECT_NAMES.get(code, code),
                            'id': ep_id,
                            'title': details.get('title'),
                            'video_url': v_url
                        }

    # Check 2: Cross-Search all other dialects for missing languages
    missing_codes = [c for c in ALL_DIALECTS.values() if c not in available_versions]
    if missing_codes:
        print(f"[*] Searching remaining dialects ({', '.join(missing_codes)}) for matching title/slug...")
        
        def scan_code(code):
            name = DIALECT_NAMES.get(code, code).lower()
            match_item = search_dialect_by_query(title_name or base_slug, name, code, content_type)
            if match_item:
                item_id = str(match_item.get('_id') or match_item.get('id'))
                details = fetch_details_by_id(item_id, content_type, code) or match_item
                v_url = extract_video_url(details)
                if v_url:
                    return code, {
                        'dialect_code': code,
                        'language_name': DIALECT_NAMES.get(code, code),
                        'id': item_id,
                        'title': details.get('title'),
                        'video_url': v_url
                    }
            return code, None

        with ThreadPoolExecutor(max_workers=len(missing_codes)) as executor:
            results = executor.map(scan_code, missing_codes)
            for code, res in results:
                if res:
                    available_versions[code] = res

    print("\n" + "=" * 80)
    print(f"  ALL AVAILABLE LANGUAGES FOUND FOR '{title_name}': {len(available_versions)}")
    print("=" * 80)

    audio_inputs = []
    metadata_flags = []

    for idx, (code, v) in enumerate(available_versions.items()):
        lang_label = v['language_name']
        iso_code = ISO_LANG_CODES.get(code, code)
        is_primary = (code == dialect_found)
        tag = " [PRIMARY VIDEO]" if is_primary else ""
        print(f"  [{idx+1}] {lang_label:<12} (code: {code}) | Content ID: {v['id']} | URL: {v['video_url']}{tag}")
        
        audio_inputs.append(f"-i \"{v['video_url']}\"")
        metadata_flags.append(f"-map {idx}:a -metadata:s:a:{idx} language={iso_code} -metadata:s:a:{idx} title=\"{lang_label}\"")

    if available_versions:
        output_filename = f"{re.sub(r'[^\w\s-]', '', title_name).strip().replace(' ', '_')}_MultiAudio.mkv"
        
        print("\n" + "=" * 80)
        print("  GENERATED FFMPEG COMMAND FOR MULTI-AUDIO MUXING:")
        print("=" * 80)
        first_input = list(available_versions.values())[0]['video_url']
        inputs_str = " ".join([f"-i \"{v['video_url']}\"" for v in available_versions.values()])
        maps_str = " -map 0:v " + " ".join([f"-map {idx}:a" for idx in range(len(available_versions))])
        meta_str = " ".join([f"-metadata:s:a:{idx} language={ISO_LANG_CODES.get(code, code)} -metadata:s:a:{idx} title=\"{v['language_name']}\"" for idx, (code, v) in enumerate(available_versions.items())])
        
        ffmpeg_cmd = f"ffmpeg {inputs_str}{maps_str} -c copy -shortest {meta_str} \"{output_filename}\""
        print(ffmpeg_cmd)
        print("\n[*] Note: '-shortest' ensures the video duration dictates output length even if audio tracks have small playtime differences.")
        print("=" * 80)

if __name__ == '__main__':
    user_in = sys.argv[1] if len(sys.argv) > 1 else "24879"
    find_all_language_versions(user_in)
