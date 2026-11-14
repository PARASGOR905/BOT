from __future__ import annotations

import base64
import json
import re
import click
import m3u8

from vinetrimmer.objects import Title, Tracks, Track
from vinetrimmer.services.BaseService import BaseService


class AAONXT(BaseService):
    """
    Service code for AAONXT (https://aaonxt.com/)
    """

    ALIASES = ["AAONXT", "aaonxt"]

    TITLE_RE = [
        r"https?://(?:www\.)?aaonxt\.com/(?P<type>movies|series)/.*-(?P<id>[a-f0-9]{24})",
        r"^aaonxt:(?P<type>movies|series):(?P<id>[a-f0-9]{24})$",
        r"^(?P<id>[a-f0-9]{24})$",
        r"^(?P<id>[a-f0-9]{24}_\d+_\d+)$",
    ]

    @staticmethod
    @click.command(name="AAONXT", short_help="https://aaonxt.com/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return AAONXT(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.url = title
        super().__init__(ctx)
        
        # Determine content type / kind
        if "/series/" in self.url:
            self.kind = "series"
        elif "/movies/" in self.url:
            self.kind = "movie"
        elif "_" in title and len(title.split("_")) == 3:
            self.kind = "episode"
        else:
            self.kind = "movie"

        self.parse_title(ctx, self.url)

        # Default fallback credentials from user request
        self.auth_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6IjY1NDlmODJlNWQ2NGMzNmM1MDIyMzRiYSJ9.r_LjoRtRJIb_xXnpy9lye2sIJHWS7-UAL8cPLZFoN4g"
        self.profile_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6IjY1NDlmODJlNWQ2NGMzNmM1MDIyMzRiYSIsInByb2ZpbGVfaWQiOiJjN2YyMWYzNS0wOWJkLTQ5YzgtYWQxOS02YTE2ZTVhYzBhNzYiLCJpc0NoaWxkIjpmYWxzZX0.8oJhCv0Equ-4ELemPH7LWLBagrDQJawW_Cxw2ZTeUIA"

        if self.credentials:
            self.auth_token = self.credentials.username
            self.profile_token = self.credentials.password

        # Update headers with auth and profile tokens
        self.session.headers.update({
            "accept": "application/json",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json",
            "origin": "https://aaonxt.com",
            "referer": "https://aaonxt.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "authorization": f"Bearer {self.auth_token}" if not self.auth_token.lower().startswith("bearer ") else self.auth_token,
            "x-profile-token": self.profile_token
        })

    def _get_current_build_id(self):
        try:
            from curl_cffi import requests as c_req
            self.log.debug("Fetching homepage to extract Next.js build ID")
            response = c_req.get(
                "https://aaonxt.com/",
                headers=self.session.headers,
                timeout=10,
                impersonate="chrome"
            )
            if response.status_code == 200:
                match = re.search(r'"buildId"\s*:\s*"([^"]+)"', response.text)
                if match:
                    build_id = match.group(1)
                    self.log.debug(f"Found build ID: {build_id}")
                    return build_id
        except Exception as e:
            self.log.debug(f"Build ID fetch error: {e}")
        return None

    def _get_metadata(self, content_id, content_type="movie"):
        try:
            from curl_cffi import requests as c_req
            
            # If the title input is a full URL, we can fetch it directly
            if self.url.startswith("http"):
                self.log.debug(f"Fetching metadata from URL directly: {self.url}")
                resp = c_req.get(
                    self.url,
                    headers=self.session.headers,
                    impersonate="chrome",
                    timeout=15
                )
                if resp.status_code == 200:
                    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', resp.text, re.S)
                    if match:
                        data = json.loads(match.group(1))
                        props = data.get('props', {}).get('pageProps', {})
                        item = props.get('MovieData') or props.get('SeriesData') or props.get('movie') or props.get('series') or props.get('data', {})
                        if item:
                            return item

            # Otherwise, or as fallback, fetch Next.js build ID and use Next.js JSON endpoint
            build_id = self._get_current_build_id()
            if not build_id:
                return None

            actual_id = content_id
            if (content_type == "episode" or content_type == "series") and "_" in content_id:
                actual_id = content_id.split('_')[0]

            t = "movies" if content_type == "movie" else "series"
            url = f"https://aaonxt.com/_next/data/{build_id}/{t}/{actual_id}.json"
            self.log.debug(f"Fetching Next.js page data from: {url}")
            
            resp = c_req.get(
                url,
                headers=self.session.headers,
                impersonate="chrome",
                timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                props = data.get('pageProps', {})
                item = props.get('MovieData') or props.get('SeriesData') or props.get('movie') or props.get('series') or props.get('data', {})
                return item
        except Exception as e:
            self.log.debug(f"Metadata fetch error: {e}")
        return None

    def get_titles(self):
        if self.kind == "movie":
            metadata = self._get_metadata(self.title, "movie")
            name = self.title
            year = None
            if metadata:
                name = metadata.get("name") or metadata.get("title") or name
                pub_date = metadata.get("videoUploadDateTime") or metadata.get("videoUploadDateTimeFp")
                if pub_date:
                    try:
                        year = int(pub_date[:4])
                    except (ValueError, TypeError):
                        pass
            return Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=name,
                year=year,
                source=self.ALIASES[0],
                service_data={"content_id": self.title, "content_type": "movie"}
            )
        elif self.kind == "series":
            metadata = self._get_metadata(self.title, "series")
            if not metadata:
                raise ValueError(f"Could not retrieve series metadata for {self.title}")
                
            show_name = metadata.get("name") or metadata.get("title") or "Unknown Series"
            episodes_list = []
            seasons = metadata.get('seasons', [])
            
            def add_episode(ep, s_num, ep_num):
                ep_name = ep.get('name') or ep.get('title') or f"Episode {ep_num}"
                ep_id = f"{self.title}_{s_num}_{ep_num}"
                episodes_list.append(Title(
                    id_=ep_id,
                    type_=Title.Types.TV,
                    name=show_name,
                    season=int(s_num),
                    episode=int(ep_num),
                    episode_name=ep_name,
                    source=self.ALIASES[0],
                    service_data={"content_id": ep_id, "content_type": "episode"}
                ))

            if not seasons:
                eps = metadata.get('episodes', [])
                for ep in eps:
                    num = ep.get('number') or ep.get('episode') or 1
                    season = ep.get('season') or 1
                    add_episode(ep, season, num)
            else:
                for season in seasons:
                    s_num = season.get('season') or season.get('number') or 1
                    eps = season.get('episodes', [])
                    for ep in eps:
                        num = ep.get('number') or ep.get('episode') or 1
                        s_num_ep = ep.get('season') or s_num
                        add_episode(ep, s_num_ep, num)
                        
            return episodes_list
        elif self.kind == "episode":
            parts = self.title.split('_')
            series_id = parts[0]
            s_num = int(parts[1])
            ep_num = int(parts[2])
            
            metadata = self._get_metadata(series_id, "series")
            show_name = "Unknown Series"
            ep_name = f"Episode {ep_num}"
            
            if metadata:
                show_name = metadata.get("name") or metadata.get("title") or "Unknown Series"
                seasons = metadata.get('seasons', [])
                found_ep = None
                if not seasons:
                    eps = metadata.get('episodes', [])
                    for ep in eps:
                        if int(ep.get('season', 1)) == s_num and int(ep.get('number', 1)) == ep_num:
                            found_ep = ep
                            break
                else:
                    for season in seasons:
                        if int(season.get('season', 1)) == s_num or int(season.get('number', 1)) == s_num:
                            eps = season.get('episodes', [])
                            for ep in eps:
                                if int(ep.get('number', 1)) == ep_num:
                                    found_ep = ep
                                    break
                            if found_ep:
                                break
                if found_ep:
                    ep_name = found_ep.get('name') or found_ep.get('title') or ep_name
                    
            return Title(
                id_=self.title,
                type_=Title.Types.TV,
                name=show_name,
                season=s_num,
                episode=ep_num,
                episode_name=ep_name,
                source=self.ALIASES[0],
                service_data={"content_id": self.title, "content_type": "episode"}
            )

    def _extract_pssh_from_m3u8(self, m3u8_url):
        self.log.debug(f"Fetching Master M3U8 to find PSSH: {m3u8_url}")
        resp = self.session.get(m3u8_url)
        resp.raise_for_status()
        
        import urllib.parse
        lines = resp.text.split('\n')
        stream_url = None
        for line in lines:
            if not line.startswith('#') and line.strip() and '.m3u8' in line:
                stream_url = urllib.parse.urljoin(m3u8_url, line.strip())
                break
                
        if not stream_url:
            self.log.warning("Could not find sub-playlist in Master M3U8.")
            return None
            
        self.log.debug(f"Fetching Sub M3U8: {stream_url}")
        sub_resp = self.session.get(stream_url)
        sub_resp.raise_for_status()
        
        for line in sub_resp.text.split('\n'):
            if line.startswith('#EXT-X-KEY:METHOD=SAMPLE-AES'):
                match = re.search(r'URI="data:text/plain;base64,([^"]+)"', line)
                if match:
                    return match.group(1)
                    
        self.log.warning("No Widevine PSSH found in Sub M3U8.")
        return None

    def get_tracks(self, title):
        content_id = title.service_data["content_id"]
        content_type = title.service_data["content_type"]
        
        # 1. Fetch playback URL from FastPix API
        api_url = self.config["endpoints"]["playback"]
        payload = {
            "content_id": content_id,
            "content_type": content_type
        }
 
        if content_type == "episode" and "_" in content_id:
            parts = content_id.split('_')
            if len(parts) == 3:
                payload["series_id"] = parts[0]
                payload["season"] = int(parts[1])
                payload["episode"] = int(parts[2])

        self.log.debug(f"Fetching playback URL from FastPix API for {content_id}...")
        resp = self.session.post(api_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise ValueError(f"FastPix API returned failure: {data}")
            
        playback_url = data.get("playback_url")
        if not playback_url:
            raise ValueError(f"No playback URL returned by FastPix API for {content_id}")
            
        self.log.debug(f"Received playback URL: {playback_url}")

        # 2. Extract playback_id and token from playback_url
        import urllib.parse
        parsed_url = urllib.parse.urlparse(playback_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        token = query_params.get("token", [None])[0]
        
        path_parts = parsed_url.path.strip("/").split("/")
        last_part = path_parts[-1]
        playback_id = last_part.replace(".m3u8", "").replace(".mpd", "")

        # 3. Construct and store license URL
        license_url = f"https://api.fastpix.io/v1/on-demand/drm/license/widevine/{playback_id}?token={token}"
        title.service_data["license_url"] = license_url

        # 4. Extract Widevine PSSH
        pssh_b64 = self._extract_pssh_from_m3u8(playback_url)
        if not pssh_b64:
            raise ValueError(f"Failed to extract Widevine PSSH from manifest: {playback_url}")
            
        # Decode and parse PSSH box
        pssh_bytes = base64.b64decode(pssh_b64)
        from vinetrimmer.vendor.pymp4.parser import Box
        from vinetrimmer.utils import Cdm
        
        try:
            pssh = Box.parse(pssh_bytes)
        except Exception:
            pssh = Box.parse(Box.build(dict(
                type=b"pssh",
                version=0, 
                flags=0,
                system_ID=Cdm.uuid,
                init_data=pssh_bytes
            )))

        # 5. Load and populate tracks
        master_text = self.session.get(playback_url).text
        master = m3u8.loads(master_text, uri=playback_url)
        tracks = Tracks.from_m3u8(master, source=self.ALIASES[0])

        from langcodes import Language

        for track in tracks.videos:
            track.encrypted = True
            track.pssh = pssh

        for track in tracks.audios:
            track.encrypted = True
            track.pssh = pssh
            # Override language to Odia since the manifest defaults to eng
            track.language = Language.get("or")
            if not track.bitrate:
                track.bitrate = 128000  # Default fallback (128 kbps)
            
        for track in tracks.subtitles:
            track.encrypted = False

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge, title, track, session_id, **_):
        lic_url = title.service_data.get("license_url")
        if not lic_url:
            raise ValueError("License URL is missing from title service_data")
            
        self.log.info(f"Requesting Widevine license from: {lic_url}")
        
        headers = {
            "Content-Type": "application/octet-stream",
            "Accept": "*/*",
            "Origin": "https://aaonxt.com",
            "Referer": "https://aaonxt.com/",
            "User-Agent": self.session.headers.get("User-Agent")
        }
        
        r = self.session.post(lic_url, data=challenge, headers=headers)
        r.raise_for_status()
        return r.content
