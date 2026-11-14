import base64
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from typing import List, Optional, Dict, Any, Tuple

import click
import m3u8
import requests

from vinetrimmer.objects import AudioTrack, TextTrack, Title, Tracks, VideoTrack
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils.collections import as_list
from vinetrimmer.utils import try_get
from vinetrimmer.vendor.pymp4.parser import Box
from vinetrimmer.utils.widevine.device import LocalDevice


class AppleTV(BaseService):
    """
    Service code for Apple's TV streaming service (https://tv.apple.com).
          
    \b
    Authorization: Cookies
    Security: 
    - PR SL3000 4K / 1080p
    - WV L1 / L3
    """

    ALIASES = ["ATV", "appletv"]
    TITLE_RE = [
        r"^(?:https?://tv\.apple\.com(?:/[a-z]{2})?/(?:movie|show|episode)/[a-z0-9-]+/)?(?P<id>umc\.cmc\.[a-z0-9]+)",
        r"^(?:https?://(?:geo\.)?itunes\.apple\.com(?:/[a-z]{2})?/(?:movie|tv-season)(?:/.+?)?/)?id(?P<id>\d+)(?:\?.+)?$",
        r"^(?P<id>umc\.cmc\..+)$",
        r"^(?P<id>\d+)$",
    ]

    VIDEO_CODEC_MAP = {
        "H264": ["avc"],
        "H265": ["hvc", "hev", "dvh"]
    }
    
    AUDIO_CODEC_MAP = {
        "AAC": ["HE", "stereo"],
        "AC3": ["ac3"],
        "EC3": ["ec3", "atmos"]
    }

    BASE_URL = "https://tv.apple.com"
    USER_AGENT_MANIFEST = "com.apple.tv/1.0 iOS/16.1 AppleTV/16.1 model/AppleTV6,2 hwp/t8011 build/20K71 (3; dt:163)"
    USER_AGENT_MANIFEST_ATMOS = "com.apple.tv/1.0 iOS/16.1 AppleTV/16.1 model/AppleTV6,2 hwp/t8011 build/20K71 (3; dt:163) #atmos"

    DEVICE_CONFIG = {
        "web": {"utscf": "OjAAAAAAAAA~", "utsk": "6e3013c6d6fae3c2::::::baf1a0dbeffe95a4", "caller": "web", "sf": "143441", "v": "56", "pfm": "web", "locale": "en-US"},
        "android": {"utscf": "OjAAAAAAAAA~", "utsk": "6e3013c6d6fae3c2::::::efc34e1f4b91a286", "caller": "vz", "sf": "143441", "v": "56", "pfm": "vz", "mfr": "AndroidTV", "locale": "en-US"},
    }

    ENDPOINTS = {
        "title": "https://tv.apple.com/api/uts/v3/{type}/{id}",
        "tv_episodes": "https://tv.apple.com/api/uts/v3/shows/{id}/episodes",
        "manifest": "https://tv.apple.com/api/uts/v2/view/product/{id}/personalized",
        "lookup": "https://uts-api.itunes.apple.com/uts/v3/contents/lookup",
        "license": "https://play-edge.itunes.apple.com/WebObjects/MZPlayLocal.woa/wa/fpsRequest",
        "storefronts": "https://uts-api.itunes.apple.com/uts/v3/storefronts",
        "certificate": "https://play-edge.itunes.apple.com/WebObjects/MZPlayLocal.woa/wa/widevineCert",
        "search": "https://uts-api.itunes.apple.com/uts/v3/search",
    }

    LICENSE_RESPONSE_CODES = {
        -1001: "Invalid PSSH.",
        -1002: "You do not own the title in the requested quality.",
        -1003: "Invalid request URI or user agent.",
        -1004: "Maximum number of simultaneous streams exceeded for your account.",
        -1008: "Maximum number of simultaneous streams exceeded for this rental.",
        -1009: "Rental expired.",
        -1012: "Invalid user agent.",
        -1017: "This content is geo-restricted.",
        -1021: "Device has insufficient security level or is blacklisted/revoked.",
        130001: "Device has insufficient security level or is revoked.",
        170101: "Invalid license challenge. This may also mean the device's serial number is revoked.",
        190107: "License denied for test device.",
        190116: "License denied by Widevine. Possibly a test device.",
        190120: "Device has insufficient security level or is revoked.",
        190121: "Device has been permanently revoked by Widevine.",
        190122: "Authorization has been denied for this request. Device may be blacklisted.",
        190612: "The device's serial number is revoked permanently.",
    }

    # ISO-Country Code to Storefront ID mapping
    STOREFRONT_MAP = {
        "US": "143441", "GB": "143444", "DE": "143443", "FR": "143442", 
        "CA": "143455", "AU": "143460", "JP": "143462", "BR": "143503",
        "MX": "143468", "ES": "143454", "IT": "143450", "NL": "143452",
        "SE": "143456", "CH": "143459", "AT": "143445", "BE": "143446",
        "DK": "143458", "FI": "143447", "IE": "143449", "NO": "143457",
        "PT": "143453", "PL": "143478", "RU": "143469", "TR": "143480",
        "CN": "143465", "HK": "143463", "TW": "143470", "KR": "143466",
        "SG": "143464", "IN": "143467", "AE": "143481", "SA": "143479",
        "ZA": "143472", "NZ": "143461", "CL": "143483", "CO": "143501",
        "AR": "143505", "PE": "143507", "VE": "143502", "PH": "143474",
        "MY": "143473", "TH": "143475", "ID": "143476", "VN": "143471",
        "IL": "143491", "EG": "143516", "NG": "143561", "KE": "143529"
    }

    @staticmethod
    @click.command(name="AppleTV", short_help="https://tv.apple.com")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return AppleTV(ctx, **kwargs)

    def _get_wanted_value(self, ctx):
        current = ctx
        depth = 0
        while current is not None and depth < 10:
            params = getattr(current, "params", {}) or {}
            value = params.get("wanted")
            if value not in (None, "", [], ()):
                return value
            current = getattr(current, "parent", None)
            depth += 1
        return None

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.raw_title = title or ctx.parent.params.get("title")
        self.wanted = self._get_wanted_value(ctx)
        self.last_episode_only = isinstance(self.wanted, str) and self.wanted.strip().lower() == "last-episode"
        self.cdm = ctx.obj.cdm
        self.vcodec = ctx.parent.params["vcodec"]
        self.acodec = ctx.parent.params["acodec"]
        self.alang = ctx.parent.params["alang"]
        self.subs_only = ctx.parent.params["subs_only"]
        
        self.extra_server_parameters = None
        self.storefront = None
        self.developer_token = None
        self.watermarktoken = None
        self.watermark_param = None
        self.dynamic_license_url = None
        self.rental_id = None
        self.is_itunes = False
        
        self.is_playready = (getattr(self.cdm.device, "type", None) == LocalDevice.Types.PLAYREADY)
        self.device_config = self.DEVICE_CONFIG["android"] if self.is_playready else self.DEVICE_CONFIG["web"]
        
        try:
            from curl_cffi import requests as cffi_requests
            self.cffi_session = cffi_requests.Session(impersonate="chrome124")
            for cookie in self.session.cookies:
                self.cffi_session.cookies.set(cookie.name, cookie.value, domain=getattr(cookie, "domain", ""), path=getattr(cookie, "path", "/"))
        except Exception:
            self.cffi_session = None

        self.configure()
        self.parse_title(ctx, self.raw_title)

    def _http_get(self, url: str, **kwargs):
        if self.cffi_session is not None:
            headers = kwargs.pop("headers", None)
            req_headers = dict(self.session.headers)
            if headers:
                req_headers.update(headers)
            return self.cffi_session.get(url, headers=req_headers, **kwargs)
        return self.session.get(url, **kwargs)

    def _http_post(self, url: str, **kwargs):
        if self.cffi_session is not None:
            headers = kwargs.pop("headers", None)
            req_headers = dict(self.session.headers)
            if headers:
                req_headers.update(headers)
            return self.cffi_session.post(url, headers=req_headers, **kwargs)
        return self.session.post(url, **kwargs)

    def parse_title(self, ctx, title):
        super().parse_title(ctx, title)
        if self.title and self.title.isnumeric():
            self.log.info(f" - Resolving numeric iTunes ID: {self.title}")
            resolved = self.lookup_itunes_id(self.title)
            if resolved:
                self.log.info(f" - Resolved to Apple TV content ID: {resolved}")
                self.title = resolved
            else:
                self.log.warning(f" - Failed to resolve iTunes ID: {self.title}, using with 'umc.cmc.' prefix")
                if not self.title.startswith("umc.cmc."):
                    self.title = f"umc.cmc.{self.title}"
        elif self.title and not self.title.startswith("umc.cmc."):
            self.title = f"umc.cmc.{self.title}"

    def lookup_itunes_id(self, itunes_id: str) -> Optional[str]:
        """Convert an iTunes numeric ID to an Apple TV+ content ID."""
        try:
            params = {"ids": itunes_id, **self.device_config}
            res = self._http_get(self.ENDPOINTS["lookup"], params=params).json()
            content = (res.get("data") or {}).get("content") or {}
            values = list(content.values())
            return values[0]["id"] if values else None
        except Exception as e:
            self.log.error(f"Failed to look up iTunes ID {itunes_id}: {e}")
            return None

    def configure(self):
        """Configure session with developer token, storefront, and headers."""
        self.log.info("Configuring Apple TV session...")
        self._get_developer_token()
        self._set_storefront()
        self._update_session_headers()

    def _get_environment_config(self) -> Optional[dict]:
        """Loads environment config data from the WEB App's serialized server data."""
        try:
            res = self._http_get(self.BASE_URL).text
            script_match = re.search(r'<script[^>]*id=["\']serialized-server-data["\'][^>]*>(.*?)</script>', res, re.DOTALL)
            if script_match:
                data = json.loads(script_match.group(1).strip())
                candidates = data if isinstance(data, list) else ([data] if not isinstance(data.get("data"), list) else data["data"])
                for item in candidates:
                    if not isinstance(item, dict):
                        continue
                    cfg = (item.get("data") or {}).get("configureParams")
                    if isinstance(cfg, dict) and cfg:
                        return cfg
        except Exception as e:
            self.log.debug(f"Failed to parse serialized server data: {e}")
        return None

    def _get_developer_token(self):
        """Acquire developer token from serialized server data or meta tags."""
        self.log.info("Acquiring developer token...")
        
        # 1. Try serialized server data
        env = self._get_environment_config()
        if env and env.get("developerToken"):
            self.developer_token = env["developerToken"]
            self.log.info("✓ Got developer token from serialized server data")
            return

        # 2. Try direct regex from HTML
        try:
            res = self._http_get(self.BASE_URL).text
            direct_match = re.search(r'"developerToken":"([^"]+)"', res)
            if direct_match:
                self.developer_token = direct_match.group(1)
                self.log.info("✓ Got developer token from page JSON")
                return

            env_match = re.search(r'web-tv-app/config/environment"[\s\S]*?content="([^"]+)', res)
            if env_match:
                env_data = json.loads(unquote(env_match.group(1)))
                token = env_data.get('MEDIA_API', {}).get('token') or env_data.get('developerToken')
                if token:
                    self.developer_token = token
                    self.log.info("✓ Got developer token from environment meta tag")
                    return
        except Exception as e:
            self.log.debug(f"Error scraping developer token: {e}")

        # 3. Fallback to Apple Music
        try:
            r = self._http_get("https://music.apple.com/us/browse")
            inline_tokens = re.findall(r'eyJh[A-Za-z0-9\._-]{100,}', r.text)
            if inline_tokens:
                self.developer_token = max(inline_tokens, key=len)
                self.log.info("✓ Got developer token from Apple Music")
                return
        except Exception as e:
            self.log.debug(f"Failed to get token from Apple Music: {e}")

        if not self.developer_token:
            raise ValueError("No Apple TV developer token could be obtained")

    def _set_storefront(self):
        """Set storefront based on URL region, cookies, or dynamic storefronts endpoint."""
        region = None

        # 1. Extract region from URL if available
        if self.raw_title:
            url_match = re.search(r'apple\.com/([a-z]{2})/(?:movie|show|episode|tv-season)', self.raw_title, re.IGNORECASE)
            if url_match:
                region = url_match.group(1).upper()
                self.log.info(f"Detected region '{region}' from title URL")

        # 2. Fallback to 'itua' cookie
        if not region:
            for cookie in self.session.cookies:
                if cookie.name == "itua" and cookie.value:
                    region = cookie.value.strip().upper()[:2]
                    break

        if region:
            try:
                resp = self._http_get(self.ENDPOINTS["storefronts"], params=self.device_config)
                if resp.status_code == 200:
                    sf_id = resp.json().get("data", {}).get(region, {}).get("storefrontId")
                    if sf_id:
                        self.storefront = str(sf_id)
                        self.log.info(f"Auto-detected storefront ID: {self.storefront} (Region: {region})")
                        return
            except Exception as e:
                self.log.debug(f"Storefronts API lookup error: {e}")

        if region and region in self.STOREFRONT_MAP:
            self.storefront = self.STOREFRONT_MAP[region]
            self.log.info(f"Storefront from static map: {self.storefront} ({region})")
        else:
            self.storefront = "143441"  # Default to US
            self.log.info(f"Defaulting to US storefront: {self.storefront}")

    def _update_session_headers(self):
        """Update session headers with authorization and media user tokens."""
        media_token = None
        for cookie in self.session.cookies:
            if cookie.name in ("media-user-token", "media_user_token") and cookie.value:
                media_token = cookie.value
                break

        headers = {
            "User-Agent": self.USER_AGENT_MANIFEST,
            "Authorization": f"Bearer {self.developer_token}",
            "Accept": "application/json",
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/",
        }

        if media_token:
            headers["media-user-token"] = media_token
            headers["x-apple-music-user-token"] = media_token
            self.log.info("✓ Attached media-user-token headers")
        else:
            self.log.warning("No 'media-user-token' cookie found - authenticated playback may fail")

        self.session.headers.update(headers)
        if self.cffi_session is not None:
            self.cffi_session.headers.update(headers)

    def _get_base_params(self):
        """Get base parameters for API requests."""
        return {
            **self.device_config,
            "sf": self.storefront or "143441",
            "l": "en",
            "ctx_brand": "tvs.sbd.4000",
            "count": "100",
            "skip": "0",
        }

    def _get_title_info(self) -> Optional[Dict]:
        """Get title information from API."""
        for media_type in ["shows", "movies"]:
            try:
                params = self._get_base_params()
                url = self.ENDPOINTS["title"].format(type=media_type, id=self.title)
                response = self._http_get(url=url, params=params)
                if response.status_code == 200:
                    data = response.json()
                    content = data.get("data", {}).get("content")
                    if content:
                        return content
            except Exception as e:
                self.log.debug(f"Title info lookup error for {media_type}: {e}")
        return None

    def get_titles(self) -> List[Title]:
        """Get title(s) based on the provided ID."""
        title_info = self._get_title_info()
        if not title_info:
            raise self.log.exit(f" - Title ID {self.title!r} could not be found.")

        content_type = title_info.get("type", "").lower()
        if content_type == "movie" or title_info.get("type") == "Movie":
            return self._process_movie_title(title_info)
        else:
            return self._process_tv_series(title_info)

    def _process_movie_title(self, title_info: Dict) -> List[Title]:
        """Process movie title information."""
        release_date = title_info.get("releaseDate")
        year = None
        if release_date:
            try:
                year = datetime.utcfromtimestamp(release_date / 1000).year
            except (TypeError, ValueError, OSError):
                pass

        return [Title(
            id_=self.title,
            type_=Title.Types.MOVIE,
            name=title_info.get("title", "Unknown"),
            year=year,
            original_lang=title_info.get("originalSpokenLanguages", [{}])[0].get("locale", "en"),
            source=self.ALIASES[0],
            service_data={"id": self.title, "content": title_info}
        )]

    def _process_tv_series(self, title_info: Dict) -> List[Title]:
        """Process TV series episodes."""
        params = self._get_base_params()
        params["includeSeasonSummary"] = "true"
        url = self.ENDPOINTS["tv_episodes"].format(id=self.title)

        response = self._http_get(url=url, params=params)
        response.raise_for_status()

        data = response.json().get("data", {})
        episodes = data.get("episodes", [])
        total = data.get("totalEpisodeCount", 0)

        if total > len(episodes):
            params["nextToken"] = f"0:{total}"
            res2 = self._http_get(url=url, params=params).json().get("data", {})
            episodes = res2.get("episodes", []) or episodes

        show_title = title_info.get("title", "Unknown")
        titles = []
        for ep in episodes:
            ep_id = ep.get("id")
            if not ep_id:
                continue

            titles.append(Title(
                id_=ep_id,
                type_=Title.Types.TV,
                name=ep.get("showTitle") or show_title,
                season=ep.get("seasonNumber", 1),
                episode=ep.get("episodeNumber", 1),
                episode_name=ep.get("title"),
                original_lang=title_info.get("originalSpokenLanguages", [{}])[0].get("locale", "en"),
                source=self.ALIASES[0],
                service_data={"id": ep_id, "content": ep}
            ))

        if self.last_episode_only and titles:
            return [titles[-1]]

        return titles

    def _get_stream_data(self, content_id: str) -> Dict:
        """Get stream data for content ID."""
        params = self._get_base_params()
        url = self.ENDPOINTS["manifest"].format(id=content_id)
        response = self._http_get(url=url, params=params)
        response.raise_for_status()
        data = response.json()
        playables = data.get("data", {}).get("content", {}).get("playables", [])
        if not playables:
            raise ValueError(f"No playable streams found for content ID: {content_id}")
        return playables[0]

    def _get_manifest_url_and_metadata(self, stream_data: dict) -> Tuple[str, bool, Optional[str], Optional[dict], Optional[str]]:
        """Resolve the HLS manifest URL and DRM-relevant metadata from playables entry."""
        is_itunes = stream_data.get("isItunes", False)
        has_access = stream_data.get("isEntitledToPlay", False)

        if stream_data.get("isComingSoon") or stream_data.get("assets", {}).get("kind") == "preorder":
            raise ValueError("This title is not yet available for streaming")

        if is_itunes:
            itunes_data = stream_data.get("itunesMediaApiData", {})
            variant_priority = {"UHD": 0, "HD": 1, "SD": 2}

            if has_access:
                offers = sorted(itunes_data.get("personalizedOffers", []), key=lambda x: variant_priority.get(x.get("variant"), float("inf")))
                if not offers:
                    raise ValueError("Could not get manifest, title may not be released yet.")
                manifest_url = offers[0].get("hlsUrl")
                rental_id = offers[0].get("rentalId")
            else:
                self.log.warning("User is not entitled to play this iTunes title, attempting preview stream...")
                offers = sorted([o for o in itunes_data.get("offers", []) if o.get("kind") == "buy"], key=lambda x: variant_priority.get(x.get("variant"), float("inf")))
                if not offers:
                    raise ValueError("Could not get preview manifest, title may not be released yet.")
                manifest_url = offers[0].get("hlsUrl")
                rental_id = None

            if not manifest_url:
                raise ValueError("Could not get manifest URL from iTunes offers.")
            return manifest_url, True, rental_id, None, None

        if not has_access:
            raise self.log.exit(" - User is not entitled to play this title (active subscription required)")

        assets = stream_data.get("assets", {})
        manifest_url = assets.get("hlsUrl")
        if not manifest_url:
            raise ValueError("Could not get HLS manifest URL from assets.")

        extra_server_parameters = assets.get("fpsKeyServerQueryParameters")
        if not extra_server_parameters:
            qs = parse_qs(urlparse(manifest_url).query)
            adam_id = (qs.get("a") or [None])[0]
            svc_id = (qs.get("svcId") or [None])[0]
            if adam_id or svc_id:
                extra_server_parameters = {"adamId": adam_id, "svcId": svc_id}

        dynamic_license_url = assets.get("fpsKeyServerUrl")
        return manifest_url, False, None, extra_server_parameters, dynamic_license_url

    def get_tracks(self, title: Title) -> Tracks:
        """Get available tracks for the title."""
        content_id = title.service_data.get("id", self.title)
        stream_data = self._get_stream_data(content_id)

        manifest_url, is_itunes, rental_id, extra_params, dynamic_license = self._get_manifest_url_and_metadata(stream_data)

        self.extra_server_parameters = extra_params
        self.dynamic_license_url = dynamic_license
        self.rental_id = rental_id
        self.is_itunes = is_itunes

        self.log.info(f" - Fetching HLS manifest from: {manifest_url}")
        master_playlist = self._fetch_hls_manifest(manifest_url)
        tracks = self._parse_tracks_from_hls(master_playlist, manifest_url)

        # Extract w parameter & watermarkingToken
        self.watermarktoken = None
        self.watermark_param = None

        if hasattr(master_playlist, 'playlists') and master_playlist.playlists:
            for playlist in master_playlist.playlists:
                if hasattr(playlist, 'uri') and playlist.uri:
                    match_w = re.search(r'[?&]w=([^&]+)', playlist.uri)
                    if match_w:
                        self.watermark_param = match_w.group(1)
                        self.log.info(f" - Found w parameter in playlist URI: {self.watermark_param}")
                        break

                if hasattr(playlist, 'keys') and playlist.keys:
                    for key in playlist.keys:
                        if key and hasattr(key, 'uri') and key.uri:
                            match_w = re.search(r'[?&]w=([^&]+)', key.uri)
                            if match_w:
                                self.watermark_param = match_w.group(1)
                                self.log.info(f" - Found w parameter in EXT-X-KEY URI: {self.watermark_param}")
                                break
                if self.watermark_param:
                    break

        for track in tracks:
            if isinstance(track, VideoTrack):
                match_token = re.search(r'watermarkingToken=([^&]+)', track.url)
                if match_token:
                    self.watermarktoken = unquote(match_token.group(1))
                    self.log.info(f" - Found watermarking token: {self.watermarktoken}")
                if not self.watermark_param:
                    match_w = re.search(r'[?&]w=([^&]+)', track.url)
                    if match_w:
                        self.watermark_param = match_w.group(1)
                        self.log.info(f" - Found w parameter in video URL: {self.watermark_param}")

        tracks = self._filter_and_enhance_tracks(tracks, title)
        return tracks

    def get_chapters(self, title: Title) -> List:
        """Get chapters for the title (currently not supported)."""
        return []

    def certificate(self, **_) -> Optional[bytes]:
        """Get Widevine certificate."""
        return None  # Uses common privacy cert

    def _fetch_hls_manifest(self, hls_url: str) -> m3u8.M3U8:
        """Fetch and parse HLS manifest."""
        headers = {"User-Agent": self.USER_AGENT_MANIFEST}
        response = self._http_get(url=hls_url, headers=headers)
        response.raise_for_status()
        return m3u8.loads(response.text, hls_url)

    def _preserve_playready_key_uri(self, track) -> Optional[str]:
        """Fetch media playlist and keep the complete PlayReady EXT-X-KEY URI."""
        try:
            track_url = as_list(track.url)[0] if track.url else None
            if not track_url:
                return getattr(track, "full_key_uri", None)

            qs = parse_qs(urlparse(track_url).query)
            key_info = (qs.get("keyInfo") or [None])[0]

            response = self._http_get(
                track_url,
                headers={"User-Agent": self.USER_AGENT_MANIFEST},
                timeout=15
            )
            response.raise_for_status()

            # 1. Search directly in raw playlist text for EXT-X-KEY
            for line in response.text.splitlines():
                if line.startswith("#EXT-X-KEY"):
                    if "com.microsoft.playready" in line or "data:text/plain" in line or "9a04f079-9840-4286-ab92-e65be0885f95" in line:
                        m_uri = re.search(r'URI=["\']?(data:text/plain[^"\'\s]+)["\']?', line)
                        if m_uri:
                            uri = m_uri.group(1)
                            track.full_key_uri = uri
                            if "," in uri:
                                track.pr_pssh = uri.split(",", 1)[1]
                            self.log.info(" - Preserved PlayReady key URI from EXT-X-KEY")
                            return uri

            # 2. If keyInfo was in the variant URL, build URI with keyInfo
            if key_info and hasattr(track, "pr_pssh") and track.pr_pssh:
                pssh_str = track.pr_pssh if isinstance(track.pr_pssh, str) else base64.b64encode(track.pr_pssh).decode()
                wm_prefix = f"watermarkingToken={self.watermarktoken};" if self.watermarktoken else ""
                uri = f"data:text/plain;{wm_prefix}keyInfo={key_info};w={self.watermark_param or 'Om'};charset=UTF-16;base64,{pssh_str}"
                track.full_key_uri = uri
                self.log.info(" - Constructed PlayReady key URI with keyInfo")
                return uri

        except Exception as e:
            self.log.debug(f"Failed to preserve PlayReady key URI: {e}")

        return getattr(track, "full_key_uri", None)

    def _parse_tracks_from_hls(self, master_playlist: m3u8.M3U8, base_url: str) -> Tracks:
        """Parse tracks from HLS master playlist."""
        tracks = Tracks.from_m3u8(
            master=master_playlist,
            source=self.ALIASES[0]
        )

        # Store original manifest data
        for track in tracks:
            if hasattr(track, 'extra'):
                track.extra = {"manifest": track.extra}
            else:
                track.extra = {"manifest": None}

        return tracks

    def _filter_and_enhance_tracks(self, tracks: Tracks, title: Title = None) -> Tracks:
        """Filter and enhance track information."""
        # Filter video tracks by codec
        if self.vcodec and self.vcodec in self.VIDEO_CODEC_MAP:
            tracks.videos = [
                x for x in tracks.videos
                if any(codec in (x.codec or "").lower() for codec in self.VIDEO_CODEC_MAP[self.vcodec])
            ]

        # Filter audio tracks by codec
        if self.acodec and self.acodec in self.AUDIO_CODEC_MAP:
            tracks.audios = [
                x for x in tracks.audios
                if any(codec in (x.codec or "").lower() for codec in self.AUDIO_CODEC_MAP[self.acodec])
            ]

        # Enhance track information
        for track in tracks:
            self._enhance_track_info(track)

        # Keep all non-SDH subtitle streams visible and available
        subtitle_candidates = [
            x for x in tracks.subtitles
            if not getattr(x, "sdh", False)
        ]

        # Performance:
        # do NOT probe every video/subtitle playlist for duration.
        # Use title metadata as episode duration and score subtitles by URL only.
        target_duration = self._get_title_duration_seconds(title)

        preferred_video_urls = [v.url for v in tracks.videos if "vod-ak" in v.url]
        if not preferred_video_urls:
            preferred_video_urls = [v.url for v in tracks.videos]

        grouped = {}
        for sub in subtitle_candidates:
            key = (
                self._base_lang_code(getattr(sub, "language", None)),
                getattr(sub, "forced", False),
            )
            grouped.setdefault(key, []).append(sub)

        selected_subtitles = []

        for _, group in grouped.items():
            best_sub = None
            best_score = None

            for sub in group:
                url_score = self._best_url_similarity(sub.url, preferred_video_urls)

                # Prefer:
                # 1) vod-ak subtitle URLs
                # 2) highest path similarity to chosen video ladder
                # 3) shortest URL only as stable tie-breaker
                score = (
                    0 if "vod-ak" in sub.url else 1,
                    -url_score,
                    len(sub.url),
                )

                if best_score is None or score < best_score:
                    best_score = score
                    best_sub = sub

            if best_sub is not None:
                best_sub = self._trim_subtitle_track_to_main_block(best_sub, target_duration)
                selected_subtitles.append(best_sub)

        tracks.subtitles = selected_subtitles

        preferred_audio_lang = self._get_preferred_audio_lang(tracks, title)

        for sub in tracks.subtitles:
            setattr(sub, "default", False)

        if preferred_audio_lang:
            default_sub = next((
                sub for sub in tracks.subtitles
                if not getattr(sub, "forced", False)
                and self._base_lang_code(getattr(sub, "language", None))
                and self._base_lang_code(getattr(sub, "language", None)) != preferred_audio_lang
            ), None)

            if default_sub is not None:
                setattr(default_sub, "default", True)
                self.log.info(
                    f" - AppleTV: subtitle {default_sub.language} set as default "
                    f"(audio: {preferred_audio_lang})"
                )

        # Filter by CDN & Deduplicate identical tracks across redundant Apple CDN ladders
        # Apple TV serves 2 or more CDN mirrors (e.g. vod-ak, vod-fa, vod-aoc) in the same manifest
        seen_cdn = None
        for cdn_prefix in ["vod-ak", "vod-fa", "vod-aoc"]:
            if any(cdn_prefix in getattr(x, "url", "") for x in tracks.videos):
                seen_cdn = cdn_prefix
                break

        if seen_cdn:
            filtered_videos = [x for x in tracks.videos if seen_cdn in getattr(x, "url", "")]
            filtered_audios = [x for x in tracks.audios if seen_cdn in getattr(x, "url", "")]
        else:
            filtered_videos = tracks.videos
            filtered_audios = tracks.audios

        # Deduplicate identical tracks by stream signature
        unique_videos = []
        seen_vid_keys = set()
        for v in filtered_videos:
            key = (v.width, v.height, v.bitrate, v.codec, getattr(v, "fps", None), getattr(v, "hdr10", False), getattr(v, "dv", False))
            if key not in seen_vid_keys:
                seen_vid_keys.add(key)
                unique_videos.append(v)

        unique_audios = []
        seen_aud_keys = set()
        for a in filtered_audios:
            key = (a.language, a.codec, a.bitrate, getattr(a, "channels", None), getattr(a, "descriptive", False), getattr(a, "atmos", False))
            if key not in seen_aud_keys:
                seen_aud_keys.add(key)
                unique_audios.append(a)

        return Tracks(unique_videos + unique_audios + tracks.subtitles)

    def _enhance_track_info(self, track):
        """Enhance track with additional information."""
        if isinstance(track, VideoTrack):
            track.encrypted = True
            track.needs_ccextractor_first = False
            
            # Try to determine quality from URL
            if track.extra.get("manifest") and track.extra["manifest"].uri:
                uri = track.extra["manifest"].uri
            #    for quality_str, quality_val in self.config["quality_map"].items():
            #        if quality_str.lower() in uri.lower():
            #            track.extra["quality"] = quality_val
            #            break
        
        elif isinstance(track, AudioTrack):
            track.encrypted = True

            # Extract bitrate from every place Apple/Vinetrimmer may keep it.
            # Some Apple audio tracks do not expose the original manifest URI in
            # track.url, but it is still available as track.extra["manifest"].uri.
            search_parts = []

            if getattr(track, "url", None):
                search_parts.append(str(track.url))

            manifest = None
            if getattr(track, "extra", None):
                manifest = track.extra.get("manifest")

            manifest_uri = getattr(manifest, "uri", None)
            if manifest_uri:
                search_parts.append(str(manifest_uri))

            search_in = " ".join(search_parts)
            bitrate_match = (
                re.search(r"[?&]g=(\d+)(?:&|$)", search_in)
                or re.search(r"_gr(\d+)_", search_in)
            )

            if bitrate_match:
                bitrate_str = bitrate_match.group(1)
                if len(bitrate_str) >= 3:
                    track.bitrate = int(bitrate_str[-3:]) * 1000

            # Apple sometimes exposes an AAC stereo fallback rendition without
            # bitrate markers. In Vinetrimmer this AAC track can have codec
            # value "stereo" (see AUDIO_CODEC_MAP), not literal "AAC".
            if not getattr(track, "bitrate", None):
                codec_text = str(getattr(track, "codec", "") or "").lower()
                channels_text = str(getattr(track, "channels", "") or "").lower()
                if (
                    "stereo" in codec_text
                    or "aac" in codec_text
                    or codec_text == "he"
                    or "2.0" in channels_text
                ):
                    track.bitrate = 64000

            # Clean up codec string
            if track.codec:
                track.codec = track.codec.replace("_vod", "")
        
        elif isinstance(track, TextTrack):
            track.codec = "vtt"

    def _base_lang_code(self, lang) -> Optional[str]:
        if not lang:
            return None

        try:
            text = str(lang).lower()
        except Exception:
            return None

        return text.split("-")[0].split("_")[0]

    def _get_preferred_audio_lang(self, tracks: Tracks, title: Title = None) -> Optional[str]:
        requested = as_list(self.alang)

        requested_codes = [self._base_lang_code(x) for x in requested]
        for code in requested_codes:
            if code and code not in ("all", "orig"):
                return code

        if "orig" in requested_codes and title and getattr(title, "original_lang", None):
            code = self._base_lang_code(title.original_lang)
            if code:
                return code

        first_audio = next(iter(tracks.audios), None)
        if first_audio:
            return self._base_lang_code(getattr(first_audio, "language", None))

        return None

    def _get_title_duration_seconds(self, title: Title = None) -> Optional[float]:
        """Extract title duration in seconds from Apple metadata."""
        if not title or not getattr(title, "service_data", None):
            return None

        data = title.service_data

        for key in (
            "durationInMilliseconds",
            "durationInMs",
            "durationMilliseconds",
            "duration",
            "runtimeInMilliseconds",
        ):
            value = data.get(key)
            if value is None:
                continue

            try:
                value = float(value)
            except (TypeError, ValueError):
                continue

            # Apple usually stores milliseconds
            if value > 10000:
                return value / 1000.0
            return value

        return None

    def _best_url_similarity(self, subtitle_url: str, video_urls: List[str]) -> int:
        """Return the best common path-prefix score against selected video URLs."""
        best = 0
        subtitle_base = subtitle_url.split("?")[0]

        for video_url in video_urls:
            video_base = video_url.split("?")[0]
            score = self._common_prefix_len(subtitle_base, video_base)
            if score > best:
                best = score

        return best

    def _common_prefix_len(self, a: str, b: str) -> int:
        """Length of common prefix between two strings."""
        n = min(len(a), len(b))
        i = 0
        while i < n and a[i] == b[i]:
            i += 1
        return i

    def _trim_subtitle_track_to_main_block(self, track: TextTrack, target_duration: Optional[float]):
        """Return subtitle track with original HTTP playlist URL."""
        return track

    def _split_subtitle_playlist_blocks(self, playlist) -> List[List]:
        """Split subtitle playlist into blocks separated by EXT-X-DISCONTINUITY."""
        blocks = []
        current = []

        for segment in getattr(playlist, "segments", []):
            if getattr(segment, "discontinuity", False) and current:
                blocks.append(current)
                current = []

            current.append(segment)

        if current:
            blocks.append(current)

        return blocks

    def _sum_segments_duration(self, segments: List) -> float:
        """Sum EXTINF durations for a list of HLS segments."""
        total = 0.0

        for segment in segments:
            try:
                total += float(segment.duration or 0)
            except (TypeError, ValueError):
                pass

        return total

    def _write_local_subtitle_playlist(self, source_url: str, segments: List) -> Optional[str]:
        """
        Write a local subtitle playlist containing only the selected main block.
        Segment URIs are rewritten to absolute URLs.
        """
        try:
            cache_dir = os.path.join(tempfile.gettempdir(), "appletv_subtitles")
            os.makedirs(cache_dir, exist_ok=True)

            digest_source = source_url + "|" + "|".join(getattr(seg, "uri", "") for seg in segments)
            digest = hashlib.md5(digest_source.encode("utf-8")).hexdigest()
            out_path = os.path.join(cache_dir, f"{digest}.m3u8")

            target_duration = 1
            for seg in segments:
                try:
                    seg_duration = int(float(seg.duration or 0))
                    if seg_duration > target_duration:
                        target_duration = seg_duration
                except (TypeError, ValueError):
                    pass

            lines = [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                "#EXT-X-PLAYLIST-TYPE:VOD",
                f"#EXT-X-TARGETDURATION:{target_duration}",
                "#EXT-X-MEDIA-SEQUENCE:0",
            ]

            for seg in segments:
                try:
                    duration = float(seg.duration or 0)
                except (TypeError, ValueError):
                    duration = 0.0

                seg_uri = getattr(seg, "absolute_uri", None) or urljoin(source_url, seg.uri)

                lines.append(f"#EXTINF:{duration:.3f},")
                lines.append(seg_uri)

            lines.append("#EXT-X-ENDLIST")

            with open(out_path, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + "\n")

            return out_path

        except Exception:
            return None

    def _lease(self, streaming_key: dict, license_url: Optional[str] = None) -> dict:
        """Send a lease request to the Apple TV+ license server."""
        payload = {"streaming-request": {"version": 1, "streaming-keys": [streaming_key]}}
        url = license_url or self.dynamic_license_url or self.ENDPOINTS["license"]
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/",
            "x-apple-renewal": "true",
        }
        res = self._http_post(url, json=payload, headers=headers)
        res.raise_for_status()
        return res.json()

    def license(self, challenge: bytes, track, title, **_) -> bytes:
        """Request license from Apple license server and manage streaming lease."""
        w_param = self.watermark_param or ""
        wm_prefix = f"watermarkingToken={self.watermarktoken};" if self.watermarktoken else ""

        if self.is_playready:
            key_system = "com.microsoft.playready"
            if hasattr(track, 'pr_pssh'):
                if isinstance(track.pr_pssh, str):
                    pssh_bytes = base64.b64decode(track.pr_pssh)
                else:
                    pssh_bytes = track.pr_pssh
            else:
                pssh_bytes = b''
            pssh_b64 = base64.b64encode(pssh_bytes).decode('utf-8')

            uri = self._preserve_playready_key_uri(track)
            if not uri:
                if getattr(self, "is_itunes", False):
                    uri = f"data:text/plain;{wm_prefix}w={w_param or 'Om'};charset=UTF-16;base64,{pssh_b64}"
                else:
                    if w_param:
                        uri = f"data:text/plain;{wm_prefix}w={w_param};charset=UTF-16;base64,{pssh_b64}"
                    else:
                        uri = f"data:text/plain;{wm_prefix}charset=UTF-16;base64,{pssh_b64}"

            challenge_b64 = base64.b64encode(challenge).decode('utf-8')
            key_id = 0
        else:
            key_system = "com.widevine.alpha"
            pssh_box = Box.build(track.pssh) if hasattr(track, 'pssh') else b''
            pssh_b64 = base64.b64encode(pssh_box).decode()

            if w_param:
                uri = f"data:text/plain;w={w_param};base64,{pssh_b64}"
            else:
                uri = f"data:text/plain;base64,{pssh_b64}"

            challenge_b64 = base64.b64encode(challenge).decode()
            key_id = 1

        adam_id = (self.extra_server_parameters or {}).get('adamId', '')
        svc_id = (self.extra_server_parameters or {}).get('svcId', '')

        streaming_key = {
            "id": key_id,
            "uri": uri,
            "challenge": challenge_b64,
            "key-system": key_system,
            "lease-action": "start",
            "isExternal": True,
        }

        if getattr(self, "is_itunes", False):
            if getattr(self, "rental_id", None):
                streaming_key["rental-id"] = self.rental_id
        else:
            if adam_id:
                streaming_key["adamId"] = adam_id
            if svc_id:
                streaming_key["svcId"] = svc_id

        license_url = self.dynamic_license_url or self.ENDPOINTS["license"]

        try:
            res_data = self._lease(streaming_key, license_url=license_url)
            res_key = res_data.get("streaming-response", {}).get("streaming-keys", [{}])[0]
            if "license" not in res_key:
                code = res_key.get("errorCode") or res_key.get("status")
                desc = self.LICENSE_RESPONSE_CODES.get(code, "Unknown error")
                raise ValueError(f"Failed to get Apple TV license: {desc} [{code}]")
            license_bytes = base64.b64decode(res_key["license"])
        finally:
            try:
                streaming_key["lease-action"] = "stop"
                self._lease(streaming_key, license_url=license_url)
            except Exception as e:
                self.log.debug(f"Failed to stop license lease: {e}")

        return license_bytes
