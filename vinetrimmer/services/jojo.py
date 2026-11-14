from __future__ import annotations

import json
import time
import click
import m3u8

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class Jojo(BaseService):
    """
    Service code for JOJO App (https://jojoapp.in).

    Authorization: Cookies (sessionid) or Guest session fallback.
    Security: AES-128 HLS.
    """
    ALIASES = ["JOJO", "jojo"]
    GEOFENCE = []
    TITLE_RE = [
        r"^https?://(?:www\.)?jojoapp\.in/(?P<content_type>shows?|movies?|watches?|assets?)/(?:(?P<slug>[^/]+)/)?(?:[^/]+-)?(?P<id>\d+)(?:[/?#].*)?$",
        r"^https?://(?:www\.)?jojoapp\.in/(?P<content_type>shows?|movies?|watches?|assets?)/(?P<slug>[^/?#]+)(?:[/?#].*)?$",
        r"^(?P<content_type>shows?|movies?|watches?|assets?):(?P<id>\d+)$",
        r"^(?P<id>https?://(?:cdn\.)?thesupercms\.com/.*\.m3u8(?:\?.*)?)$",
        r"^(?P<id>https?://.*\.m3u8(?:\?.*)?)$",
        r"^(?P<id>\d+)$",
    ]

    @staticmethod
    @click.command(name="Jojo", short_help="https://jojoapp.in")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return Jojo(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        self.raw_title_input = title
        self.title = title
        super().__init__(ctx)
        assert ctx.parent is not None

        parsed = self.parse_title(ctx, title) or {}
        self.parsed = parsed
        self.m3u8_url = None
        self.slug = parsed.get("slug")
        self.content_type = parsed.get("content_type") or "movies"
        self.wanted = ctx.parent.params.get("wanted")

        if ".m3u8" in self.title.lower():
            self.m3u8_url = self.title
            self.content_id = 0
        elif parsed.get("id"):
            self.content_id = int(parsed["id"])
            self.title = str(self.content_id)
        elif self.title.isdigit():
            self.content_id = int(self.title)
        else:
            import re
            m = re.search(r"(\d+)(?:[/?#].*)?$", self.title)
            if m:
                self.title = m.group(1)
                self.content_id = int(self.title)
            else:
                self.content_id = 0

        self._api_root = self.config["endpoints"]["api"]
        self._site_root = self.config["endpoints"]["site"]

        self.aes_key = "sBYDzGabIR2aEPagELKBN41kIR7xBm1G5emAODCCLl0="
        self.aes_iv = "2d8f2f3bfb6a2e6d129f3eaf4ef104d0"

        self._setup_auth()
        self._connect_socket()

    def _setup_auth(self):
        # Look for sessionid in cookies, then credentials
        session_id = None
        if self.cookies:
            cookies_dict = {}
            # Handle both dictionary and CookieJar objects safely
            if hasattr(self.cookies, "items"):
                cookies_dict = dict(self.cookies.items())
            else:
                for cookie in self.cookies:
                    cookies_dict[cookie.name] = cookie.value
            session_id = cookies_dict.get("sessionid") or cookies_dict.get("session_id")

        if not session_id and self.credentials:
            if hasattr(self.credentials, "get"):
                session_id = self.credentials.get("token") or self.credentials.get("session_id")
            else:
                if self.credentials.username in ("token", "session_id", "sessionid"):
                    session_id = self.credentials.password
                else:
                    session_id = self.credentials.username or self.credentials.password


        if not session_id:
            self.log.info("No session ID found, performing Guest Login...")
            session_id = self._guest_login()

        self.session_id = session_id
        
        # Setup headers
        self.session.headers.update(self.config.get("headers") or {})
        self.session.headers.update({
            "sessionid": session_id,
            "Origin": self._site_root,
            "Referer": f"{self._site_root}/",
        })

    def _guest_login(self) -> str:
        url_guest = f"{self._api_root}/v2/auth/guest"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "deviceTypeCode": self.config["headers"]["deviceTypeCode"],
            "deviceID": self.config["headers"]["deviceID"],
            "language": self.config["headers"]["language"],
            "appversion": self.config["headers"]["appversion"],
            "project": self.config["headers"]["project"],
        }
        
        raw_payload = {"data": "data"}
        encrypted_payload = self._encrypt_aes(json.dumps(raw_payload))
        
        resp = self.session.post(url_guest, json={"data": encrypted_payload}, headers=headers)
        resp_json = resp.json()
        
        encrypted_guest = resp_json.get("data")
        if not encrypted_guest:
            self.log.exit(f" - Guest login failed: {resp_json}")
            
        decrypted_guest = self._decrypt_aes(encrypted_guest)
        guest_data = json.loads(decrypted_guest)
        guest_session_id = guest_data.get("data", {}).get("session_id")
        if not guest_session_id:
            self.log.exit(f" - Guest session_id not found in decrypted response: {guest_data}")
            
        return guest_session_id

    def _connect_socket(self):
        try:
            import socketio
        except ImportError:
            self.log.warning("socketio module is not installed. Playback requests might fail.")
            return

        self.sio = socketio.Client()
        socket_connected = [False]

        @self.sio.event
        def connect():
            self.log.debug("Socket.IO connected successfully!")
            socket_connected[0] = True

        @self.sio.event
        def disconnect():
            self.log.debug("Socket.IO disconnected.")

        auth_data = {
            "devicetypecode": self.config["headers"]["deviceTypeCode"],
            "deviceID": self.config["headers"]["deviceID"],
            "sessionid": self.session_id,
            "language": int(self.config["headers"]["language"]),
        }

        self.log.info("Connecting to JOJO Socket.IO server...")
        try:
            self.sio.connect(
                self.config["endpoints"]["socket"],
                headers={"User-Agent": self.config["headers"]["User-Agent"]},
                auth=auth_data,
                transports=["websocket"],
            )
            # Wait a moment to ensure connection handshake completes
            time.sleep(1.5)
        except Exception as e:
            self.log.warning(f"Failed to connect Socket.IO: {e}. Playback calls might fail.")

    def _disconnect_socket(self):
        if hasattr(self, "sio") and self.sio and self.sio.connected:
            self.sio.disconnect()

    def __del__(self):
        self._disconnect_socket()

    def _encrypt_aes(self, plaintext_str: str) -> str:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        import base64
        
        key = base64.b64decode(self.aes_key)
        iv = bytes.fromhex(self.aes_iv)
        plaintext = plaintext_str.encode("utf-8")
        cipher = AES.new(key, AES.MODE_CBC, iv)
        padded = pad(plaintext, AES.block_size)
        ciphertext = cipher.encrypt(padded)
        return ciphertext.hex()

    def _decrypt_aes(self, ciphertext_hex: str) -> str:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
        import base64
        
        key = base64.b64decode(self.aes_key)
        iv = bytes.fromhex(self.aes_iv)
        ciphertext = bytes.fromhex(ciphertext_hex)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(ciphertext)
        return unpad(decrypted, AES.block_size).decode("utf-8")

    def _api_post(self, path: str, payload: dict) -> dict:
        url = f"{self._api_root}{path}"
        encrypted_payload = self._encrypt_aes(json.dumps(payload))
        resp = self.session.post(url, json={"data": encrypted_payload})
        resp_json = resp.json()
        
        encrypted_data = resp_json.get("data")
        if not encrypted_data:
            raise Exception(f"API {path} returned no data block: {resp_json}")
            
        decrypted_str = self._decrypt_aes(encrypted_data)
        return json.loads(decrypted_str)

    def _api_get(self, path: str, params: dict = None) -> dict:
        url = f"{self._api_root}{path}"
        resp = self.session.get(url, params=params or {})
        resp_json = resp.json()
        
        encrypted_data = resp_json.get("data")
        if not encrypted_data:
            raise Exception(f"API {path} returned no data block: {resp_json}")
            
        decrypted_str = self._decrypt_aes(encrypted_data)
        return json.loads(decrypted_str)

    def _get_asset_details(self, asset_id: int) -> dict:
        # Asset detail uses POST /asset/{asset_id} with empty JSON payload
        res = self._api_post(f"/asset/{asset_id}", {})
        return res.get("data") or {}

    def _get_season_episodes(self, season_id: int) -> list:
        # Season episodes uses GET /{season_id}/episodes
        res = self._api_get(f"/{season_id}/episodes", params={"page": 1})
        return res.get("data", {}).get("episodes") or []

    @staticmethod
    def _parse_year(date_str: str | None) -> int | None:
        if not date_str:
            return None
        date_str = str(date_str).strip()
        if not date_str:
            return None
        try:
            from datetime import datetime, timezone, timedelta
            iso_str = date_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is not None:
                ist = timezone(timedelta(hours=5, minutes=30))
                dt = dt.astimezone(ist)
            return dt.year
        except Exception:
            pass
        import re
        m = re.search(r"(\d{4})", date_str)
        if m:
            return int(m.group(1))
        return None

    def get_titles(self) -> list[Title] | Title:
        if self.m3u8_url:
            import re
            m = re.search(r"/(?:movies|shows|output)/([^/]+)/", self.m3u8_url)
            if not m:
                m = re.search(r"/([^/]+?)(?:\.m3u8|/master\.m3u8)", self.m3u8_url)
            name = (m.group(1) if m else "JOJO Video").replace("-", " ").replace("_", " ").title()
            return Title(
                id_="1",
                type_=Title.Types.MOVIE,
                name=name,
                year=None,
                original_lang="gu",
                source=self.ALIASES[0],
                service_data={"playback_url": self.m3u8_url},
            )

        asset = {}
        if self.content_id:
            try:
                asset = self._get_asset_details(self.content_id)
            except Exception as e:
                self.log.debug(f"Failed to get asset details: {e}")

        if not asset:
            name = self.slug.replace("-", " ").replace("_", " ").title() if self.slug else f"JOJO-{self.content_id}"
            return Title(
                id_=str(self.content_id),
                type_=Title.Types.MOVIE,
                name=name,
                year=None,
                original_lang="gu",
                source=self.ALIASES[0],
                service_data={"slug": self.slug},
            )

        asset_type = asset.get("asset_type")

        if asset_type == 3:  # Show
            titles = []
            for season in asset.get("seasons") or []:
                season_id = season.get("season_id") or season.get("asset_id")
                season_num = int(season.get("season_number") or 1)
                episodes = self._get_season_episodes(season_id)
                
                for ep in episodes:
                    ep_num = int(ep.get("episode_number") or 0)
                    if self.wanted and f"{season_num}x{ep_num}" not in self.wanted:
                        continue
                    year = self._parse_year(ep.get("asset_release_date") or asset.get("asset_release_date"))
                    titles.append(Title(
                        id_=str(ep.get("asset_id")),
                        type_=Title.Types.TV,
                        name=asset.get("asset_title"),
                        year=year,
                        season=season_num,
                        episode=ep_num,
                        episode_name=ep.get("asset_title"),
                        original_lang="gu",
                        source=self.ALIASES[0],
                        service_data=ep,
                    ))
            if not titles:
                self.log.exit(" - No JOJO episodes matched.")
            return titles

        elif asset_type == 1:  # Movie
            year = self._parse_year(asset.get("asset_release_date"))
            return Title(
                id_=str(self.content_id),
                type_=Title.Types.MOVIE,
                name=asset.get("asset_title"),
                year=year,
                original_lang="gu",
                source=self.ALIASES[0],
                service_data=asset,
            )

        elif asset_type == 5:  # Episode directly
            parent_id = asset.get("parent_id")
            show_name = "JOJO Show"
            season_num = 1
            if parent_id:
                parent_season = self._get_asset_details(parent_id)
                show_name = parent_season.get("asset_title") or show_name
                season_num = int(parent_season.get("season_number") or 1)

            year = self._parse_year(asset.get("asset_release_date"))
            return Title(
                id_=str(self.content_id),
                type_=Title.Types.TV,
                name=show_name,
                year=year,
                season=season_num,
                episode=int(asset.get("episode_number") or 1),
                episode_name=asset.get("asset_title"),
                original_lang="gu",
                source=self.ALIASES[0],
                service_data=asset,
            )

        else:  # Fallback for other content categories
            year = self._parse_year(asset.get("asset_release_date"))
            return Title(
                id_=str(self.content_id),
                type_=Title.Types.MOVIE,
                name=asset.get("asset_title") or f"JOJO-{self.content_id}",
                year=year,
                original_lang="gu",
                source=self.ALIASES[0],
                service_data=asset,
            )

    def get_tracks(self, title: Title) -> Tracks:
        m3u8_url = None
        if getattr(self, "m3u8_url", None):
            m3u8_url = self.m3u8_url
        elif title.service_data and title.service_data.get("playback_url"):
            m3u8_url = title.service_data["playback_url"]
        elif title.id and title.id.isdigit() and int(title.id) > 0:
            try:
                playback_data = self._api_post("/playback", {"asset_id": int(title.id), "is_subscribe": False})
                pb_info = playback_data.get("data") or {}
                m3u8_url = pb_info.get("playback_url")
            except Exception as e:
                self.log.debug(f"Playback API request failed: {e}")

        if not m3u8_url:
            slug = (title.service_data or {}).get("slug") or getattr(self, "slug", None)
            if not slug and title.name:
                slug = title.name.lower().replace(" ", "-")
            if slug:
                content_type = getattr(self, "content_type", "movies") or "movies"
                fallback_url = f"https://cdn.thesupercms.com/V2/{content_type}/{slug}/output/master.m3u8"
                self.log.info(f"Checking direct CDN stream fallback: {fallback_url}")
                try:
                    resp_check = self.session.head(fallback_url, timeout=10)
                    if resp_check.status_code == 200:
                        m3u8_url = fallback_url
                except Exception as e:
                    self.log.debug(f"CDN head check error: {e}")

        if not m3u8_url:
            self.log.exit(" - JOJO playback is not available: Asset Status Inactive (status 403)")

        self.log.debug(" + HLS URL: %s", m3u8_url)
        
        # Load the HLS manifest
        resp = self.session.get(m3u8_url, timeout=120)
        resp.raise_for_status()
        master = m3u8.loads(resp.text, uri=resp.url)

        # Standardize language mappings in HLS media playlists
        for media in master.media:
            if media.type == "AUDIO" and (media.language == "guj" or media.name == "guj"):
                media.language = "gu"

        tracks = Tracks.from_m3u8(master, source=self.ALIASES[0])

        # HLS AES-128 is not considered DRM-protected in this downloader's context
        for track in tracks.videos + tracks.audios + tracks.subtitles:
            track.original_url = m3u8_url
            track.encrypted = False

        # Override audio track language and set default bitrate for JOJO
        from langcodes import Language
        for track in tracks.audios:
            track.language = Language.get("gu")
            if not track.bitrate or track.bitrate == 0:
                try:
                    playlist_resp = self.session.get(track.url, timeout=10)
                    playlist_resp.raise_for_status()
                    import m3u8 as m3u8_lib
                    audio_playlist = m3u8_lib.loads(playlist_resp.text, uri=track.url)
                    if audio_playlist.segments:
                        first_seg = audio_playlist.segments[0]
                        size = 0
                        if getattr(first_seg, "byterange", None):
                            try:
                                size = int(first_seg.byterange.split("@")[0])
                            except (ValueError, IndexError):
                                pass
                        if not size:
                            seg_url = first_seg.absolute_uri
                            seg_head = self.session.head(seg_url, timeout=10)
                            size = int(seg_head.headers.get("Content-Length", 0))
                            if not size:
                                seg_get = self.session.get(seg_url, headers={"Range": "bytes=0-0"}, timeout=10)
                                cr = seg_get.headers.get("Content-Range")
                                if cr and "/" in cr:
                                    size = int(cr.split("/")[-1])
                        if size and first_seg.duration:
                            calculated_bitrate = int((size * 8) / first_seg.duration)
                            if calculated_bitrate > 5_000_000:
                                total_duration = sum(seg.duration for seg in audio_playlist.segments)
                                if total_duration > 0:
                                    calculated_bitrate = int((size * 8) / total_duration)
                            track.bitrate = (calculated_bitrate // 1000) * 1000
                except Exception as e:
                    self.log.debug(f"Failed to dynamically calculate audio bitrate: {e}")

                if not track.bitrate or track.bitrate == 0:
                    track.bitrate = 128000

        return tracks

    def license(self, *_args, **_kwargs) -> None:
        return None
