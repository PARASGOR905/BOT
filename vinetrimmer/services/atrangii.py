from __future__ import annotations

import re
import urllib.parse
import click
import m3u8

from vinetrimmer.objects import Title, Tracks, TextTrack, AudioTrack, Track
from vinetrimmer.services.BaseService import BaseService


class Atrangii(BaseService):
    """
    Service code for Atrangii (https://atrangii.in/)
    """

    ALIASES = ["ATR", "atrangii", "Atrangii"]

    TITLE_RE = [
        r"https?://(?:www\.)?atrangii\.in/media/(?P<id>[a-zA-Z0-9_-]+)",
        r"https?://(?:www\.)?atrangii\.in/home/(?P<id>[a-zA-Z0-9_-]+)",
        r"^atrangii:(?P<id>[a-zA-Z0-9_-]+)$",
    ]

    @staticmethod
    @click.command(name="Atrangii", short_help="https://atrangii.in/", help=__doc__)
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return Atrangii(ctx, **kwargs)

    def __init__(self, ctx, title):
        self.url = title
        super().__init__(ctx)
        self.parse_title(ctx, self.url)

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Origin": "https://atrangii.in",
            "Referer": "https://atrangii.in/",
        })

    def authenticate(self):
        # 1. Try loading token from token.txt first
        import os
        from vinetrimmer.config import directories
        token_path = os.path.join(directories.cookies, "atrangii", "token.txt")
        if os.path.isfile(token_path):
            try:
                with open(token_path, "r", encoding="utf-8") as f:
                    token = f.read().strip()
                if token:
                    self.session.headers["Authorization"] = f"Bearer {token}"
                    self.log.info("Successfully loaded access token from token.txt.")
                    return
            except Exception as e:
                self.log.warning(f"Failed to read token.txt: {e}")

        if not self.credentials:
            self.log.info("No credentials or token.txt provided, using anonymous access.")
            return

        token_url = self.config["endpoints"]["token"]
        payload = {
            "grant_type": "password",
            "username": self.credentials.username,
            "password": self.credentials.password,
            "client_id": "consumerWeb",
            "client_secret": "consumerWeb@123",
            "scope": "read write"
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }
        self.log.info(f"Logging in with username '{self.credentials.username}'...")
        r = self.session.post(token_url, data=payload, headers=headers)
        if r.status_code == 401:
            raise ValueError("Atrangii login failed: Invalid username or password.")
        r.raise_for_status()
        
        token_data = r.json()
        access_token = token_data.get("access_token")
        if access_token:
            self.session.headers["Authorization"] = f"Bearer {access_token}"
            self.log.info("Successfully authenticated on Atrangii.")

    def get_titles(self):
        # 1. Login if credentials are set
        self.authenticate()

        # 2. Get media details from API
        detail_url = self.config["endpoints"]["detail"]
        params = {
            "titleYearSlug": self.title,
            "familySafe": "no"
        }
        
        self.log.info(f"Fetching media metadata for slug '{self.title}'...")
        r = self.session.get(detail_url, params=params)
        
        if r.status_code == 400:
            raise ValueError(f"Media details for slug '{self.title}' not found on Atrangii. Please make sure the slug/URL is correct.")
            
        r.raise_for_status()
        data = r.json()

        titles = []
        media_type = data.get("mediaType")
        title_name = data["mainContent"]["contentMetaData"]["title"]

        if media_type == "MULTI":  # TV Show/Web Series
            seasons = data.get("seasons_", [])
            if not seasons:
                self.log.warning("Show has no seasons.")
                return []
                
            for season in seasons:
                season_num = int(season.get("seasonNumber") or 0)
                episodes = season.get("episodes_", [])
                for ep in episodes:
                    ep_content = ep.get("mainContent")
                    if not ep_content:
                        continue
                    
                    ep_num = int(ep.get("episodeNumber") or 1)
                    ep_title = ep_content["contentMetaData"]["title"]
                    
                    service_data = {
                        "content_id": ep_content["id"],
                        "content_source": ep_content.get("contentSource", {})
                    }
                    
                    titles.append(
                        Title(
                            id_=ep_content["id"],
                            type_=Title.Types.TV,
                            name=title_name,
                            season=season_num if season_num > 0 else 1,
                            episode=ep_num,
                            episode_name=ep_title,
                            original_lang=data.get("coreLanguage", "hi").lower(),
                            source=self.ALIASES[0],
                            service_data=service_data
                        )
                    )
        else:  # SINGLE / Movie
            movie_content = data["mainContent"]
            release_date = movie_content["contentMetaData"].get("releaseDate")
            year = int(release_date[:4]) if release_date else None
            
            service_data = {
                "content_id": movie_content["id"],
                "content_source": movie_content.get("contentSource", {})
            }
            
            titles.append(
                Title(
                    id_=movie_content["id"],
                    type_=Title.Types.MOVIE,
                    name=title_name,
                    year=year,
                    original_lang=data.get("coreLanguage", "hi").lower(),
                    source=self.ALIASES[0],
                    service_data=service_data
                )
            )

        return titles

    def get_tracks(self, title):
        content_id = title.service_data["content_id"]
        content_source = title.service_data["content_source"]
        
        # 1. Fetch CloudFront signed cookies
        is_allowed_url = self.config["endpoints"]["is_allowed"]
        payload = {
            "contentId": content_id,
            "platform": "WEB_CS"
        }
        
        self.log.info("Requesting playback permission and CloudFront cookies...")
        r = self.session.post(is_allowed_url, json=payload)
        r.raise_for_status()
        is_allowed_data = r.json()
        
        if is_allowed_data.get("status") == "failed" or not is_allowed_data.get("signedCookies"):
            error_code = is_allowed_data.get("errorCode") or "UNKNOWN_ERROR"
            if error_code == "NOT_LOGGEDIN":
                raise ValueError(
                    "Access Denied: You are not logged in. Please configure your credentials or token.txt."
                )
            elif error_code == "SUBSCRIPTION_MISSING":
                raise ValueError(
                    f"Access Denied: Your account does not have an active subscription permitted to watch this content."
                )
            else:
                raise ValueError(
                    f"Access Denied: Playback permission check failed with error code: {error_code}"
                )
        
        # Extract signed cookies if they exist in response
        self.log.info("Applying CloudFront signed cookies to session...")
        self.session.cookies.set("CloudFront-Policy", is_allowed_data["cookiePolicy"])
        self.session.cookies.set("CloudFront-Signature", is_allowed_data["cookieSignature"])
        self.session.cookies.set("CloudFront-Key-Pair-Id", is_allowed_data["cookieKeyPairId"])

        # 2. Get stream URL
        # If we logged in, content_source in title.service_data might have been from the unauthenticated call.
        # Let's re-fetch it if authenticated to get the fresh details.
        if self.session.headers.get("Authorization"):
            self.log.info("Re-fetching media detail to obtain authenticated stream URL...")
            detail_url = self.config["endpoints"]["detail"]
            params = {
                "titleYearSlug": self.title,
                "familySafe": "no"
            }
            r_detail = self.session.get(detail_url, params=params)
            r_detail.raise_for_status()
            fresh_data = r_detail.json()
            
            fresh_content_source = None
            if fresh_data.get("mediaType") == "MULTI":
                for season in fresh_data.get("seasons_", []):
                    for ep in season.get("episodes_", []):
                        if ep.get("mainContent") and ep["mainContent"]["id"] == content_id:
                            fresh_content_source = ep["mainContent"].get("contentSource")
                            break
            else:
                if fresh_data.get("mainContent") and fresh_data["mainContent"]["id"] == content_id:
                    fresh_content_source = fresh_data["mainContent"].get("contentSource")
            
            if fresh_content_source:
                content_source = fresh_content_source

        # Parse streams from content_source
        video_info_list = content_source.get("videoSourceInfo", [])
        if not video_info_list:
            raise ValueError(f"No video streams found for title: {title.name}")
            
        video_info = video_info_list[0]
        content_url = video_info.get("contentUrl")
        server_url = video_info.get("preferedMediaServer")
        
        if not content_url:
            raise ValueError(
                f"Stream URL is not available. This content likely requires a subscription. "
                f"Please ensure you have configured Atrangii premium credentials in vinetrimmer.yml."
            )
            
        if server_url and not content_url.startswith("http"):
            manifest_url = urllib.parse.urljoin(server_url, content_url)
        else:
            manifest_url = content_url
            
        self.log.info(f"Loading HLS playlist from: {manifest_url}")
        playlist_res = self.session.get(manifest_url)
        playlist_res.raise_for_status()
        
        master = m3u8.loads(playlist_res.text, uri=manifest_url)
        tracks = Tracks.from_m3u8(master, source=self.ALIASES[0])
        
        # Muxed HLS fallback
        if not tracks.audios:
            lang_code = title.original_lang or "hin"
            self.log.info(
                f"No separate audio rendition found — stream is muxed (lang={lang_code})"
            )
            import uuid
            from langcodes import Language
            muxed_audio = AudioTrack(
                id_=uuid.uuid4().hex[:6],
                source=self.ALIASES[0],
                url=manifest_url,
                codec="aac",
                language=Language.get(lang_code),
                bitrate=0,
                channels="2.0",
                descriptor=Track.Descriptor.URL,
                encrypted=False,
            )
            muxed_audio.is_muxed_with_video = True
            tracks.audios.append(muxed_audio)

        # Mark tracks as unencrypted (clear/standard AES-128 HLS)
        for track in tracks.videos + tracks.audios + tracks.subtitles:
            track.encrypted = False
            
        # 3. Add external subtitles from content_source
        subtitle_list = content_source.get("subtitles", [])
        for sub in subtitle_list:
            sub_file = sub.get("fileId")
            sub_lang = sub.get("languageCode") or "eng"
            if sub_file:
                sub_url = f"https://media-files.atrangii.in{sub_file}"
                from hashlib import md5
                from langcodes import Language
                sub_track = TextTrack(
                    id_=md5(sub_url.encode()).hexdigest()[0:6],
                    source=self.ALIASES[0],
                    url=sub_url,
                    codec="vtt",
                    language=Language.get(sub_lang),
                )
                sub_track.encrypted = False
                tracks.add(sub_track)
                
        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, challenge, **_):
        return None
