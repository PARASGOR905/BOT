import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Union
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote

import click
import requests

from vinetrimmer.config import directories
from vinetrimmer.objects import Title, Tracks, TextTrack, MenuTrack
from vinetrimmer.services.BaseService import BaseService

class Gain(BaseService):
    """
    Service code for the GAIN streaming service (https://www.gain.tv).
    Authorization: Cookies.
    Security: FHD@L3/SL2K.
    """

    ALIASES = ["GAIN", "gain", "gaintv"]
    GEOFENCE = ["tr"]
    TITLE_RE = r"^(?:https?://(?:www\.)?gain\.tv/(?:watch|izle|program|film|t|dizi|title)/)?(?P<id>[a-zA-Z0-9_-]+)(?:/.*)?$"

    @staticmethod
    @click.command(name="Gain", short_help="https://www.gain.tv", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return Gain(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self._setup_session()
        self.wv_license_url = None
        
        self.endpoints = self.config.get("endpoints", {
            "search": "https://api.gain.tv/v1/search",
            "playback": "https://api.gain.tv/2da7kf8jf/CALL/ProfileTitle/getPlaybackInfo/{profile_id}/?videoContentId={id}&packageType=Dash&__culture=tr-tr",
            "metadata": "https://api.gain.tv/2da7kf8jf/CALL/ProfileTitle/getPlaybackInfo/{profile_id}/?videoContentId={id}&packageType=Dash&__culture=tr-tr",
            "profile_title": "https://api.gain.tv/2da7kf8jf/CALL/ProfileTitle/getProfileTitleById/{profile_id}?titleId={title_id}&__culture=tr-tr",
            "profile_season": "https://api.gain.tv/2da7kf8jf/CALL/ProfileTitle/getProfileSeason/{profile_id}?seasonId={season_id}&titleId={title_id}&__culture=tr-tr"
        })

    def _get_auth_token(self):
        gaps_val = None
        if hasattr(self, "session") and self.session.cookies:
            gaps_val = self.session.cookies.get("GAPS")
        if not gaps_val and getattr(self, "cookies", None):
            if hasattr(self.cookies, "get"):
                gaps_val = self.cookies.get("GAPS")
            else:
                for c in self.cookies:
                    if getattr(c, "name", "") == "GAPS":
                        gaps_val = c.value
                        break

        if gaps_val:
            try:
                decoded = base64.b64decode(unquote(gaps_val)).decode("utf-8")
                data = json.loads(decoded)
                token = data.get("accessToken")
                if token:
                    self.log.debug("GAIN token extracted from GAPS cookie.")
                    return token
            except Exception as e:
                self.log.debug(f"Failed to extract token from GAPS cookie: {e}")

        for c_name in ["accessToken", "access_token", "auth_token", "token"]:
            val = self.session.cookies.get(c_name) if hasattr(self, "session") else None
            if val:
                return val

        try:
            profile = getattr(self, "profile", "default") or "default"
            for folder in ["gain", "GAIN"]:
                cookie_path = os.path.join(directories.cookies, folder, f"{profile}.txt")
                if os.path.isfile(cookie_path):
                    with open(cookie_path, "r", encoding="utf-8") as f:
                        raw = f.read().strip()
                    if raw and not raw.startswith("# Netscape") and "\t" not in raw:
                        first_line = raw.splitlines()[0].strip()
                        if first_line.lower().startswith("bearer "):
                            first_line = first_line[7:].strip()
                        if "." in first_line:
                            self.log.debug("GAIN token loaded from raw text cookie file.")
                            return first_line
        except Exception as e:
            self.log.debug(f"Raw cookie file check error: {e}")

        auth_token = self.config.get("auth_token")
        if auth_token:
            self.log.debug("GAIN token loaded from gain.yml (fallback).")
            return auth_token

        return None

    def _setup_session(self):
        self.session.headers.update({
            "User-Agent": self.config.get(
                "user_agent", 
                "Gain/1.2 (Android; TCL 55P89K; Build/QTG1.200305.005; Android 11; xlJbYkGTTBiS1hFjOsGmAbPckXKDHT4V;)"
            ),
            "Origin": "https://www.gain.tv",
            "Referer": "https://www.gain.tv/",
            "Accept": "*/*",
            "Content-Type": "application/json",
        })
        
        auth_token = self._get_auth_token()

        if auth_token:
            auth_token = unquote(str(auth_token)).strip()
            if auth_token.lower().startswith("bearer "):
                auth_token = auth_token[7:].strip()

            self.session.headers["Authorization"] = f"Bearer {auth_token}"
            self.log.debug("GAIN token loaded.")
        else:
            self.log.warning("GAIN auth token not found (check cookies/gain/default.txt or gain.yml).")

    def _api_request(self, method, url, **kwargs):
        try:
            response = self.session.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as e:
            status_code = e.response.status_code
            error_body = e.response.text
            self.log.error(f"Gain API Response [{status_code}]: {error_body}")
            raise self.log.exit(f"API request failed (HTTP {status_code}).")
        except json.JSONDecodeError:
            raise self.log.exit("API did not return valid JSON.")

    def _get_profile_id(self):
        profile_id = "92AGMFI0MLQ84Z11NMR0YZ4I"

        # 1. Extract profile ID from GUPS cookie
        gups_val = None
        if hasattr(self, "session") and self.session.cookies:
            gups_val = self.session.cookies.get("GUPS")
        if not gups_val and getattr(self, "cookies", None):
            if hasattr(self.cookies, "get"):
                gups_val = self.cookies.get("GUPS")
            else:
                for c in self.cookies:
                    if getattr(c, "name", "") == "GUPS":
                        gups_val = c.value
                        break

        if gups_val:
            try:
                decoded = base64.b64decode(unquote(gups_val)).decode("utf-8")
                data = json.loads(decoded)
                pid = (
                    data.get("persistedUserData", {})
                    .get("selectedProfile", {})
                    .get("id")
                )
                if pid:
                    return pid
            except Exception as e:
                self.log.debug(f"Failed to decode GUPS profileId: {e}")

        # 2. Extract profile ID from Authorization header JWT claims
        auth_header = self.session.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return profile_id

        try:
            token = auth_header[7:].strip()
            parts = token.split(".")
            if len(parts) != 3:
                return profile_id

            payload = parts[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            decoded = base64.urlsafe_b64decode(payload).decode("utf-8")
            claims = json.loads(decoded).get("claims", {})
            return claims.get("profileId") or json.loads(decoded).get("profileId") or profile_id
        except Exception as e:
            self.log.debug(f"Failed to decode JWT profileId, using default: {e}")
            return profile_id

    def _unwrap_data(self, response_data):
        if not isinstance(response_data, dict):
            return {}
        data = response_data.get("data", response_data)
        if isinstance(data, dict):
            return data
        return {}

    def _get_profile_title(self, title_id):
        profile_id = self._get_profile_id()
        url = self.endpoints.get("profile_title", "").format(
            profile_id=profile_id,
            title_id=title_id
        )
        self.log.debug(f"Requesting GAIN profile title: titleId={title_id}")
        response_data = self._api_request("GET", url)
        data = self._unwrap_data(response_data)
        if not data:
            raise self.log.exit(f"GAIN ProfileTitle response empty: titleId={title_id}")
        return data

    def _get_profile_season(self, season_id, title_id, profile_id):
        url = self.endpoints.get("profile_season", "").format(
            profile_id=profile_id,
            season_id=season_id,
            title_id=title_id
        )
        self.log.debug(f"Requesting GAIN season: titleId={title_id}, seasonId={season_id}")
        response_data = self._api_request("GET", url)
        return self._unwrap_data(response_data)

    def get_titles(self):
        content_id = self.title
        try:
            data = self._get_profile_title(content_id)
        except Exception as e:
            raise self.log.exit(f"Failed to get GAIN title info: {e}")

        self.title_data = data

        title_info = data.get("titleInfo", data)
        if not isinstance(title_info, dict):
            title_info = data

        if not data.get("id"):
            data["id"] = content_id

        content_type = title_info.get("contentType", {})
        if isinstance(content_type, dict):
            content_type_id = str(content_type.get("id", "")).upper()
        else:
            content_type_id = str(content_type).upper()

        title_type = str(title_info.get("type", data.get("type", ""))).upper()

        if (content_type_id == "FILM" or title_type in ("SINGLE", "MOVIE", "FILM") or str(data.get("type", "")).lower() == "movie"):
            name = title_info.get("name", data.get("title", "Unknown Movie"))
            raw_year = str(title_info.get("publishYear", data.get("release_year", ""))).strip()
            year_val = int(raw_year) if raw_year.isdigit() else 2000

            return [Title(
                id_=str(title_info.get("videoContentId", data.get("id"))),
                type_=Title.Types.MOVIE,
                name=name,
                year=year_val,
                source=self.ALIASES[0],
                service_data=data
            )]

        # It's a series
        episodes = []
        series_title = title_info.get("name", data.get("title", "Unknown Series"))
        actual_title_id = content_id
        profile_id = self._get_profile_id()

        seasons = data.get("seasons", [])
        if not isinstance(seasons, list):
            seasons = []

        if not seasons and isinstance(title_info.get("seasons"), list):
            seasons = title_info["seasons"]

        normalized_seasons = []
        seen_season_ids = set()

        for season in seasons:
            if not isinstance(season, dict):
                continue
            season_id = season.get("id") or season.get("seasonId")
            if season_id and season_id not in seen_season_ids:
                normalized_seasons.append(season)
                seen_season_ids.add(season_id)

        complete_seasons = []

        for season in normalized_seasons:
            season_id = season.get("id") or season.get("seasonId")
            season_data = season
            season_episodes = season_data.get("videoContents", season_data.get("episodes", []))
            if not isinstance(season_episodes, list):
                season_episodes = []
            
            if not season_episodes and actual_title_id and season_id:
                try:
                    season_data = self._get_profile_season(season_id, actual_title_id, profile_id)
                except Exception as e:
                    self.log.warning(f"Could not get season: seasonId={season_id}: {e}")
                    continue
            
            if isinstance(season_data, dict):
                complete_seasons.append(season_data)

        if not complete_seasons:
            possible_season_id = data.get("seasonId") or data.get("currentSeasonId") or title_info.get("seasonId")
            if possible_season_id and actual_title_id:
                try:
                    season_data = self._get_profile_season(possible_season_id, actual_title_id, profile_id)
                    if season_data:
                        complete_seasons.append(season_data)
                except Exception as e:
                    self.log.warning(f"Could not get current season: seasonId={possible_season_id}: {e}")

        for season in complete_seasons:
            s_num = season.get("seasonRank") or season.get("season_number")
            if not isinstance(s_num, int):
                sm = re.search(r"(\d+)\.\s*Sezon", str(season.get("name", "")), re.IGNORECASE)
                s_num = int(sm.group(1)) if sm else 1

            season_episodes = season.get("videoContents", season.get("episodes", []))
            if not isinstance(season_episodes, list):
                continue

            for ep in season_episodes:
                if not isinstance(ep, dict):
                    continue

                video_content_id = ep.get("videoContentId") or ep.get("id")
                if not video_content_id:
                    continue

                short_name = str(ep.get("shortName", ""))
                ep_name = str(ep.get("name", "")).strip()

                e_num = None
                short_match = re.search(r"S(\d+):B(\d+)", short_name, re.IGNORECASE)
                if short_match:
                    s_num = int(short_match.group(1))
                    e_num = int(short_match.group(2))

                if e_num is None:
                    name_match = re.search(r"(\d+)\.\s*Bölüm", ep_name, re.IGNORECASE)
                    if name_match:
                        e_num = int(name_match.group(1))

                if e_num is None:
                    rank = ep.get("rank", ep.get("episode_number"))
                    if isinstance(rank, int):
                        e_num = rank

                if e_num is None:
                    e_num = 1

                raw_year = str(ep.get("releaseYear", ep.get("publishYear", title_info.get("publishYear", "")))).strip()
                year_val = int(raw_year) if raw_year.isdigit() else 2000

                ep_data = dict(ep)
                if "imageInfo" not in ep_data and "imageInfo" in title_info:
                    ep_data["imageInfo"] = title_info["imageInfo"]
                if "logoImageUrl" not in ep_data and "logoImageUrl" in title_info:
                    ep_data["logoImageUrl"] = title_info["logoImageUrl"]
                if "titleInfo" not in ep_data:
                    ep_data["titleInfo"] = title_info

                episodes.append(Title(
                    id_=str(video_content_id),
                    type_=Title.Types.TV,
                    name=series_title,
                    season=s_num,
                    episode=e_num,
                    episode_name=ep_name or f"Episode {e_num}",
                    year=year_val,
                    source=self.ALIASES[0],
                    service_data=ep_data
                ))

        unique = {}
        for ep in episodes:
            key = (str(ep.id), getattr(ep, "season", 0), getattr(ep, "episode", 0))
            unique[key] = ep

        episodes = list(unique.values())
        episodes.sort(key=lambda ep: (getattr(ep, "season", 0), getattr(ep, "episode", 0), str(ep.id)))

        if not episodes:
            raise self.log.exit("No episodes found for the series.")

        self.log.info(f"GAIN: Found {len(episodes)} episodes.")
        return episodes

    def get_tracks(self, title):
        content_id = str(title.id)
        profile_id = self._get_profile_id()
        playback_endpoint = self.endpoints.get("playback", "")
        if "{profile_id}" in playback_endpoint:
            url = playback_endpoint.format(id=content_id, profile_id=profile_id)
        else:
            url = playback_endpoint.format(id=content_id)
        
        response_data = self._api_request("POST", url, data="")
        stream_data = response_data.get("data", response_data)

        if isinstance(getattr(title, "service_data", None), dict):
            title.service_data.update(stream_data)
        else:
            title.service_data = stream_data

        current_video_content = stream_data.get("currentVideoContent", {})
        
        m3u8_url = (
            current_video_content.get("playbackUrl") or 
            stream_data.get("hlsUrl") or 
            stream_data.get("url") or 
            stream_data.get("playbackUrl") or
            stream_data.get("mpd_url")
        )
        playback_token = current_video_content.get("playbackToken") or stream_data.get("playbackToken")

        if not m3u8_url:
            self.log.error(f"Raw Stream Response: {stream_data}")
            raise self.log.exit(f"HLS/MPD stream URL not found for '{title.id}'.")

        if playback_token and "playbackToken=" not in m3u8_url:
            m3u8_url = self._append_query_param(m3u8_url, "playbackToken", playback_token)

        self.log.debug(f"Manifest URL: {m3u8_url}")

        try:
            if ".mpd" in m3u8_url.lower() or "packageType=Dash" in m3u8_url:
                tracks = Tracks.from_mpd(url=m3u8_url, session=self.session, source=self.ALIASES[0])
            else:
                tracks = Tracks.from_m3u8(url=m3u8_url, session=self.session, source=self.ALIASES[0])
        except Exception as e:
            raise self.log.exit(f"Failed to parse manifest: {str(e)}")

        found_drm_url = None
        found_drm_token = None

        def search_drm_keys(data):
            nonlocal found_drm_url, found_drm_token
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, str):
                        if "gain.la.drm.cloud" in v:
                            found_drm_url = v
                        elif k.lower() in ["drmtoken", "widevinetoken", "licensetoken", "buydrmtoken"]:
                            found_drm_token = v
                    search_drm_keys(v)
            elif isinstance(data, list):
                for item in data:
                    search_drm_keys(item)

        search_drm_keys(stream_data)

        if found_drm_url:
            self.wv_license_url = found_drm_url
        elif found_drm_token:
            self.wv_license_url = f"https://gain.la.drm.cloud/acquire-license/widevine?BrandGuid=58abb79c-4ea7-4924-8482-2938f80236e9&UserToken={found_drm_token}"
        else:
            auth_header = self.session.headers.get("Authorization", "")
            clean_token = auth_header.replace("Bearer ", "").strip()
            self.wv_license_url = f"https://gain.la.drm.cloud/acquire-license/widevine?BrandGuid=58abb79c-4ea7-4924-8482-2938f80236e9&UserToken={clean_token}"

        subtitles = stream_data.get("subtitles", [])
        for sub in subtitles:
            sub_url = sub.get("url")
            if sub_url:
                sub_lang = sub.get("language", "tr")
                tracks.add(TextTrack(
                    id_=f"{title.id}_sub_{sub_lang}",
                    source=self.ALIASES[0],
                    url=sub_url,
                    codec="vtt" if ".vtt" in sub_url.lower() else "srt",
                    language=sub_lang,
                    forced=sub.get("forced", False),
                    sdh=sub.get("sdh", False)
                ))

        # Check and attach cover & logo images (attachments)
        attachments = self._extract_attachments(stream_data, title)
        if attachments:
            self._attach_cover_hook(title, tracks, attachments)

        return tracks

    def get_chapters(self, title):
        chapters = []
        data = getattr(title, "service_data", {}) or {}
        cvc = data.get("currentVideoContent", data)

        raw_chapters = cvc.get("chapters", [])
        
        import datetime
        def to_tc(seconds):
            return datetime.datetime.utcfromtimestamp(seconds).strftime("%H:%M:%S.%f")[:-3]
            
        if raw_chapters:
            for idx, chap in enumerate(raw_chapters, start=1):
                chap_title = str(chap.get("title") or chap.get("name") or f"Chapter {idx}")
                start_t = float(chap.get("start_time", chap.get("start", 0.0)))
                chapters.append(MenuTrack(
                    number=idx,
                    title=chap_title,
                    timecode=to_tc(start_t)
                ))
            return chapters

        intro_start = cvc.get("skipIntroStart")
        intro_end = cvc.get("skipIntroEnd")
        outro_start = cvc.get("skipNextEpisode")

        intro_start_s = (float(intro_start) / 1000.0) if intro_start is not None else None
        intro_end_s = (float(intro_end) / 1000.0) if intro_end is not None else None
        outro_start_s = (float(outro_start) / 1000.0) if outro_start is not None else None

        idx = 1
        if intro_start_s is not None and intro_start_s > 0:
            chapters.append(MenuTrack(number=idx, title="Prologue", timecode=to_tc(0.0)))
            idx += 1

        if intro_start_s is not None and intro_end_s is not None and intro_end_s > intro_start_s:
            chapters.append(MenuTrack(number=idx, title="Intro", timecode=to_tc(intro_start_s)))
            idx += 1

        main_content_start = intro_end_s if intro_end_s is not None else 0.0
        if intro_start_s is None or intro_start_s <= 0:
            chapters.append(MenuTrack(number=idx, title="Episode", timecode=to_tc(0.0)))
            idx += 1
        else:
            chapters.append(MenuTrack(number=idx, title="Episode", timecode=to_tc(main_content_start)))
            idx += 1

        if outro_start_s is not None and outro_start_s > main_content_start:
            chapters.append(MenuTrack(number=idx, title="Credits", timecode=to_tc(outro_start_s)))

        return chapters

    def _append_query_param(self, url, param_name, param_value):
        url_parts = list(urlparse(url))
        query = dict(parse_qs(url_parts[4]))
        query[param_name] = param_value
        url_parts[4] = urlencode(query, doseq=True)
        return urlunparse(url_parts)
        
    def license(self, challenge, *args, **kwargs):
        if not getattr(self, "wv_license_url", None):
            raise self.log.exit("License URL not set.")
            
        is_playready = False
        if isinstance(challenge, str) or (isinstance(challenge, bytes) and b"PlayReady" in challenge) or (isinstance(challenge, bytes) and b"xmlns" in challenge):
            is_playready = True
            
        if is_playready:
            url = self.wv_license_url.replace("/widevine", "/playready")
            custom_headers = {
                "Accept": "*/*",
                "Accept-Language": "tr-TR,tr;q=0.9",
                "Origin": "https://www.gain.tv",
                "Referer": "https://www.gain.tv/",
                "Content-Type": "text/xml; charset=utf-8", 
                "User-Agent": "Gain/1.2 (Android; TCL 55P89K; Build/QTG1.200305.005; Android 11; xlJbYkGTTBiS1hFjOsGmAbPckXKDHT4V;)"
            }
        else:
            url = self.wv_license_url
            custom_headers = {
                "Accept": "*/*",
                "Accept-Language": "tr-TR,tr;q=0.9",
                "Origin": "https://www.gain.tv",
                "Referer": "https://www.gain.tv/",
                "Sec-Ch-Ua": '"Brave";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site",
                "Content-Type": "application/octet-stream",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
            }

        try:
            res = self.session.post(
                url,
                data=challenge,
                headers=custom_headers
            )
            res.raise_for_status()
            return res.content
        except requests.HTTPError as e:
            self.log.error(f"License HTTP Error: {e.response.status_code} - {e.response.text}")
            raise self.log.exit(f"Failed to get license. HTTP {e.response.status_code}")

    def _extract_attachments(self, stream_data, title):
        sources = []
        if isinstance(getattr(title, "service_data", None), dict):
            sources.append(title.service_data)
            if isinstance(title.service_data.get("titleInfo"), dict):
                sources.append(title.service_data["titleInfo"])

        if hasattr(self, "title_data") and isinstance(self.title_data, dict):
            sources.append(self.title_data)
            if isinstance(self.title_data.get("titleInfo"), dict):
                sources.append(self.title_data["titleInfo"])

        if isinstance(stream_data, dict):
            sources.append(stream_data)

        cover_url = None
        logo_url = None

        # 1. Look for logoImageUrl (clearlogo / title treatment PNG)
        for src in sources:
            l_url = src.get("logoImageUrl") or src.get("logoUrl")
            if l_url and isinstance(l_url, str) and l_url.startswith("http"):
                logo_url = l_url
                break

        # 2. Look for cover photo (prioritize 2:3 vertical poster)
        best_cover = None
        for src in sources:
            image_info = src.get("imageInfo")
            if isinstance(image_info, list):
                for item in image_info:
                    if not isinstance(item, dict):
                        continue
                    img_url = item.get("imageUrl") or item.get("url")
                    if not img_url or not isinstance(img_url, str):
                        continue
                    use_type = str(item.get("useType", "")).lower()
                    ratio = str(item.get("ratio", "")).strip()

                    # Prioritize 2:3 coverPhoto / portrait poster
                    if use_type == "coverphoto" and ratio == "2:3":
                        cover_url = img_url
                        break
                    elif ratio == "2:3" and not best_cover:
                        best_cover = img_url

                if cover_url:
                    break

                if best_cover and not cover_url:
                    cover_url = best_cover
                    break

                # Fallback to any image in imageInfo
                for item in image_info:
                    if isinstance(item, dict):
                        img_url = item.get("imageUrl") or item.get("url")
                        if img_url and isinstance(img_url, str):
                            cover_url = img_url
                            break
                if cover_url:
                    break

            # Episode thumbnail fallback
            vc_images = src.get("videoContentImageInfo")
            if isinstance(vc_images, list) and not cover_url:
                for item in vc_images:
                    if isinstance(item, dict):
                        img_url = item.get("imageUrl") or item.get("url")
                        if img_url and isinstance(img_url, str):
                            cover_url = img_url
                            break
                if cover_url:
                    break

            # Direct image fields fallback
            for key in ["coverImageUrl", "posterImageUrl", "imageUrl"]:
                val = src.get(key)
                if val and isinstance(val, str) and val.startswith("http") and not cover_url:
                    cover_url = val
                    break
            if cover_url:
                break

        return {
            "cover": cover_url,
            "logo": logo_url
        }

    def _attach_cover_hook(self, title, tracks, attachments):
        if not attachments or not isinstance(attachments, dict):
            return

        clean_id = re.sub(r"[^\w\-]", "_", str(title.id))
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Referer": "https://www.gain.tv/",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
        }

        downloaded_attachments = []

        # Download Cover Poster
        cover_url = attachments.get("cover")
        if cover_url:
            try:
                res = requests.get(cover_url, headers=headers, timeout=15)
                if res.ok and len(res.content) >= 1024:
                    c_ext = ".png" if "image/png" in res.headers.get("Content-Type", "").lower() or ".png" in cover_url.lower().split("?")[0] else ".jpg"
                    c_mime = "image/png" if c_ext == ".png" else "image/jpeg"
                    temp_cover = os.path.join(tempfile.gettempdir(), f"gain_cover_{clean_id}{c_ext}")
                    with open(temp_cover, "wb") as f:
                        f.write(res.content)
                    downloaded_attachments.append({
                        "name": f"cover{c_ext}",
                        "mime": c_mime,
                        "codec": c_ext[1:].upper(),
                        "path": temp_cover
                    })
                    self.log.debug(f"Downloaded cover image: {cover_url} ({len(res.content)} bytes)")
            except Exception as e:
                self.log.debug(f"Error downloading cover image: {e}")

        # Download Title Logo (ClearLogo / Text PNG)
        logo_url = attachments.get("logo")
        if logo_url:
            try:
                res = requests.get(logo_url, headers=headers, timeout=15)
                if res.ok and len(res.content) >= 512:
                    l_ext = ".png"
                    l_mime = "image/png"
                    temp_logo = os.path.join(tempfile.gettempdir(), f"gain_logo_{clean_id}{l_ext}")
                    with open(temp_logo, "wb") as f:
                        f.write(res.content)
                    downloaded_attachments.append({
                        "name": "clearlogo.png",
                        "mime": l_mime,
                        "codec": "PNG",
                        "path": temp_logo
                    })
                    self.log.debug(f"Downloaded clearlogo PNG: {logo_url} ({len(res.content)} bytes)")
            except Exception as e:
                self.log.debug(f"Error downloading clearlogo image: {e}")

        if not downloaded_attachments:
            return

        def wrap_str(orig_str_fn):
            def custom_str(*args, **kwargs):
                base = orig_str_fn(*args, **kwargs)
                if "Attachment:" in base:
                    return base
                lines = []
                count = len(downloaded_attachments)
                lines.append(f"\n{count} Attachment{'s' if count > 1 else ''}:")
                for att in downloaded_attachments:
                    lines.append(f"+- ATT | {att['codec']} | {att['name']}")
                att_block = "\n".join(lines)
                return (base + att_block).strip() if base else att_block.strip()
            return custom_str

        def wrap_mux(orig_mux_fn):
            def mux_with_attachment(prefix, *args, **kwargs):
                muxed_location, returncode = orig_mux_fn(prefix, *args, **kwargs)
                if returncode in (0, 1) and os.path.exists(muxed_location):
                    valid_attachments = [a for a in downloaded_attachments if os.path.exists(a["path"])]
                    if valid_attachments:
                        mkvpropedit = shutil.which("mkvpropedit")
                        if not mkvpropedit:
                            root_bin = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "binaries", "mkvpropedit.exe" if os.name == "nt" else "mkvpropedit")
                            if os.path.isfile(root_bin):
                                mkvpropedit = root_bin
                        if not mkvpropedit:
                            mkvpropedit = shutil.which("mkvpropedit", path="./binaries")
                        if not mkvpropedit:
                            mkvmerge = shutil.which("mkvmerge") or shutil.which("mkvmerge", path="./binaries")
                            if mkvmerge:
                                cand = os.path.join(os.path.dirname(mkvmerge), "mkvpropedit.exe" if os.name == "nt" else "mkvpropedit")
                                if os.path.isfile(cand):
                                    mkvpropedit = cand

                        if mkvpropedit:
                            cmd = [mkvpropedit, str(muxed_location)]
                            for a in valid_attachments:
                                self.log.debug(f"Attaching {a['name']} to MKV...")
                                cmd.extend([
                                    "--attachment-name", a["name"],
                                    "--attachment-mime-type", a["mime"],
                                    "--add-attachment", a["path"]
                                ])
                            try:
                                sub_res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                                if sub_res.returncode == 0:
                                    self.log.debug(f"{len(valid_attachments)} attachment(s) successfully added to MKV.")
                                else:
                                    self.log.warning(f"mkvpropedit warning: {sub_res.stderr.strip() or sub_res.stdout.strip()}")
                            except Exception as err:
                                self.log.warning(f"Error attaching images: {err}")
                        else:
                            self.log.warning("mkvpropedit not found, could not attach images to MKV.")

                # Clean up temporary attachment files after mux
                for a in downloaded_attachments:
                    if os.path.exists(a["path"]):
                        try:
                            os.unlink(a["path"])
                        except Exception:
                            pass

                return muxed_location, returncode
            return mux_with_attachment

        # Hook Tracks.__str__ and Tracks.mux so attachment is processed during mux
        if hasattr(title, "tracks"):
            if hasattr(title.tracks, "__str__"):
                title.tracks.__str__ = wrap_str(title.tracks.__str__)
            if hasattr(title.tracks, "mux"):
                title.tracks.mux = wrap_mux(title.tracks.mux)
        if hasattr(tracks, "__str__"):
            tracks.__str__ = wrap_str(tracks.__str__)
        if hasattr(tracks, "mux"):
            tracks.mux = wrap_mux(tracks.mux)