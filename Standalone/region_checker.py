#!/usr/bin/env python3
"""
================================================================================
    Global Streaming Region Availability Checker (Prime Video & Netflix)
                   Powered by JustWatch GraphQL API
================================================================================

A lightning-fast, comprehensive tool to check Amazon Prime Video and Netflix
availability for movies and TV shows across 130 countries worldwide.

Features:
  • High-performance GraphQL batching: Checks all 130 regions in ~2 seconds.
  • Multi-service coverage: Amazon Prime Video & Netflix.
  • Complete classification: Netflix Subscription, Prime Video Subscription,
    Prime Video Channels, Freevee / Free with Ads, and Amazon Store (Rent / Buy).
  • Presentation quality detection: 4K UHD, HD, and SD.
  • Exact digital pricing per country and currency for Amazon Store.
  • Direct URL detection: Paste any Prime Video or Netflix URL directly.
  • Flexible search: Title, title with year (e.g. 'Inception (2010)'),
    JustWatch node ID (e.g. 'tm92641'), or JustWatch URL.
  • CLI filters: --netflix, --prime, --service, --sub, --free, --channels, --rent-buy, --4k, --country.
  • Automation & scripting: --json, --save, --yes, --non-interactive.

Usage examples:
  python prime_region_checker.py "https://www.netflix.com/in/title/80137461"
  python prime_region_checker.py "Inception"
  python prime_region_checker.py "Stranger Things" --netflix
  python prime_region_checker.py "The Boys" --prime
  python prime_region_checker.py "Dune" --sub --4k
  python prime_region_checker.py "Fallout" --country US,GB,DE,IN,JP
  python prime_region_checker.py "Inception" --save
  python prime_region_checker.py "tm92641" --json
"""

import sys
import os
import io
import re
import time
import json
import argparse
import textwrap
import html as html_module
from typing import Dict, List, Any, Optional, Tuple, Set

# ── Force UTF-8 standard streams & ANSI colors on Windows terminals ────────
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # Enable ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004) for STD_OUTPUT_HANDLE (-11)
        h_out = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(h_out, ctypes.byref(mode)):
            kernel32.SetConsoleMode(h_out, mode.value | 0x0004)
    except Exception:
        pass

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util import Retry
except ImportError:
    print("ERROR: 'requests' package is required. Install: pip install requests", file=sys.stderr)
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Constants & Regional Locales
# ─────────────────────────────────────────────────────────────────────────────

JUSTWATCH_GRAPHQL = "https://apis.justwatch.com/graphql"

DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Origin": "https://www.justwatch.com",
    "Referer": "https://www.justwatch.com/",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

ALL_LOCALES: Dict[str, str] = {
    "AD": "Andorra",        "AE": "United Arab Emirates", "AG": "Antigua & Barbuda",
    "AL": "Albania",        "AO": "Angola",               "AR": "Argentina",
    "AT": "Austria",        "AU": "Australia",            "AZ": "Azerbaijan",
    "BA": "Bosnia",         "BB": "Barbados",             "BE": "Belgium",
    "BF": "Burkina Faso",   "BG": "Bulgaria",             "BH": "Bahrain",
    "BM": "Bermuda",        "BO": "Bolivia",              "BR": "Brazil",
    "BS": "Bahamas",        "BY": "Belarus",              "BZ": "Belize",
    "CA": "Canada",         "CD": "DR Congo",             "CH": "Switzerland",
    "CI": "Ivory Coast",    "CL": "Chile",                "CM": "Cameroon",
    "CO": "Colombia",       "CR": "Costa Rica",           "CU": "Cuba",
    "CV": "Cape Verde",     "CY": "Cyprus",               "CZ": "Czechia",
    "DE": "Germany",        "DK": "Denmark",              "DO": "Dominican Republic",
    "DZ": "Algeria",        "EC": "Ecuador",              "EE": "Estonia",
    "EG": "Egypt",          "ES": "Spain",                "ET": "Ethiopia",
    "FI": "Finland",        "FJ": "Fiji",                 "FR": "France",
    "GB": "United Kingdom", "GH": "Ghana",                "GQ": "Equatorial Guinea",
    "GR": "Greece",         "GT": "Guatemala",            "GY": "Guyana",
    "HK": "Hong Kong",      "HN": "Honduras",             "HR": "Croatia",
    "HU": "Hungary",        "ID": "Indonesia",            "IE": "Ireland",
    "IL": "Israel",         "IN": "India",                "IQ": "Iraq",
    "IS": "Iceland",        "IT": "Italy",                "JM": "Jamaica",
    "JO": "Jordan",         "JP": "Japan",                "KE": "Kenya",
    "KR": "South Korea",    "KW": "Kuwait",               "LB": "Lebanon",
    "LC": "St. Lucia",      "LI": "Liechtenstein",        "LT": "Lithuania",
    "LU": "Luxembourg",     "LV": "Latvia",               "LY": "Libya",
    "MA": "Morocco",        "MC": "Monaco",               "MD": "Moldova",
    "ME": "Montenegro",     "MK": "North Macedonia",      "ML": "Mali",
    "MT": "Malta",          "MU": "Mauritius",            "MX": "Mexico",
    "MY": "Malaysia",       "MZ": "Mozambique",           "NE": "Niger",
    "NG": "Nigeria",        "NI": "Nicaragua",            "NL": "Netherlands",
    "NO": "Norway",         "NZ": "New Zealand",          "OM": "Oman",
    "PA": "Panama",         "PE": "Peru",                 "PH": "Philippines",
    "PK": "Pakistan",       "PL": "Poland",               "PS": "Palestine",
    "PT": "Portugal",       "PY": "Paraguay",             "QA": "Qatar",
    "RO": "Romania",        "RS": "Serbia",               "RU": "Russia",
    "SA": "Saudi Arabia",   "SC": "Seychelles",           "SD": "Sudan",
    "SE": "Sweden",         "SG": "Singapore",            "SI": "Slovenia",
    "SK": "Slovakia",       "SN": "Senegal",              "SV": "El Salvador",
    "TD": "Chad",           "TH": "Thailand",             "TN": "Tunisia",
    "TR": "Turkey",         "TT": "Trinidad & Tobago",    "TW": "Taiwan",
    "TZ": "Tanzania",       "UA": "Ukraine",              "UG": "Uganda",
    "US": "United States",  "UY": "Uruguay",              "VE": "Venezuela",
    "YE": "Yemen",          "ZA": "South Africa",         "ZM": "Zambia",
    "ZW": "Zimbabwe",
}

LANGUAGE_NAMES: Dict[str, str] = {
    "en": "English",    "es": "Spanish",    "fr": "French",     "de": "German",
    "it": "Italian",    "ja": "Japanese",   "pt": "Portuguese", "hi": "Hindi",
    "zh": "Chinese",    "ko": "Korean",     "ru": "Russian",    "ar": "Arabic",
    "nl": "Dutch",      "pl": "Polish",     "tr": "Turkish",    "cs": "Czech",
    "ca": "Catalan",    "da": "Danish",     "fi": "Finnish",    "sv": "Swedish",
    "no": "Norwegian",  "nb": "Norwegian",  "hu": "Hungarian",  "el": "Greek",
    "he": "Hebrew",     "th": "Thai",       "vi": "Vietnamese", "id": "Indonesian",
    "ro": "Romanian",   "uk": "Ukrainian",  "bg": "Bulgarian",  "hr": "Croatian",
    "sk": "Slovak",     "sl": "Slovenian",  "sr": "Serbian",    "et": "Estonian",
    "lv": "Latvian",    "lt": "Lithuanian", "is": "Icelandic",  "ta": "Tamil",
    "te": "Telugu",     "kn": "Kannada",    "ml": "Malayalam",  "mr": "Marathi",
    "bn": "Bengali",    "gu": "Gujarati",   "pa": "Punjabi",    "fa": "Persian",
}

def format_audio_languages(lang_codes: List[str]) -> List[str]:
    """Convert ISO language codes to readable language names without duplicates."""
    result = []
    for code in lang_codes:
        name = LANGUAGE_NAMES.get(code.lower(), code.upper())
        if name not in result:
            result.append(name)
    return result

# ─────────────────────────────────────────────────────────────────────────────
# HTTP Session
# ─────────────────────────────────────────────────────────────────────────────

def get_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

HTTP_CLIENT = get_session()

def resolve_prime_url(raw_url: str) -> str:
    """Resolve raw Amazon GTI / app / watch URLs into clean canonical www.primevideo.com/detail/... links."""
    if not raw_url:
        return raw_url
    if "primevideo.com/detail/" in raw_url and "gti=" not in raw_url:
        return raw_url

    match = re.search(r"gti=([^&]+)", raw_url)
    if match:
        target = f"https://app.primevideo.com/detail?gti={match.group(1)}"
    else:
        target = raw_url.replace("watch.amazon.com", "app.primevideo.com").replace("www.amazon.com", "app.primevideo.com")

    clean_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = requests.get(target, headers=clean_headers, allow_redirects=True, timeout=5)
        if r.status_code == 200 and "primevideo.com/detail/" in r.url:
            return r.url
    except Exception:
        pass
    return raw_url

def resolve_netflix_url(raw_url: str) -> str:
    """Normalize Netflix URL to canonical title link."""
    if not raw_url:
        return raw_url
    m = re.search(r"netflix\.com/(?:[a-z]{2}/)?title/(\d+)", raw_url)
    if m:
        return f"https://www.netflix.com/title/{m.group(1)}"
    return raw_url

def extract_title_from_prime_url(raw_url: str) -> Optional[str]:
    """Extract movie or TV show title from Amazon / Prime Video URL by fetching page HTML."""
    if not ("primevideo.com" in raw_url or "amazon." in raw_url or "gti=" in raw_url):
        return None
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = HTTP_CLIENT.get(raw_url, headers=headers, allow_redirects=True, timeout=8)
        if r.status_code == 200:
            html_text = html_module.unescape(r.text)
            title_text = ""
            m_og = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html_text, re.I)
            if not m_og:
                m_og = re.search(r'<meta\s+name=["\']title["\']\s+content=["\']([^"\']+)["\']', html_text, re.I)
            if m_og:
                title_text = m_og.group(1)
            else:
                m_t = re.search(r'<title>([^<]+)</title>', html_text, re.I)
                if m_t:
                    title_text = m_t.group(1)
            if title_text:
                title_text = html_module.unescape(title_text)
                title_text = re.sub(r"^(?:Watch\s+|Regarder\s+|Ver\s+|Prime\s+Video:\s*|Amazon\.com:\s*|Amazon\s+Video:\s*|Amazon:\s*)", "", title_text, flags=re.I)
                title_text = re.sub(r"\s*[\(\[]\s*(?:OV|OmU|OV/OmU|dt\./OV|Subbed|Dubbed|4K|4K\s+UHD|HD|SD)\s*[\)\]]", "", title_text, flags=re.I)
                title_text = re.sub(r"\s*(?:online\s+streamen|online\s+ansehen|watch\s+online|en\s+streaming|ver\s+online|online|streamen|stream|watch)?\s*[\-\u2010-\u2015\|:~]\s*(?:online\s+streamen|online\s+ansehen|watch\s+online|en\s+streaming|ver\s+online|online|streamen|stream|watch)?\s*(?:Prime\s+Video|Amazon\s+Video|Amazon).*$", "", title_text, flags=re.I)
                title_text = re.sub(r"\s*[\-\u2010-\u2015\|:~]\s*(?:Prime\s+Video|Amazon\s+Video|Amazon\b).*$", "", title_text, flags=re.I)
                title_text = re.sub(r"\s+(?:online\s+streamen|online\s+ansehen|watch\s+online|en\s+streaming|ver\s+online|online|streamen)\s*$", "", title_text, flags=re.I)
                return title_text.strip()
    except Exception:
        pass
    return None

def extract_title_from_netflix_url(raw_url: str) -> Optional[str]:
    """Extract movie or TV show title from Netflix URL by inspecting page HTML."""
    if not ("netflix.com" in raw_url and "title" in raw_url):
        return None
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = HTTP_CLIENT.get(raw_url, headers=headers, allow_redirects=True, timeout=8)
        if r.status_code == 200:
            html_text = r.text
            # 1. <h1> tag inside hero / title wrapper
            m_h1 = re.search(r'<h1[^>]*>(.*?)</h1>', html_text, re.I | re.DOTALL)
            if m_h1:
                title = re.sub(r'<[^>]+>', '', m_h1.group(1)).strip()
                title = html_module.unescape(title)
                if title and title.lower() != "netflix":
                    return title
            # 2. og:title meta tag
            m_og = re.search(r'<meta\s+(?:property|name)=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html_text, re.I)
            if m_og:
                title = html_module.unescape(m_og.group(1))
                title = re.sub(r"\s*\|\s*Netflix.*$", "", title, flags=re.I).strip()
                if title and title.lower() != "netflix":
                    return title
            # 3. <title> tag
            m_t = re.search(r'<title>([^<]+)</title>', html_text, re.I)
            if m_t:
                title = html_module.unescape(m_t.group(1))
                title = re.sub(r"\s*\|\s*Netflix.*$", "", title, flags=re.I).strip()
                title = re.sub(r"^Watch\s+", "", title, flags=re.I).strip()
                if title and title.lower() != "netflix":
                    return title
    except Exception:
        pass
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Terminal Styling
# ─────────────────────────────────────────────────────────────────────────────

class Style:
    USE_COLOR = "NO_COLOR" not in os.environ

    @classmethod
    def disable(cls):
        cls.RESET       = ""
        cls.BOLD        = ""
        cls.DIM         = ""
        cls.GREEN       = ""
        cls.CYAN        = ""
        cls.YELLOW      = ""
        cls.BLUE        = ""
        cls.MAGENTA     = ""
        cls.RED         = ""
        cls.BRIGHT_RED  = ""
        cls.WHITE       = ""
        cls.BRIGHT_CYAN = ""
        cls.GRAY        = ""

    RESET       = "\033[0m"  if USE_COLOR else ""
    BOLD        = "\033[1m"  if USE_COLOR else ""
    DIM         = "\033[2m"  if USE_COLOR else ""
    GREEN       = "\033[32m" if USE_COLOR else ""
    CYAN        = "\033[36m" if USE_COLOR else ""
    YELLOW      = "\033[33m" if USE_COLOR else ""
    BLUE        = "\033[34m" if USE_COLOR else ""
    MAGENTA     = "\033[35m" if USE_COLOR else ""
    RED         = "\033[31m" if USE_COLOR else ""
    BRIGHT_RED  = "\033[91m" if USE_COLOR else ""
    WHITE       = "\033[37m" if USE_COLOR else ""
    BRIGHT_CYAN = "\033[96m" if USE_COLOR else ""
    GRAY        = "\033[90m" if USE_COLOR else ""

# ─────────────────────────────────────────────────────────────────────────────
# GraphQL Client
# ─────────────────────────────────────────────────────────────────────────────

def execute_gql(query: str, variables: Optional[dict] = None) -> dict:
    """Execute JustWatch GraphQL query with retry & error extraction."""
    resp = HTTP_CLIENT.post(
        JUSTWATCH_GRAPHQL,
        headers=DEFAULT_HEADERS,
        json={"query": query, "variables": variables or {}},
        timeout=15
    )
    if resp.status_code == 422:
        try:
            err = resp.json().get("errors", [{}])[0].get("message", "Validation error")
            raise RuntimeError(f"GraphQL Validation Failed: {err}")
        except Exception:
            resp.raise_for_status()

    resp.raise_for_status()
    payload = resp.json()
    if "errors" in payload and not payload.get("data"):
        err_msg = payload["errors"][0].get("message", "Unknown GraphQL error")
        raise RuntimeError(f"JustWatch API Error: {err_msg}")
    return payload.get("data", {})


SEARCH_QUERY = """
query SearchTitles($filter: TitleFilter, $country: Country!, $language: Language!, $first: Int!) {
  popularTitles(country: $country, filter: $filter, first: $first) {
    edges {
      node {
        id
        objectId
        objectType
        content(country: $country, language: $language) {
          title
          fullPath
          originalReleaseYear
          shortDescription
          genres {
            translation(language: $language)
          }
          scoring {
            imdbScore
            tmdbScore
          }
        }
      }
    }
  }
}
"""

def score_title_match(node_title: str, query_text: str, prefix_title: str) -> int:
    """Calculate title match score [0-100] to rank results and eliminate irrelevant keyword matches."""
    node_t = node_title.strip().lower()
    q_t = query_text.strip().lower()
    p_t = prefix_title.strip().lower()

    if node_t == p_t or node_t == q_t:
        return 100
    if node_t.startswith(p_t) or p_t.startswith(node_t):
        return 90

    p_words = set(re.findall(r"\w+", p_t))
    q_words = set(re.findall(r"\w+", q_t)) - {"online", "prime", "video", "netflix", "watch", "stream", "movie", "show"}
    node_words = set(re.findall(r"\w+", node_t))

    if p_words and p_words.issubset(node_words):
        return 85
    if node_words and node_words.issubset(p_words):
        return 80
    if p_words & node_words:
        return 60
    if q_words & node_words:
        return 40
    return 0


def search_titles(
    query_text: str,
    media_type: Optional[str] = None,
    year: Optional[int] = None,
    limit: int = 10
) -> List[dict]:
    """Search JustWatch for titles matching query, type, and year."""
    clean_query = query_text.strip()

    # Extract year if specified as 'Title (YYYY)' or 'Title [YYYY]'
    year_match = re.search(r"[\(\[]?((?:19|20)\d{2})[\)\]]?$", clean_query)
    if year_match and not year:
        try:
            year = int(year_match.group(1))
            clean_query = clean_query[:year_match.start()].strip()
        except ValueError:
            pass

    filter_dict: Dict[str, Any] = {"searchQuery": clean_query}
    if media_type:
        filter_dict["objectTypes"] = ["MOVIE"] if media_type.lower() == "movie" else ["SHOW"]
    if year:
        filter_dict["releaseYear"] = {"min": year, "max": year}

    variables = {
        "country": "US",
        "language": "en",
        "first": limit,
        "filter": filter_dict
    }

    try:
        data = execute_gql(SEARCH_QUERY, variables)
    except Exception:
        # Fall back to searching without strict year filter if failed
        if year:
            filter_dict.pop("releaseYear", None)
            variables["filter"] = filter_dict
            data = execute_gql(SEARCH_QUERY, variables)
        else:
            raise

    edges = data.get("popularTitles", {}).get("edges", [])
    results = []
    for edge in edges:
        node = edge.get("node", {})
        content = node.get("content", {})
        scoring = content.get("scoring") or {}
        genres = [g.get("translation") for g in (content.get("genres") or []) if g.get("translation")]
        full_path = content.get("fullPath") or ""
        raw_type = node.get("objectType", "UNKNOWN")
        jw_url = f"https://www.justwatch.com{full_path}" if full_path else ""

        results.append({
            "id": node.get("id"),
            "objectId": node.get("objectId"),
            "type": raw_type,
            "title": content.get("title", "Unknown"),
            "url": jw_url,
            "year": content.get("originalReleaseYear"),
            "imdb": scoring.get("imdbScore"),
            "tmdb": scoring.get("tmdbScore"),
            "genres": genres,
            "description": content.get("shortDescription", ""),
        })

    # If strict search returned nothing, retry with raw input query
    if not results and clean_query != query_text.strip():
        variables["filter"] = {"searchQuery": query_text.strip()}
        if media_type:
            variables["filter"]["objectTypes"] = ["MOVIE"] if media_type.lower() == "movie" else ["SHOW"]
        data = execute_gql(SEARCH_QUERY, variables)
        edges = data.get("popularTitles", {}).get("edges", [])
        for edge in edges:
            node = edge.get("node", {})
            content = node.get("content", {})
            scoring = content.get("scoring") or {}
            genres = [g.get("translation") for g in (content.get("genres") or []) if g.get("translation")]
            full_path = content.get("fullPath") or ""
            raw_type = node.get("objectType", "UNKNOWN")
            jw_url = f"https://www.justwatch.com{full_path}" if full_path else ""

            results.append({
                "id": node.get("id"),
                "objectId": node.get("objectId"),
                "type": raw_type,
                "title": content.get("title", "Unknown"),
                "url": jw_url,
                "year": content.get("originalReleaseYear"),
                "imdb": scoring.get("imdbScore"),
                "tmdb": scoring.get("tmdbScore"),
                "genres": genres,
                "description": content.get("shortDescription", ""),
            })

    # Try searching prefix title if query has subtitle or colon/dash
    prefix_title = ""
    if ":" in clean_query or "-" in clean_query or "–" in clean_query:
        prefix_title = re.split(r"[:\-–—]", clean_query)[0].strip()
        if prefix_title and len(prefix_title) > 2 and prefix_title.lower() != clean_query.lower():
            try:
                sub_vars = {
                    "country": "US",
                    "language": "en",
                    "first": limit,
                    "filter": {"searchQuery": prefix_title}
                }
                if media_type:
                    sub_vars["filter"]["objectTypes"] = ["MOVIE"] if media_type.lower() == "movie" else ["SHOW"]
                sub_data = execute_gql(SEARCH_QUERY, sub_vars)
                seen_ids = {r["id"] for r in results}
                for edge in sub_data.get("popularTitles", {}).get("edges", []):
                    n = edge.get("node", {})
                    c = n.get("content", {})
                    if n.get("id") not in seen_ids:
                        sc = c.get("scoring") or {}
                        g_list = [g.get("translation") for g in (c.get("genres") or []) if g.get("translation")]
                        fp = c.get("fullPath") or ""
                        rt = n.get("objectType", "UNKNOWN")
                        results.append({
                            "id": n.get("id"),
                            "objectId": n.get("objectId"),
                            "type": rt,
                            "title": c.get("title", "Unknown"),
                            "url": f"https://www.justwatch.com{fp}" if fp else "",
                            "year": c.get("originalReleaseYear"),
                            "imdb": sc.get("imdbScore"),
                            "tmdb": sc.get("tmdbScore"),
                            "genres": g_list,
                            "description": c.get("shortDescription", ""),
                        })
                        seen_ids.add(n.get("id"))
            except Exception:
                pass

    if not prefix_title:
        prefix_title = clean_query

    # Score and filter out zero-match noise
    filtered_results = []
    for r in results:
        s = score_title_match(r["title"], clean_query, prefix_title)
        if s > 0:
            r["match_score"] = s
            filtered_results.append(r)

    if filtered_results:
        filtered_results.sort(key=lambda x: (x.get("match_score", 0), x.get("year") or 0, x.get("imdb") or 0), reverse=True)
        return filtered_results

    return results


def build_batch_query(countries: List[str], target_type: Optional[str] = None) -> str:
    """Build GraphQL batch query fetching title info and all region offers via aliasing."""
    if target_type == "MOVIE":
        offer_fragment = """
          ... on Movie {
            offers(country: %s, platform: WEB) {
              monetizationType
              presentationType
              retailPrice(language: "en")
              standardWebURL
              audioLanguages
              package {
                packageId
                clearName
                technicalName
                shortName
              }
            }
          }
        """
    elif target_type == "SHOW":
        offer_fragment = """
          ... on Show {
            offers(country: %s, platform: WEB) {
              monetizationType
              presentationType
              retailPrice(language: "en")
              standardWebURL
              audioLanguages
              package {
                packageId
                clearName
                technicalName
                shortName
              }
            }
          }
        """
    else:
        offer_fragment = """
          ... on Movie {
            offers(country: %s, platform: WEB) {
              monetizationType
              presentationType
              retailPrice(language: "en")
              standardWebURL
              audioLanguages
              package { packageId clearName technicalName shortName }
            }
          }
          ... on Show {
            offers(country: %s, platform: WEB) {
              monetizationType
              presentationType
              retailPrice(language: "en")
              standardWebURL
              audioLanguages
              package { packageId clearName technicalName shortName }
            }
          }
        """

    country_subqueries = []
    for c in countries:
        frag = offer_fragment % (c, c) if "%s" in offer_fragment and offer_fragment.count("%s") == 2 else offer_fragment % c
        country_subqueries.append(f"  c_{c.lower()}: node(id: $id) {{\n{frag}\n  }}")

    query = f"""
    query GetBatchOffers($id: ID!) {{
      info: node(id: $id) {{
        id
        ... on Movie {{
          objectId
          objectType
          content(country: US, language: "en") {{
            title
            fullPath
            originalReleaseYear
            runtime
            shortDescription
            genres {{ translation(language: "en") }}
            scoring {{ imdbScore tmdbScore }}
            externalIds {{ imdbId tmdbId }}
          }}
        }}
        ... on Show {{
          objectId
          objectType
          totalSeasonCount
          content(country: US, language: "en") {{
            title
            fullPath
            originalReleaseYear
            shortDescription
            genres {{ translation(language: "en") }}
            scoring {{ imdbScore tmdbScore }}
            externalIds {{ imdbId tmdbId }}
          }}
        }}
      }}
      {' '.join(country_subqueries)}
    }}
    """
    return query


def fetch_title_and_offers(
    node_id: str,
    target_countries: List[str],
    target_type: Optional[str] = None
) -> Tuple[dict, dict]:
    """
    Fetch title metadata and all country offers in lightning-fast batched GraphQL requests.
    Batches in chunks of 70 to stay well within query limits and execute in ~1-2 seconds.
    """
    country_offers_raw: Dict[str, list] = {}
    title_metadata: dict = {}

    chunk_size = 70
    chunks = [target_countries[i:i + chunk_size] for i in range(0, len(target_countries), chunk_size)]

    for chunk in chunks:
        gql_query = build_batch_query(chunk, target_type)
        data = execute_gql(gql_query, {"id": node_id})

        if not title_metadata and "info" in data and data["info"]:
            info_node = data["info"]
            content = info_node.get("content") or {}
            scoring = content.get("scoring") or {}
            ext_ids = content.get("externalIds") or {}
            genres = [g.get("translation") for g in (content.get("genres") or []) if g.get("translation")]

            full_path = content.get("fullPath") or ""
            raw_type = info_node.get("objectType", "UNKNOWN")
            m_type_slug = "movie" if raw_type.upper() == "MOVIE" else "tv-show"
            
            if full_path:
                jw_url = f"https://www.justwatch.com{full_path}"
            else:
                title_slug = re.sub(r"[^\w\-]", "", content.get("title", "").lower().replace(" ", "-"))
                jw_url = f"https://www.justwatch.com/us/{m_type_slug}/{title_slug}" if title_slug else ""

            imdb_id = ext_ids.get("imdbId")
            tmdb_id = ext_ids.get("tmdbId")
            imdb_url = f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else ""
            tmdb_url = f"https://www.themoviedb.org/{'movie' if raw_type.upper() == 'MOVIE' else 'tv'}/{tmdb_id}" if tmdb_id else ""

            title_metadata = {
                "id": info_node.get("id"),
                "objectId": info_node.get("objectId"),
                "type": raw_type,
                "title": content.get("title", "Unknown"),
                "url": jw_url,
                "imdb_url": imdb_url,
                "tmdb_url": tmdb_url,
                "year": content.get("originalReleaseYear"),
                "runtime": content.get("runtime"),
                "seasons": info_node.get("totalSeasonCount"),
                "imdb": scoring.get("imdbScore"),
                "tmdb": scoring.get("tmdbScore"),
                "imdbId": imdb_id,
                "tmdbId": tmdb_id,
                "genres": genres,
                "description": content.get("shortDescription", ""),
            }

        for c in chunk:
            c_node = data.get(f"c_{c.lower()}")
            offers = (c_node.get("offers") or []) if c_node else []
            country_offers_raw[c] = offers

    # Extract primary service URLs and audio languages
    raw_prime_url = None
    raw_netflix_url = None
    all_audio_codes = []
    for c_code, offers in country_offers_raw.items():
        for offer in offers:
            pkg = offer.get("package") or {}
            tech_name = (pkg.get("technicalName") or "").lower()
            clear_lower = (pkg.get("clearName") or "").lower()

            if "amazondvdbr" in tech_name or "dvd" in clear_lower or "music" in tech_name:
                continue

            # Prime Video detection
            if "amazon" in tech_name or "prime" in tech_name or "amazon" in clear_lower or "prime" in clear_lower:
                if not raw_prime_url and offer.get("standardWebURL"):
                    raw_prime_url = offer.get("standardWebURL")
                for lang in (offer.get("audioLanguages") or []):
                    if lang not in all_audio_codes:
                        all_audio_codes.append(lang)

            # Netflix detection
            if "netflix" in tech_name or "netflix" in clear_lower:
                if not raw_netflix_url and offer.get("standardWebURL"):
                    raw_netflix_url = offer.get("standardWebURL")
                for lang in (offer.get("audioLanguages") or []):
                    if lang not in all_audio_codes:
                        all_audio_codes.append(lang)

    title_metadata["prime_url"] = resolve_prime_url(raw_prime_url) if raw_prime_url else None
    title_metadata["netflix_url"] = resolve_netflix_url(raw_netflix_url) if raw_netflix_url else None
    title_metadata["audio_languages"] = format_audio_languages(all_audio_codes)

    return title_metadata, country_offers_raw


# ─────────────────────────────────────────────────────────────────────────────
# Offer Parsing & Classification
# ─────────────────────────────────────────────────────────────────────────────

def parse_region_availability(country_offers_raw: Dict[str, list]) -> Dict[str, dict]:
    """
    Parse raw JustWatch offers into structured streaming categories:
      1. netflix_sub   : Netflix Subscription (Standard / Premium / with Ads)
      2. prime_sub     : Prime Video membership (Flatrate)
      3. prime_channels: Prime Video Add-on channels (Max, Paramount+, Starz, etc.)
      4. free_ads      : Free on Amazon / Freevee / miniTV
      5. rent          : Digital rental on Amazon Video
      6. buy           : Digital purchase on Amazon Video
    """
    parsed: Dict[str, dict] = {}

    for country_code, offers in country_offers_raw.items():
        country_name = ALL_LOCALES.get(country_code, country_code)

        entry = {
            "country_code": country_code,
            "country_name": country_name,
            "netflix_sub": [],
            "prime_sub": [],
            "prime_channels": [],
            "free_ads": [],
            "rent": [],
            "buy": [],
        }

        seen_netflix = set()
        seen_sub = set()
        seen_channels = set()
        seen_free = set()
        seen_rent = set()
        seen_buy = set()

        for offer in offers:
            pkg = offer.get("package") or {}
            tech_name = (pkg.get("technicalName") or "").lower()
            clear_name = (pkg.get("clearName") or "").strip()
            clear_lower = clear_name.lower()

            # Ignore physical disc mail order and music
            if "amazondvdbr" in tech_name or "dvd" in clear_lower or "music" in tech_name:
                continue

            mon = offer.get("monetizationType")
            pres = offer.get("presentationType")
            quality = "4K UHD" if pres == "_4K" else (pres or "HD")
            price = offer.get("retailPrice")

            offer_url = offer.get("standardWebURL")
            audio_langs = format_audio_languages(offer.get("audioLanguages") or [])

            # ── 1. Netflix ───────────────────────────────────────────────────
            if (
                tech_name in ("netflix", "netflixbasicwithads", "netflixkids") or
                "netflix" in clear_lower
            ):
                if mon in ("FLATRATE", "FLATRATE_AND_BUY", "FREE", "ADS"):
                    has_ads = "ads" in clear_lower or "ads" in tech_name or mon == "ADS"
                    sub_key = (quality, has_ads)
                    if sub_key not in seen_netflix:
                        seen_netflix.add(sub_key)
                        entry["netflix_sub"].append({
                            "quality": quality,
                            "has_ads": has_ads,
                            "package": clear_name,
                            "url": offer_url,
                            "audio_languages": audio_langs
                        })

            # ── Amazon / Prime Video ─────────────────────────────────────────
            if not ("amazon" in tech_name or "prime" in tech_name or "amazon" in clear_lower or "prime" in clear_lower):
                continue

            # 2. Main Amazon Prime Video Subscription
            if (
                tech_name in ("amazonprime", "amazonprimevideo", "amazonprimevideowithads") or
                clear_lower in ("amazon prime video", "amazon prime video with ads")
            ):
                if mon in ("FLATRATE", "FLATRATE_AND_BUY"):
                    has_ads = "Ads" in clear_name or "ads" in tech_name
                    sub_key = (quality, has_ads)
                    if sub_key not in seen_sub:
                        seen_sub.add(sub_key)
                        entry["prime_sub"].append({
                            "quality": quality,
                            "has_ads": has_ads,
                            "package": clear_name,
                            "url": offer_url,
                            "audio_languages": audio_langs
                        })
                elif mon in ("FREE", "ADS"):
                    if (quality, clear_name) not in seen_free:
                        seen_free.add((quality, clear_name))
                        entry["free_ads"].append({
                            "quality": quality,
                            "package": clear_name,
                            "url": offer_url,
                            "audio_languages": audio_langs
                        })

            # 3. Prime Video Channels (Add-on subscriptions)
            elif "amazon channel" in clear_lower or tech_name.startswith("amazonprimechannel"):
                if mon in ("FLATRATE", "FLATRATE_AND_BUY"):
                    channel_clean = re.sub(r"\s*Amazon Channel\s*", "", clear_name, flags=re.IGNORECASE).strip()
                    if (channel_clean, quality) not in seen_channels:
                        seen_channels.add((channel_clean, quality))
                        entry["prime_channels"].append({
                            "channel": channel_clean,
                            "quality": quality,
                            "package": clear_name,
                            "url": offer_url,
                            "audio_languages": audio_langs
                        })

            # 4. Freevee / miniTV / IMDb TV
            elif (
                tech_name in ("amazonimdbtv", "amazonminitv") or
                "freevee" in clear_lower or
                "free with ads" in clear_lower
            ):
                if (quality, clear_name) not in seen_free:
                    seen_free.add((quality, clear_name))
                    entry["free_ads"].append({
                        "quality": quality,
                        "package": clear_name,
                        "url": offer_url,
                        "audio_languages": audio_langs
                    })

            # 5. Amazon Video Digital Store (Rent / Buy)
            elif (
                tech_name in ("amazon", "amazoninstantvideo") or
                clear_lower == "amazon video"
            ):
                if mon == "RENT":
                    if (quality, price) not in seen_rent:
                        seen_rent.add((quality, price))
                        entry["rent"].append({
                            "quality": quality,
                            "price": price,
                            "url": offer_url,
                            "audio_languages": audio_langs
                        })
                elif mon == "BUY":
                    if (quality, price) not in seen_buy:
                        seen_buy.add((quality, price))
                        entry["buy"].append({
                            "quality": quality,
                            "price": price,
                            "url": offer_url,
                            "audio_languages": audio_langs
                        })

        if any((entry["netflix_sub"], entry["prime_sub"], entry["prime_channels"], entry["free_ads"], entry["rent"], entry["buy"])):
            parsed[country_code] = entry

    return parsed


# ─────────────────────────────────────────────────────────────────────────────
# Output Formatting
# ─────────────────────────────────────────────────────────────────────────────

def render_banner():
    width = 66
    print()
    print(f"{Style.CYAN}{Style.BOLD}{'=' * width}{Style.RESET}")
    print(f"{Style.CYAN}{Style.BOLD}     Global Streaming Region Availability Checker{Style.RESET}")
    print(f"{Style.WHITE}          Prime Video  &  Netflix  (130 Countries){Style.RESET}")
    print(f"{Style.CYAN}{Style.BOLD}{'=' * width}{Style.RESET}")
    print()


def render_title_info(info: dict, total_found: int, service_counts: dict):
    year_str = f" ({info['year']})" if info.get("year") else ""
    raw_type = info.get("type", "Movie")
    mtype = "Movie" if raw_type.upper() == "MOVIE" else "Show" if raw_type.upper() == "SHOW" else raw_type
    
    print(f"{Style.BOLD}Title      :{Style.RESET} {Style.BOLD}{info.get('title')}{Style.RESET}{year_str}")
    print(f"{Style.BOLD}Type       :{Style.RESET} {Style.CYAN}{mtype}{Style.RESET}")
    if info.get("netflix_url"):
        print(f"{Style.BOLD}Netflix    :{Style.RESET} {Style.RED}{info.get('netflix_url')}{Style.RESET}")
    if info.get("prime_url"):
        print(f"{Style.BOLD}Prime Video:{Style.RESET} {Style.GREEN}{info.get('prime_url')}{Style.RESET}")
    if info.get("audio_languages"):
        audio_str = ", ".join(info["audio_languages"])
        print(f"{Style.BOLD}Main Audio :{Style.RESET} {Style.YELLOW}{audio_str}{Style.RESET}")
    
    counts_str = []
    if "netflix" in service_counts and service_counts["netflix"] > 0:
        counts_str.append(f"{Style.RED}{service_counts['netflix']} on Netflix{Style.RESET}")
    if "prime" in service_counts and service_counts["prime"] > 0:
        counts_str.append(f"{Style.GREEN}{service_counts['prime']} on Prime Video{Style.RESET}")
    
    summary_txt = f" ({', '.join(counts_str)})" if counts_str else ""
    print(f"\n{Style.GREEN}{Style.BOLD}[FOUND]{Style.RESET}  Available in {Style.BOLD}{Style.GREEN}{total_found}{Style.RESET} region(s){summary_txt}:")


def format_quality_badge(qualities: List[str]) -> str:
    """Return compact quality badge [4K], [HD], or [SD]."""
    if "4K UHD" in qualities:
        return "[4K]"
    elif "HD" in qualities:
        return "[HD]"
    elif "SD" in qualities:
        return "[SD]"
    return ""


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes for accurate character length."""
    return re.sub(r"\033\[[0-9;]*m", "", text)


def render_columns_table(heading: str, items: List[str], num_cols: int = 3, heading_color: str = Style.CYAN):
    """
    Render items arranged across 3 columns separated by '  |  '.
    Formatted exactly as:
    -- Heading --
        [AU] Australia [4K]  |  [AT] Austria [4K]  |  [BE] Belgium [4K]
    """
    if not items:
        return

    print(f"\n{heading_color}{Style.BOLD}-- {heading} --{Style.RESET}")
    total = len(items)

    col_widths = [0] * num_cols
    for idx, item in enumerate(items):
        col_idx = idx % num_cols
        col_widths[col_idx] = max(col_widths[col_idx], len(strip_ansi(item)))

    sep = f"  {Style.GRAY}|{Style.RESET}  "
    for i in range(0, total, num_cols):
        row_items = items[i:i + num_cols]
        row_strs = []
        for c_idx, item in enumerate(row_items):
            vis_len = len(strip_ansi(item))
            pad = " " * (col_widths[c_idx] - vis_len)
            row_strs.append(item + pad)
        print("    " + sep.join(row_strs))
    print()


def display_results(
    title_info: dict,
    results: Dict[str, dict],
    total_checked: int,
    filter_flags: dict
):
    """Format and print categorized availability results."""
    service_filter = (filter_flags.get("service") or "all").lower()
    include_netflix = service_filter in ("all", "netflix")
    include_prime = service_filter in ("all", "prime")

    category_filters = [
        filter_flags.get("sub"),
        filter_flags.get("free"),
        filter_flags.get("channels"),
        filter_flags.get("rent_buy")
    ]
    show_all = not any(category_filters)
    show_sub = show_all or bool(filter_flags.get("sub"))
    show_free = show_all or bool(filter_flags.get("free"))
    show_channels = show_all or bool(filter_flags.get("channels"))
    show_rent_buy = show_all or bool(filter_flags.get("rent_buy"))
    only_4k = bool(filter_flags.get("only_4k", False))

    # Filter by 4K and selected services
    filtered_results = {}
    for code, data in results.items():
        if only_4k:
            has_4k = any(
                item.get("quality") == "4K UHD"
                for cat in ("netflix_sub", "prime_sub", "prime_channels", "free_ads", "rent", "buy")
                for item in data[cat]
            )
            if not has_4k:
                continue

        has_netflix = bool(data["netflix_sub"]) and include_netflix
        has_prime = any((data["prime_sub"], data["prime_channels"], data["free_ads"], data["rent"], data["buy"])) and include_prime

        if has_netflix or has_prime:
            filtered_results[code] = data

    total_unique = len(filtered_results)

    # Compute per-service totals
    service_counts = {}
    if include_netflix:
        service_counts["netflix"] = sum(1 for d in filtered_results.values() if d["netflix_sub"])
    if include_prime:
        service_counts["prime"] = sum(1 for d in filtered_results.values() if any((d["prime_sub"], d["prime_channels"], d["free_ads"], d["rent"], d["buy"])))

    render_title_info(title_info, total_unique, service_counts)

    if total_unique == 0:
        svc_label = "Netflix or Prime Video" if service_filter == "all" else service_filter.capitalize()
        print(f"\n  {Style.YELLOW}Not available on {svc_label} in any checked region.{Style.RESET}\n")
        return

    # ── 1. NETFLIX SUBSCRIPTION ──────────────────────────────────────────────
    if include_netflix and show_sub:
        netflix_regions = [
            (code, data) for code, data in filtered_results.items()
            if data["netflix_sub"] and (not only_4k or any(x["quality"] == "4K UHD" for x in data["netflix_sub"]))
        ]
        netflix_regions.sort(key=lambda x: x[1]["country_name"])

        if netflix_regions:
            items = []
            for code, data in netflix_regions:
                quals = list({x["quality"] for x in data["netflix_sub"] if not only_4k or x["quality"] == "4K UHD"})
                q_badge = format_quality_badge(quals)
                q_color = Style.BRIGHT_CYAN if q_badge == "[4K]" else Style.CYAN
                q_str = f" {q_color}{q_badge}{Style.RESET}" if q_badge else ""
                has_ads = any(x.get("has_ads") for x in data["netflix_sub"])
                ads_note = f" {Style.YELLOW}(w/ Ads){Style.RESET}" if has_ads else ""
                items.append(f"[{Style.BOLD}{code}{Style.RESET}] {data['country_name']}{q_str}{ads_note}")
            render_columns_table(f"Netflix Subscription ({len(netflix_regions)} regions)", items, num_cols=3, heading_color=Style.RED)

    # ── 2. PRIME SUBSCRIPTION ────────────────────────────────────────────────
    if include_prime and show_sub:
        sub_regions = [
            (code, data) for code, data in filtered_results.items()
            if data["prime_sub"] and (not only_4k or any(x["quality"] == "4K UHD" for x in data["prime_sub"]))
        ]
        sub_regions.sort(key=lambda x: x[1]["country_name"])

        if sub_regions:
            items = []
            for code, data in sub_regions:
                quals = list({x["quality"] for x in data["prime_sub"] if not only_4k or x["quality"] == "4K UHD"})
                q_badge = format_quality_badge(quals)
                q_color = Style.BRIGHT_CYAN if q_badge == "[4K]" else Style.CYAN
                q_str = f" {q_color}{q_badge}{Style.RESET}" if q_badge else ""
                has_ads = any(x.get("has_ads") for x in data["prime_sub"])
                ads_note = f" {Style.YELLOW}(w/ Ads){Style.RESET}" if has_ads else ""
                items.append(f"[{Style.BOLD}{code}{Style.RESET}] {data['country_name']}{q_str}{ads_note}")
            render_columns_table(f"Prime Video Subscription ({len(sub_regions)} regions)", items, num_cols=3, heading_color=Style.GREEN)

    # ── 3. FREE WITH ADS / FREEVEE ───────────────────────────────────────────
    if include_prime and show_free:
        free_regions = [
            (code, data) for code, data in filtered_results.items()
            if data["free_ads"] and (not only_4k or any(x["quality"] == "4K UHD" for x in data["free_ads"]))
        ]
        free_regions.sort(key=lambda x: x[1]["country_name"])

        if free_regions:
            items = []
            for code, data in free_regions:
                quals = list({x["quality"] for x in data["free_ads"] if not only_4k or x["quality"] == "4K UHD"})
                q_badge = format_quality_badge(quals)
                q_color = Style.BRIGHT_CYAN if q_badge == "[4K]" else Style.CYAN
                q_str = f" {q_color}{q_badge}{Style.RESET}" if q_badge else ""
                sources = list({x["package"] for x in data["free_ads"]})
                source_str = f"  ({', '.join(sources)})" if sources else ""
                items.append(f"[{Style.BOLD}{code}{Style.RESET}] {data['country_name']}{q_str}{source_str}")
            render_columns_table(f"Free with Ads / Freevee ({len(free_regions)} regions)", items, num_cols=3, heading_color=Style.YELLOW)

    # ── 4. PRIME VIDEO CHANNELS ──────────────────────────────────────────────
    if include_prime and show_channels:
        channel_regions = [
            (code, data) for code, data in filtered_results.items()
            if data["prime_channels"] and (not only_4k or any(x["quality"] == "4K UHD" for x in data["prime_channels"]))
        ]
        channel_regions.sort(key=lambda x: x[1]["country_name"])

        if channel_regions:
            items = []
            for code, data in channel_regions:
                chans = sorted(list({x["channel"] for x in data["prime_channels"] if not only_4k or x["quality"] == "4K UHD"}))
                quals = list({x["quality"] for x in data["prime_channels"] if not only_4k or x["quality"] == "4K UHD"})
                q_badge = format_quality_badge(quals)
                q_color = Style.BRIGHT_CYAN if q_badge == "[4K]" else Style.CYAN
                q_str = f" {q_color}{q_badge}{Style.RESET}" if q_badge else ""
                chan_str = f"  ({', '.join(chans)})" if chans else ""
                items.append(f"[{Style.BOLD}{code}{Style.RESET}] {data['country_name']}{q_str}{chan_str}")
            render_columns_table(f"Prime Video Channels ({len(channel_regions)} regions)", items, num_cols=3, heading_color=Style.MAGENTA)

    # ── 5. RENT / BUY ON AMAZON STORE ────────────────────────────────────────
    if include_prime and show_rent_buy:
        store_regions = [
            (code, data) for code, data in filtered_results.items()
            if (data["rent"] or data["buy"]) and (
                not only_4k or
                any(x["quality"] == "4K UHD" for x in data["rent"]) or
                any(x["quality"] == "4K UHD" for x in data["buy"])
            )
        ]
        store_regions.sort(key=lambda x: x[1]["country_name"])

        if store_regions:
            items = []
            for code, data in store_regions:
                rent_quals = [x for x in data["rent"] if not only_4k or x["quality"] == "4K UHD"]
                buy_quals = [x for x in data["buy"] if not only_4k or x["quality"] == "4K UHD"]
                all_quals = list({x["quality"] for x in (rent_quals + buy_quals)})
                q_badge = format_quality_badge(all_quals)
                q_color = Style.BRIGHT_CYAN if q_badge == "[4K]" else Style.CYAN
                q_str = f" {q_color}{q_badge}{Style.RESET}" if q_badge else ""

                rent_prices = list(dict.fromkeys(r["price"] for r in rent_quals if r.get("price")))
                buy_prices = list(dict.fromkeys(b["price"] for b in buy_quals if b.get("price")))

                price_parts = []
                if rent_prices:
                    price_parts.append(f"Rent: {'/'.join(rent_prices)}")
                if buy_prices:
                    price_parts.append(f"Buy: {'/'.join(buy_prices)}")

                price_val = " / ".join(price_parts)
                p_str = f"  {Style.YELLOW}({price_val}){Style.RESET}" if price_val else ""
                items.append(f"[{Style.BOLD}{code}{Style.RESET}] {data['country_name']}{q_str}{p_str}")

            render_columns_table(f"Amazon Store Rent / Buy ({len(store_regions)} regions)", items, num_cols=2, heading_color=Style.BLUE)

    # Summary
    print(f"{Style.BOLD}Total: {Style.GREEN}{total_unique}{Style.RESET}{Style.BOLD} region(s) with availability{Style.RESET}\n")


# ─────────────────────────────────────────────────────────────────────────────
# JSON Serializer
# ─────────────────────────────────────────────────────────────────────────────

def format_export_json(title_info: dict, results: Dict[str, dict], total_checked: int) -> dict:
    return {
        "title": title_info.get("title"),
        "year": title_info.get("year"),
        "type": title_info.get("type"),
        "urls": {
            "netflix": title_info.get("netflix_url"),
            "prime_video": title_info.get("prime_url"),
            "justwatch": title_info.get("url"),
            "imdb": title_info.get("imdb_url"),
            "tmdb": title_info.get("tmdb_url"),
        },
        "main_audio_languages": title_info.get("audio_languages", []),
        "runtime_minutes": title_info.get("runtime"),
        "total_seasons": title_info.get("seasons"),
        "imdb_score": title_info.get("imdb"),
        "tmdb_score": title_info.get("tmdb"),
        "genres": title_info.get("genres", []),
        "external_ids": {
            "justwatch_id": title_info.get("id"),
            "imdb_id": title_info.get("imdbId"),
            "tmdb_id": title_info.get("tmdbId"),
        },
        "summary": {
            "netflix_regions": sum(1 for d in results.values() if d["netflix_sub"]),
            "prime_subscription_regions": sum(1 for d in results.values() if d["prime_sub"]),
            "free_ads_regions": sum(1 for d in results.values() if d["free_ads"]),
            "prime_channels_regions": sum(1 for d in results.values() if d["prime_channels"]),
            "digital_store_regions": sum(1 for d in results.values() if d["rent"] or d["buy"]),
            "total_available_regions": len(results),
            "total_regions_checked": total_checked,
        },
        "regions": results,
    }


def save_results_to_json(
    title_info: dict,
    results: Dict[str, dict],
    total_checked: int,
    custom_path: Optional[str] = None
) -> str:
    export_data = format_export_json(title_info, results, total_checked)
    if custom_path:
        out_path = custom_path
    else:
        title_slug = re.sub(r"[^\w\-_\. ]", "_", title_info.get("title", "title")).strip()
        title_slug = title_slug.replace(" ", "_")
        year_slug = f"_{title_info['year']}" if title_info.get("year") else ""
        out_path = f"regions_{title_slug}{year_slug}.json"

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(export_data, f, indent=2, ensure_ascii=False)

    return os.path.abspath(out_path)


# ─────────────────────────────────────────────────────────────────────────────
# Interactive Selection
# ─────────────────────────────────────────────────────────────────────────────

def pick_title_interactively(results: List[dict], non_interactive: bool = False, from_url: bool = False) -> Optional[dict]:
    """Prompt user to select from search results or auto-pick."""
    if not results:
        return None
    if len(results) == 1 or non_interactive or not sys.stdin.isatty():
        return results[0]

    top_score = results[0].get("match_score", 0)
    if from_url and top_score >= 90:
        year_str = f" ({results[0]['year']})" if results[0].get("year") else ""
        print(f"  {Style.GREEN}✓ Auto-selected best match:{Style.RESET} {Style.BOLD}{results[0]['title']}{Style.RESET}{year_str}\n")
        return results[0]

    display_limit = min(5, len(results))
    print(f"  Found {len(results)} matching titles:\n")
    for i, r in enumerate(results[:display_limit], 1):
        year = f" ({r['year']})" if r.get("year") else ""
        rec = " (Recommended)" if i == 1 and top_score >= 90 else ""
        print(f"    {Style.CYAN}{i:>2}.{Style.RESET} {Style.BOLD}{r['title']}{Style.RESET}{year}{Style.DIM}{rec}{Style.RESET}")
    print()

    while True:
        try:
            choice = input(f"  Enter number [1-{display_limit}] (or 0 to cancel): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return None

        if choice == "0":
            return None
        if choice.isdigit() and 1 <= int(choice) <= display_limit:
            return results[int(choice) - 1]
        print(f"  {Style.RED}Invalid selection. Please enter 1 to {display_limit}, or 0.{Style.RESET}")


# ─────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Check Amazon Prime Video and Netflix availability worldwide across 130 regions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
        Examples:
          python prime_region_checker.py "https://www.netflix.com/in/title/80137461"
          python prime_region_checker.py "Inception"
          python prime_region_checker.py "Stranger Things" --netflix
          python prime_region_checker.py "The Boys" --prime
          python prime_region_checker.py "Dune" --sub --4k
          python prime_region_checker.py "Fallout" -c US,GB,DE,IN,JP
          python prime_region_checker.py "Inception" --save
          python prime_region_checker.py "tm92641" --json
        """),
    )
    parser.add_argument("title", nargs="?", help="Movie / show title, JustWatch ID (tm.../ts...), or Prime/Netflix URL")
    parser.add_argument("-t", "--type", choices=["movie", "show"], default=None,
                        help="Filter search by content type (movie or show)")
    parser.add_argument("--year", type=int, default=None,
                        help="Filter search by release year (e.g. 2021)")
    parser.add_argument("-c", "--country", "--region", dest="countries", default=None,
                        help="Comma-separated country codes to check (e.g. 'US,GB,IN' or 'all')")
    parser.add_argument("--service", choices=["all", "netflix", "prime"], default="all",
                        help="Filter availability by streaming service (default: all)")
    parser.add_argument("-N", "--netflix", dest="filter_netflix", action="store_true",
                        help="Display only Netflix availability")
    parser.add_argument("-P", "--prime", dest="filter_prime", action="store_true",
                        help="Display only Amazon Prime Video availability")
    parser.add_argument("--sub", "--subscription-only", action="store_true",
                        help="Display only regions included with subscription")
    parser.add_argument("--free", action="store_true",
                        help="Display only Freevee / Free with Ads regions")
    parser.add_argument("--channels", action="store_true",
                        help="Display only Prime Video add-on channels")
    parser.add_argument("--rent-buy", action="store_true",
                        help="Display only digital Rent / Buy regions")
    parser.add_argument("--prices", "--details", dest="show_prices", action="store_true",
                        help="Display detailed prices for Rent / Buy regions")
    parser.add_argument("--4k", dest="only_4k", action="store_true",
                        help="Display only regions where 4K UHD is available")
    parser.add_argument("-s", "--save", action="store_true",
                        help="Automatically save results to a JSON file")
    parser.add_argument("-o", "--output", default=None,
                        help="Custom path for saving results JSON file")
    parser.add_argument("--json", action="store_true",
                        help="Output pure JSON to stdout (suppresses banner & console formatting)")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Automatically select the first search result without prompting")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Run non-interactively; never prompt for input")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable ANSI terminal colors")

    args = parser.parse_args()

    # Resolve service filter
    service_mode = args.service
    if args.filter_netflix and not args.filter_prime:
        service_mode = "netflix"
    elif args.filter_prime and not args.filter_netflix:
        service_mode = "prime"

    if args.no_color or args.json:
        Style.disable()

    if not args.json:
        render_banner()

    # 1. Determine title / ID
    query_str = args.title
    if not query_str:
        if args.non_interactive or not sys.stdin.isatty():
            print("ERROR: Title argument is required in non-interactive mode.", file=sys.stderr)
            sys.exit(1)
        try:
            query_str = input("  Enter movie or TV show title: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Exiting.")
            sys.exit(0)
        if not query_str:
            print("  No title entered. Exiting.")
            sys.exit(0)

    # 2. Extract JustWatch ID if direct ID or URL was passed
    node_id = None
    target_type = None
    from_url = False

    url_match = re.search(r"justwatch\.com/[^/]+/(movie|tv-show)/([^/?#]+)", query_str)
    direct_id_match = re.match(r"^(t[ms]\d+)$", query_str.strip())

    if direct_id_match:
        node_id = direct_id_match.group(1)
        target_type = "MOVIE" if node_id.startswith("tm") else "SHOW"
    elif url_match:
        from_url = True
        url_type = "movie" if url_match.group(1) == "movie" else "show"
        slug_query = url_match.group(2).replace("-", " ")
        if not args.json:
            print(f"  Detected JustWatch URL. Searching for: '{slug_query}'...")
        results = search_titles(slug_query, url_type)
        if results:
            node_id = results[0]["id"]
            target_type = results[0]["type"]
    elif "netflix.com" in query_str and "title" in query_str:
        from_url = True
        if not args.json:
            print(f"  Detected Netflix URL. Fetching title details...")
        extracted_title = extract_title_from_netflix_url(query_str)
        if extracted_title:
            if not args.json:
                print(f"  Extracted Title: '{extracted_title}'\n")
            query_str = extracted_title
        else:
            if not args.json:
                print(f"  Could not automatically extract title from Netflix URL. Searching by URL slug/ID...")
            id_match = re.search(r"title/(\d+)", query_str)
            if id_match:
                query_str = id_match.group(1)
    elif "primevideo.com" in query_str or "amazon." in query_str or "gti=" in query_str:
        from_url = True
        if not args.json:
            print(f"  Detected Prime Video URL. Fetching title details...")
        extracted_title = extract_title_from_prime_url(query_str)
        if extracted_title:
            if not args.json:
                print(f"  Extracted Title: '{extracted_title}'\n")
            query_str = extracted_title

    # 3. Perform search if not a direct ID
    if not node_id:
        if not args.json:
            print(f"  Searching for \"{query_str}\" ...\n")
        try:
            results = search_titles(query_str, media_type=args.type, year=args.year)
        except requests.exceptions.ConnectionError:
            print(f"  {Style.RED}[ERROR] Network connection failed.{Style.RESET}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"  {Style.RED}[ERROR] {e}{Style.RESET}", file=sys.stderr)
            sys.exit(1)

        if not results:
            if args.json:
                print(json.dumps({"error": f"No results found for '{query_str}'"}))
            else:
                print(f"  {Style.YELLOW}No results found for \"{query_str}\".{Style.RESET}\n")
            sys.exit(0)

        selected = pick_title_interactively(results, non_interactive=(args.yes or args.non_interactive), from_url=from_url)
        if not selected:
            sys.exit(0)

        node_id = selected["id"]
        target_type = selected["type"]

    # 4. Resolve countries to check
    if args.countries and args.countries.strip().lower() != "all":
        req_codes = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
        target_countries = [c for c in req_codes if c in ALL_LOCALES]
        if not target_countries:
            print(f"  {Style.RED}[ERROR] No valid country codes provided in '{args.countries}'.{Style.RESET}", file=sys.stderr)
            sys.exit(1)
    else:
        target_countries = list(ALL_LOCALES.keys())

    # 5. Fetch title details and offers in batched GraphQL request
    if not args.json:
        services_label = "Netflix & Prime Video" if service_mode == "all" else service_mode.capitalize()
        print(f"  Checking {services_label} availability across {len(target_countries)} regions... ", end="", flush=True)

    t0 = time.time()
    try:
        title_metadata, raw_offers = fetch_title_and_offers(node_id, target_countries, target_type)
    except requests.exceptions.ConnectionError:
        print(f"\n  {Style.RED}[ERROR] Network error during region check.{Style.RESET}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n  {Style.RED}[ERROR] {e}{Style.RESET}", file=sys.stderr)
        sys.exit(1)

    elapsed = time.time() - t0
    if not args.json:
        print(f"{Style.GREEN}Done!{Style.RESET} ({elapsed:.2f}s)\n")

    # 6. Parse and classify offers
    parsed_regions = parse_region_availability(raw_offers)

    # If original input was a direct Netflix URL and title_metadata didn't have one, keep canonical input URL
    if "netflix.com" in (args.title or "") and not title_metadata.get("netflix_url"):
        title_metadata["netflix_url"] = resolve_netflix_url(args.title)

    # 7. Output results
    if args.json:
        export_data = format_export_json(title_metadata, parsed_regions, len(target_countries))
        print(json.dumps(export_data, indent=2, ensure_ascii=False))
    else:
        filter_flags = {
            "service": service_mode,
            "sub": args.sub,
            "free": args.free,
            "channels": args.channels,
            "rent_buy": args.rent_buy,
            "only_4k": args.only_4k,
            "show_prices": args.show_prices,
        }
        display_results(title_metadata, parsed_regions, len(target_countries), filter_flags)

    # 8. Save results
    if args.save or args.output:
        saved_file = save_results_to_json(title_metadata, parsed_regions, len(target_countries), args.output)
        if not args.json:
            print(f"  {Style.GREEN}✓ Results saved to:{Style.RESET} {saved_file}\n")
    elif not args.json and not args.non_interactive and sys.stdin.isatty():
        try:
            choice = input(f"  Save results to JSON? [y/N]: ").strip().lower()
            if choice == "y":
                saved_file = save_results_to_json(title_metadata, parsed_regions, len(target_countries))
                print(f"  {Style.GREEN}✓ Results saved to:{Style.RESET} {saved_file}\n")
        except (KeyboardInterrupt, EOFError):
            pass


if __name__ == "__main__":
    main()
