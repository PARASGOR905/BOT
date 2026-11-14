#!/usr/bin/env python3
"""
STAGE.in Complete Catalog JSON Exporter
Fetches ALL movies, shows, episodes, and direct raw master MP4 video links for a selected language/URL.
"""

import os
import re
import sys
import json
import time
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

DIALECT_MAP = {
    'haryanvi': 'har',
    'rajasthani': 'raj',
    'bhojpuri': 'bho',
    'gujarati': 'guj',
    'marathi': 'mar',
    'bengali': 'ben',
    'hindi': 'hin',
    'punjabi': 'pun',
    'har': 'har',
    'raj': 'raj',
    'bho': 'bho',
    'guj': 'guj',
    'mar': 'mar',
    'ben': 'ben',
    'hin': 'hin',
    'pun': 'pun',
}

DIALECT_NAMES = {
    'har': 'haryanvi',
    'raj': 'rajasthani',
    'bho': 'bhojpuri',
    'guj': 'gujarati',
    'mar': 'marathi',
    'ben': 'bengali',
    'hin': 'hindi',
    'pun': 'punjabi',
}

API_BASE = 'https://stageapi.stage.in'
API_MOVIES_ALL = f'{API_BASE}/nest/contents/movies/all'
API_MOVIE_DETAILS = f'{API_BASE}/nest/contents/movies/details'
API_SHOW_DETAILS = f'{API_BASE}/nest/contents/shows/details'
MEDIA_BASE = 'https://media.stage.in/episode/main-video'

def parse_input_language(user_input: str) -> tuple[str, str]:
    text = user_input.strip().lower()
    if 'stage.in' in text:
        parsed = urlparse(text)
        parts = [p for p in parsed.path.strip('/').split('/') if p]
        for part in parts:
            if part in DIALECT_MAP:
                code = DIALECT_MAP[part]
                return DIALECT_NAMES[code], code
                
    for key, code in DIALECT_MAP.items():
        if key in text:
            return DIALECT_NAMES[code], code
            
    return 'hindi', 'hin'

def get_headers(dialect_code: str) -> dict:
    return {
        'app_id': 'in.stage.web',
        'platform': 'web',
        'os': 'android',
        'api-version': '2',
        'dialect': dialect_code,
        'lang': 'en',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

def format_duration(seconds):
    if not seconds:
        return 'N/A'
    try:
        seconds = int(seconds)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        if h > 0:
            return f'{h}h {m:02d}m'
        elif m > 0:
            return f'{m}m {s:02d}s'
        return f'{s}s'
    except Exception:
        return 'N/A'

def fetch_all_movies(dialect_name: str, dialect_code: str) -> list[dict]:
    headers = get_headers(dialect_code)
    movies = []
    page = 1
    per_page = 50

    print(f'[*] Fetching movie catalog for {dialect_name}...')
    while True:
        try:
            params = {'page': page, 'perPage': per_page}
            resp = requests.get(API_MOVIES_ALL, params=params, headers=headers, timeout=25)
            if resp.status_code != 200:
                break
            data = resp.json()
            items = data.get('data', [])
            if not items:
                break

            for m in items:
                movie_id = m.get('_id')
                title_obj = m.get('title', {})
                title = title_obj.get('en') or title_obj.get('hi') or str(title_obj)
                slug = m.get('slug', '')
                
                movies.append({
                    'id': movie_id,
                    'title': title,
                    'slug': slug,
                    'year': m.get('yearOfRelease'),
                    'web_url': f'https://www.stage.in/en/{dialect_name}/movie/{slug}-{movie_id}',
                    'poster': (m.get('horizontalThumbnail') or {}).get('ratio_16_9') or (m.get('thumbnail') or {}).get('ratio_16_9') or '',
                })

            if not data.get('nextPageAvailable'):
                break
            page += 1
        except Exception as e:
            print(f'[!] Error fetching movie page {page}: {e}')
            break

    print(f'[+] Found {len(movies)} movies.')
    return movies

def fetch_movie_direct_details(movie: dict, dialect_code: str) -> dict:
    headers = get_headers(dialect_code)
    movie_id = movie['id']
    try:
        resp = requests.get(API_MOVIE_DETAILS, params={'movieId': str(movie_id)}, headers=headers, timeout=25)
        if resp.status_code == 200:
            details = resp.json().get('movie', {})
            duration = details.get('duration')
            movie['duration_seconds'] = duration
            movie['length'] = format_duration(duration)
            movie['release_date'] = details.get('releaseDate') or details.get('newFormatReleaseDate') or ''
            
            source_link = details.get('sourceLink')
            if not source_link:
                sp = details.get('selectedPeripheral', {})
                source_link = sp.get('sourceLink') or (sp.get('visionularHls') or {}).get('sourceLink')

            if source_link and str(source_link).strip():
                source_str = str(source_link).strip()
                if not source_str.startswith('http'):
                    movie['raw_video_url'] = f'{MEDIA_BASE}/{source_str}'
                else:
                    movie['raw_video_url'] = source_str
            else:
                movie['raw_video_url'] = None
    except Exception:
        movie['raw_video_url'] = None

    return movie

def fetch_all_shows(dialect_name: str, dialect_code: str) -> list[dict]:
    print(f'[*] Fetching shows catalog for {dialect_name}...')
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    
    shows = []
    urls = [
        f'https://www.stage.in/en/{dialect_name}/shows',
        f'https://www.stage.in/en/{dialect_name}',
    ]
    
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                html = resp.text
                next_f_calls = re.findall(r'self\.__next_f\.push\(\[(.*?)\n?\]\)', html)
                for call in next_f_calls:
                    m = re.match(r'^\d+,\s*"(.*)"$', call, re.DOTALL)
                    if m:
                        val = m.group(1)
                        try:
                            val = json.loads('"' + val + '"')
                        except Exception:
                            pass
                        
                        items_matches = re.finditer(r'"items"\s*:\s*\[', val)
                        for im in items_matches:
                            try:
                                arr, _ = json.JSONDecoder().raw_decode(val, im.end() - 1)
                                for item in arr:
                                    if isinstance(item, dict) and item.get('type') == 'show':
                                        show_id = item.get('_id')
                                        slug = item.get('slug', '')
                                        poster = item.get('horizontalThumbnail') or item.get('thumbnail') or ""
                                        if isinstance(poster, dict):
                                            poster = poster.get('ratio_16_9') or poster.get('sourceLink') or ""
                                        elif not isinstance(poster, str):
                                            poster = ""
                                        shows.append({
                                            'id': show_id,
                                            'title': item.get('title', ''),
                                            'slug': slug,
                                            'web_url': f'https://www.stage.in/en/{dialect_name}/show/{slug}-{show_id}',
                                            'poster': poster,
                                        })
                            except Exception:
                                pass
        except Exception as e:
            print(f'[!] Error fetching {url}: {e}')

    unique_shows = list({s['id']: s for s in shows if s.get('id')}.values())
    print(f'[+] Found {len(unique_shows)} shows.')
    return unique_shows

def fetch_show_direct_details(show: dict, dialect_code: str) -> dict:
    headers = get_headers(dialect_code)
    show_id = show['id']
    show['episodes'] = []
    try:
        resp = requests.get(API_SHOW_DETAILS, params={'showId': str(show_id), 'seasonId': ''}, headers=headers, timeout=25)
        if resp.status_code == 200:
            data = resp.json().get('data', {})
            raw_episodes = data.get('episodes', [])
            total_duration = 0

            for idx, ep in enumerate(raw_episodes, start=1):
                ep_duration = ep.get('duration') or 0
                total_duration += int(ep_duration) if ep_duration else 0
                source_link = ep.get('sourceLink')
                
                raw_url = None
                if source_link and str(source_link).strip():
                    source_str = str(source_link).strip()
                    if not source_str.startswith('http'):
                        raw_url = f'{MEDIA_BASE}/{source_str}'
                    else:
                        raw_url = source_str

                ep_poster = ep.get('thumbnail') or ""
                if isinstance(ep_poster, dict):
                    ep_poster = ep_poster.get('ratio_16_9') or ep_poster.get('sourceLink') or ""
                elif not isinstance(ep_poster, str):
                    ep_poster = ""

                show['episodes'].append({
                    'episode_number': ep.get('episodeOrder') or ep.get('order') or idx,
                    'id': ep.get('id') or ep.get('_id'),
                    'title': (ep.get('title') or f'Episode {idx}').strip(),
                    'duration_seconds': ep_duration,
                    'length': format_duration(ep_duration),
                    'release_date': ep.get('releaseDate') or '',
                    'raw_video_url': raw_url,
                    'poster': ep_poster
                })

            show['episode_count'] = len(show['episodes'])
            show['total_duration_seconds'] = total_duration
            show['total_length'] = format_duration(total_duration)
    except Exception as e:
        show['episode_count'] = 0

    return show

def main():
    print('=' * 70)
    print('      STAGE.in Catalog & Raw Direct Links JSON Exporter')
    print('=' * 70)

    if len(sys.argv) > 1:
        user_input = sys.argv[1]
    else:
        print('Available languages: hindi, haryanvi, rajasthani, bhojpuri, gujarati, marathi, bengali, punjabi')
        try:
            user_input = input('Enter language name or STAGE URL (e.g. https://www.stage.in/en/hindi): ').strip()
        except EOFError:
            user_input = 'hindi'
        if not user_input:
            user_input = 'hindi'

    dialect_name, dialect_code = parse_input_language(user_input)
    print(f'\n[+] Selected Language: {dialect_name.upper()} (code: {dialect_code})')

    raw_movies = fetch_all_movies(dialect_name, dialect_code)
    raw_shows = fetch_all_shows(dialect_name, dialect_code)

    processed_movies = []
    if raw_movies:
        print(f'\n[*] Extracting direct master video links for {len(raw_movies)} movies...')
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(fetch_movie_direct_details, m, dialect_code) for m in raw_movies]
            for f in tqdm(as_completed(futures), total=len(futures), desc='Movies Progress'):
                processed_movies.append(f.result())

    processed_shows = []
    if raw_shows:
        print(f'\n[*] Extracting direct episode video links for {len(raw_shows)} shows...')
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(fetch_show_direct_details, s, dialect_code) for s in raw_shows]
            for f in tqdm(as_completed(futures), total=len(futures), desc='Shows Progress'):
                processed_shows.append(f.result())

    processed_movies.sort(key=lambda x: str(x.get('id', '')), reverse=True)
    processed_shows.sort(key=lambda x: str(x.get('id', '')), reverse=True)

    output_data = {
        'platform': 'STAGE.in',
        'language': dialect_name,
        'dialect_code': dialect_code,
        'total_movies': len(processed_movies),
        'total_shows': len(processed_shows),
        'exported_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'movies': processed_movies,
        'shows': processed_shows
    }

    out_file = f'stage_{dialect_name}_catalog.json'
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    print('\n' + '=' * 70)
    print(f'[SUCCESS] Exported full catalog to: {os.path.abspath(out_file)}')
    print(f' - Total Movies: {len(processed_movies)}')
    print(f' - Total Shows:  {len(processed_shows)}')
    total_episodes = sum(s.get('episode_count', 0) for s in processed_shows)
    print(f' - Total Show Episodes: {total_episodes}')
    print('=' * 70)

if __name__ == '__main__':
    main()
