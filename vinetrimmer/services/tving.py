from __future__ import annotations

import re
import base64
import json
import random
from http.cookiejar import CookieJar
from typing import List, Optional, Union

import click
from langcodes import Language
from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import unpad



from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils import Cdm
from vinetrimmer.utils.widevine.device import LocalDevice

from vinetrimmer.objects import Title, Tracks, VideoTrack, AudioTrack, TextTrack


class TVING(BaseService):
    """
    Service code for Tving streaming service (https://www.tving.com).

    \b
    Authorization: Cookies
    Security:
        Widevine:
            L3: UHD (DASH)

    \b
    Tips:
        - Input can be a URL or media code
            https://www.tving.com/contents/P001234567 OR P001234567 OR M000123456
        - Media codes starting with 'M' are movies, others are series/programs
        - Use -q 2160 to request 4K UHD streams (stream70 quality)

    \b
    Notes:
        - Uses cookie-based authentication (_tving_token)
        - Stream URLs are AES encrypted and decrypted client-side
        - DRM is PallyCon Widevine
    """

    TITLE_RE = r"^(?:https?://www\.tving\.com/contents/)?(?P<id>[\w]+)"
    GEOFENCE = ("KR",)
    ALIASES = ["TVING", "tving"]

    @staticmethod
    @click.command(name="TVING", short_help="https://www.tving.com")
    @click.argument("title", type=str, required=True)
    @click.pass_context
    def cli(ctx: click.Context, **kwargs) -> "TVING":
        return TVING(ctx, **kwargs)

    def __init__(self, ctx: click.Context, title: str) -> None:
        super().__init__(ctx)
        self.ctx = ctx
        self.title: str = title
        q = ctx.parent.params.get("quality")
        self.qualities: List[int] = [q] if isinstance(q, int) else list(q or [])
        self.config = {
            "endpoints": {
                "login": "https://gw.tving.com/cust/login/v1/tv/{account_type}",
                "content_info": "https://api.tving.com/v2/media/content/info",
                "stream_info": "https://api.tving.com/v3/media/stream/info",
                "episodes": "https://api.tving.com/v2/media/frequency/program/{title}"
            },
            "api_key": {
                "v2": "1e7952d0917d6aab1f0293a063697610",
                "v3": "56dc890767ec858dcb4abf184c0b2d2d"
            },
            "device": {
                "screen_code": "CSSD1100",
                "network_code": "CSND0900",
                "os_code": "CSOD0900",
                "tele_code": "CSCD0900",
                "device_id": "a7b3c2d4e5f61728394a5b6c7d8e9f01",
                "device_info": "Pixel 4 XL",
                "user_agent": "Dalvik/2.1.0 (Linux; U; Android 11; Pixel 4 XL Build/RQ3A.211001.001) tving 25.06.02"
            }
        }
        self.authenticate(self.cookies, getattr(self, "credentials", None))

    def authenticate(self, cookies, credential) -> None:

        self.app_token: Optional[str] = None
        self.tving_token: Optional[str] = None
        self.auth_token: Optional[str] = None

        if credential:
            # Login via Android App API — token goes through PallyCon Android DRM (accepts L3 Android CDMs)
            self.log.info("TVING: Logging in via Android App API with credentials...")
            tokens = self._login_android(credential.username, credential.password)
            if tokens and tokens[0]:
                self.app_token = tokens[0]
                self.tving_token = tokens[1]
                self.auth_token = tokens[2]
                self.log.info("TVING: Android App login successful.")
                self.session.cookies.clear()
                self.session.headers.update({
                    "user-agent": self.config["device"]["user_agent"],
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "net.cj.cjenm.tving",
                    "authorization": f"Bearer {self.app_token}",
                })
            else:
                self.log.warning("TVING: Android App login failed. Falling back to cookies...")

        if not self.app_token and cookies:
            # Extract both tokens from cookies
            self.tving_token = self.session.cookies.get("_tving_token")
            self.app_token = self.session.cookies.get("accessToken")
            
            if not self.tving_token:
                raise EnvironmentError("Cookie '_tving_token' not found. Provide TVING cookies or credentials.")
            
            self.log.warning("TVING: Using cookie auth. Web DRM (Atlantis) requires Chrome CDM. Use credentials for Android CDM support.")
            self.session.headers.update({
                "user-agent": self.config["device"]["user_agent"],
                "authorization": f"Bearer {self.app_token or self.tving_token}",
            })
        elif not self.app_token and not cookies:
            raise EnvironmentError("TVING requires either credentials (recommended) or cookies.")

    def _login_android(self, username: str, password: str) -> Optional[tuple[str, Optional[str], Optional[str]]]:
        """Login to TVING via Android App API and return (accessToken, tvingToken, authToken)."""
        try:
            device: dict = self.config["device"]
            
            # Use provided login endpoint or fallback to default new API
            login_endpoint = self.config["endpoints"].get(
                "login", "https://gw.tving.com/cust/login/v1/tv/{account_type}"
            )
            
            # Determine account type (cjone for email, tving for ID)
            # Allow forcing cjone by prefixing username with "cjone|"
            if username.startswith("cjone|"):
                account_type = "cjone"
                username = username.split("|", 1)[1]
            else:
                account_type = "cjone" if "@" in username else "tving"
                
            id_field = "email" if "@" in username else "userId"

            base_params = {
                "apiKey": self.config["api_key"]["v3"],
                "screenCode": device["screen_code"],
                "networkCode": device["network_code"],
                "osCode": device["os_code"],
                "teleCode": device["tele_code"],
                "deviceId": device["device_id"],
                "uuid": device["device_id"],
            }

            import requests
            res = requests.post(
                login_endpoint.format(account_type=account_type),
                params=base_params,
                json={
                    id_field: username,
                    "password": password,
                    "deviceName": "Windows",
                    "deviceId": device["device_id"],
                },
                headers={
                    "content-type": "application/json",
                    "accept-language": "ko",
                    "Accept": "application/json, text/plain, */*",
                    "X-Requested-With": "net.cj.cjenm.tving",
                },
            )
            
            res_json = res.json()
            body = res_json.get("data") or res_json.get("body") or res_json
            
            token = body.get("accessToken") or body.get("token")
            if token:
                return (token, body.get("tvingToken"), body.get("authToken"))
            else:
                self.log.warning(f"TVING login response: {res.text}")
                return None
                
        except Exception as e:
            self.log.warning(f"TVING Android login error: {e}")
            return None

    def get_titles(self) -> Titles_T:
        match = re.match(self.TITLE_RE, self.title)
        if not match:
            raise ValueError("Invalid title format. Expected format: type/id or full URL")

        title_id: str = match.group("id")
        detail: dict = self._get_detail(title_id)

        if not detail:
            self.log.warning(f"No content found for title ID: {title_id}. Using ID as title.")
            detail = {"title": title_id, "code": title_id}

        clean_title = detail.get("title", title_id).replace("[4K] ", "").replace("[4K]", "").strip()

        if detail.get("uhd_4k_yn") == "Y":
            self.log.info("This content has UHD 4K available, use -q 2160 to request it")

        if title_id.startswith("M"):
            return [Title(
                id_=detail["code"],
                type_=Title.Types.MOVIE,
                name=clean_title,
                source=self.ALIASES[0],
                service_data=detail
            )]

        episodes_data: list = self._get_episodes(title_id)
        titles = []
        for ep in episodes_data:
            ep_info: dict = ep["episode"]
            titles.append(Title(
                id_=ep_info["code"],
                type_=Title.Types.TV,
                name=clean_title,
                season=1,
                episode=ep_info["frequency"],
                episode_name=ep_info.get("display_title"),
                source=self.ALIASES[0],
                service_data=ep
            ))

        return titles

    def get_tracks(self, title: Title_T) -> Tracks:
        self.encrypted_stream = False
        stream_quality: str = "stream70" if 2160 in self.qualities else "stream50"
        random_number: str = str(random.randint(100, 99999998))

        self.log.info("Requesting stream info via TV/App API...")
        data = self._get_stream_v3(title.id, stream_quality, random_number)
        stream = (data.get("body") or {}).get("stream") if isinstance(data, dict) else None

        if not stream or (isinstance(data, dict) and data.get("code") == "403"):
            self.log.warning("TV/App API returned 403 Forbidden or no stream. Falling back to WEB API...")
            data = self._get_stream_web(title.id, stream_quality, random_number)
            stream = (data.get("body") or {}).get("stream") if isinstance(data, dict) else None

        if not stream:
            data = self._get_stream_v2(title.id, stream_quality, random_number)
            stream = (data.get("body") or {}).get("stream") if isinstance(data, dict) else None

        if not stream:
            self.log.error(f"TVING API Response: {data}")
            raise ValueError(f"Stream data unavailable for {title.id}. Content may not be streamable.")

        self.encrypted_stream = False
        if stream.get("drm_yn") == "N":
            enc_manifest = stream["playback"].get("non_drm")
        else:
            self.encrypted_stream = True
            enc_manifest = stream["playback"].get("drm", {}).get("widevine")
            drm_license_data: list = stream["playback"]["drm"]["license"]["drm_license_data"]
            wv_license: Optional[dict] = next(
                (x for x in drm_license_data if x["drm_type"] == "Widevine"),
                None,
            )
            if wv_license:
                self.license_url: str = wv_license["drm_server_url"]
                self.license_token: str = wv_license["drm_header_value"]
                self.license_header: str = wv_license.get("drm_header_key", "pallycon-customdata-v2")

            pr_license: Optional[dict] = next(
                (x for x in drm_license_data if x["drm_type"] == "PlayReady"),
                None,
            )
            if pr_license:
                self.pr_license_url: str = pr_license["drm_server_url"]
                self.pr_license_token: str = pr_license["drm_header_value"]
                self.pr_license_header: str = pr_license.get("drm_header_key", "pallycon-customdata-v2")

        manifest_url: str = self._decrypt_stream(
            enc_manifest.split("|")[1] if "|" in enc_manifest else enc_manifest,
            title.id,
            random_number,
        )

        import re
        if "device=" in manifest_url:
            manifest_url = re.sub(r"([?&]device=)[^&]*", r"\g<1>webOS", manifest_url)
        else:
            manifest_url += "&device=webOS" if "?" in manifest_url else "?device=webOS"
        if "vc=" in manifest_url:
            manifest_url = re.sub(r"([?&]vc=)[^&]*", r"\g<1>1,2,3,4", manifest_url)
        else:
            manifest_url += "&vc=1,2,3,4" if "?" in manifest_url else "?vc=1,2,3,4"


        manifest_res = self.session.get(manifest_url)
        manifest_res.raise_for_status()

        if manifest_url.endswith(".m3u8") or manifest_url.split("?")[0].endswith(".m3u8"):
            import m3u8
            tracks = Tracks.from_m3u8(m3u8.loads(manifest_res.text, uri=manifest_url), source=self.ALIASES[0])
        else:
            tracks = Tracks.from_mpd(url=manifest_url, data=manifest_res.text, session=self.session, source=self.ALIASES[0])

        audio_tracks = [t for t in tracks.audios if hasattr(t, "bitrate") and t.bitrate and t.bitrate < 100000]
        if audio_tracks:
            self.log.warning("Low quality audio detected. Retrying with WEB screenCode for better audio...")
            data2 = self._get_stream_web(title.id, stream_quality, random_number)
            stream2 = (data2.get("body") or {}).get("stream") if isinstance(data2, dict) else None
            if stream2:
                playback2 = stream2.get("playback", {})
                enc2 = playback2.get("non_drm") or playback2.get("drm", {}).get("widevine")
                if enc2:
                    url2 = self._decrypt_stream(
                        enc2.split("|")[1] if "|" in enc2 else enc2,
                        title.id,
                        random_number,
                    )
                    res2 = self.session.get(url2)
                    if res2.ok:
                        if url2.endswith(".m3u8") or url2.split("?")[0].endswith(".m3u8"):
                            import m3u8
                            tracks2 = Tracks.from_m3u8(m3u8.loads(res2.text, uri=url2), source=self.ALIASES[0])
                        else:
                            tracks2 = Tracks.from_mpd(url=url2, data=res2.text, session=self.session, source=self.ALIASES[0])
                        if tracks2.audios: tracks.audios = tracks2.audios

        if getattr(self, 'encrypted_stream', False):
            for t in tracks.videos + tracks.audios:
                t.encrypted = True
        return tracks

    def get_chapters(self, title: Title_T) -> Chapters:
        return Chapters()

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: object) -> Optional[bytes]:
        if not hasattr(self, "license_url"):
            return None

        if getattr(self, "is_web_stream", True):
            # Web DRM path
            headers = {
                getattr(self, "license_header", "pallycon-customdata-v2"): self.license_token,
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "content-type": "application/octet-stream",
            }
        else:
            # Android App DRM path
            headers = {
                getattr(self, "license_header", "pallycon-customdata-v2"): self.license_token,
                "user-agent": self.config["device"]["user_agent"],
                "content-type": "application/octet-stream",
            }

        res = self.session.post(
            self.license_url,
            data=challenge,
            headers=headers,
        )

        if not res.ok:
            self.log.error(f"DRM License Error [{res.status_code}]: {res.text}")
            res.raise_for_status()

        self.log.error(f"Pallycon Response bytes: {res.content[:200]}")
        if b"errorCode" in res.content or b"message" in res.content:
            self.log.error(f"Pallycon Error Response: {res.text}")

        return res.content

    def get_playready_license(self, challenge: bytes, **kwargs) -> bytes:
        if not hasattr(self, "pr_license_url"):
            return None

        pr_url = self.pr_license_url.replace("widevine", "playready")
        self.log.info(f"USING PLAYREADY LICENSE on {pr_url}")

        if getattr(self, "is_web_stream", True):
            # Web DRM path
            headers = {
                getattr(self, "pr_license_header", "pallycon-customdata-v2"): self.pr_license_token,
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "content-type": "text/xml; charset=utf-8",
                "SOAPAction": '"http://schemas.microsoft.com/DRM/2007/03/protocols/AcquireLicense"',
            }
        else:
            # Android App DRM path
            headers = {
                getattr(self, "pr_license_header", "pallycon-customdata-v2"): self.pr_license_token,
                "user-agent": self.config["device"]["user_agent"],
                "content-type": "text/xml; charset=utf-8",
                "SOAPAction": '"http://schemas.microsoft.com/DRM/2007/03/protocols/AcquireLicense"',
            }

        res = self.session.post(
            pr_url,
            data=challenge,
            headers=headers,
        )

        if not res.ok:
            self.log.error(f"DRM License Error [{res.status_code}]: {res.text}")
            res.raise_for_status()

        return res.content

    @staticmethod
    def _is_preroll_period(period: dict) -> bool:
        duration: str = period.get("duration", "")
        if not duration:
            return False
        try:
            s = duration.replace("PT", "")
            total = 0.0
            for part, mul in [("H", 3600), ("M", 60)]:
                if part in s:
                    val, s = s.split(part)
                    total += float(val) * mul
            if s.endswith("S"):
                s = s[:-1]
            if s:
                total += float(s)
            return total < 30
        except Exception:
            return False

    def _get_detail(self, title_id: str) -> dict:
        try:
            device: dict = self.config["device"]
            screen_code = "CSSD1300" if not getattr(self, "app_token", None) and getattr(self, "tving_token", None) else device["screen_code"]
            res = self.session.get(
                self.config["endpoints"]["content_info"],
                params={
                    "screenCode": screen_code,
                    "networkCode": device["network_code"],
                    "osCode": device["os_code"],
                    "teleCode": device["tele_code"],
                    "apiKey": self.config["api_key"]["v2"],
                    "mediaCode": title_id,
                },
            )
            data = res.json()
            content = (data.get("body") or {}).get("content", {})
            if not content:
                res = self.session.get(
                    self.config["endpoints"]["content_info"],
                    params={
                        "screenCode": "CSSD0100",
                        "networkCode": device["network_code"],
                        "osCode": device["os_code"],
                        "teleCode": device["tele_code"],
                        "apiKey": self.config["api_key"]["v2"],
                        "mediaCode": title_id,
                    },
                )
                data = res.json()
                content = (data.get("body") or {}).get("content", {})
            return content
        except Exception as e:
            self.log.warning(f"Failed to get detail: {e}")
            return {}

    def _get_episodes(self, title_id: str) -> list:
        device: dict = self.config["device"]
        screen_code = "CSSD1300" if not getattr(self, "app_token", None) and getattr(self, "tving_token", None) else device["screen_code"]
        res = self.session.get(
            self.config["endpoints"]["episodes"].format(title=title_id),
            params={
                "screenCode": screen_code,
                "networkCode": device["network_code"],
                "osCode": device["os_code"],
                "teleCode": device["tele_code"],
                "apiKey": self.config["api_key"]["v2"],
                "pocType": "APP_X_TVING_5.0.1",
                "order": "new",
                "adult": "all",
                "free": "all",
                "guest": "all",
                "scope": "all",
                "pageNo": "1",
                "pageSize": "100",
                "cacheType": "main",
                "mediaCode": title_id,
            },
        )
        self.is_web_stream = True
        return res.json()["body"]["result"]

    def _get_stream_v3(self, media_code: str, stream_quality: str, random_number: str) -> Optional[dict]:
        try:
            device: dict = self.config["device"]
            auth_token = self.app_token or self.tving_token
            
            # If using app token, we must use the Android device params. 
            # If using web cookie, webOS/CSSD1200 spoofing might still be needed by TVING
            is_app = bool(self.app_token)
            
            res = self.session.post(
                self.config["endpoints"].get("stream_info_v3", self.config["endpoints"]["stream_info"]),
                params={
                    "info": "y",
                    "mediaCode": media_code,
                    "noCache": random_number,
                    "streamCode": stream_quality,
                    "ndvr": "N",
                    "videoTypes": "h264|hevc|av1",
                    "audioTypes": "aac|ac3|ec3",
                    "imageRecommend": "Y",
                    "apiKey": self.config["api_key"]["v3"],
                    "screenCode": device["screen_code"] if is_app else "CSSD1200",
                    "networkCode": device["network_code"],
                    "osCode": device["os_code"],
                    "teleCode": device["tele_code"],
                    "deviceId": device["device_id"],
                    "uuid": device["device_id"],
                    "deviceInfo": device.get("device_info", "Pixel 4 XL") if is_app else "webOS",
                    "wm": "Y",
                    "model": device.get("model", "Pixel 4 XL") if is_app else "webOS",
                    "callingFrom": "APP" if is_app else "HTML5",
                },
                headers={
                    "content-type": "application/json",
                    "accept-language": "ko",
                    **({
                        "Auth-Token": getattr(self, "auth_token", ""),
                        "Authorization": f"Bearer {getattr(self, 'tving_token', '')}",
                        "Access-Token": getattr(self, "app_token", ""),
                    } if is_app and getattr(self, "auth_token", None) else {
                        "authorization": f"Bearer {auth_token}",
                    }),
                    **({
                        "cookie": f"_tving_token={self.tving_token};",
                    } if self.tving_token and not self.app_token else {}),
                },
            )
            data = res.json()
            # Reject if still a GUEST response (cookie auth on locked content)
            if (data.get("body") or {}).get("user", {}).get("id") == "GUEST":
                result_code = ((data.get("body") or {}).get("result") or {}).get("code", "")
                if result_code not in ("000",):
                    return None
            self.is_web_stream = False
            return data
        except Exception as e:
            self.log.warning(f"v3 stream request failed: {e}")
            return None

    def _get_stream_v2(self, media_code: str, stream_quality: str, random_number: str) -> Optional[dict]:
        try:
            device: dict = self.config["device"]
            auth_token = self.app_token or self.tving_token
            is_app = bool(self.app_token)
            
            res = self.session.post(
                self.config["endpoints"]["stream_info"],
                params={
                    "info": "y",
                    "mediaCode": media_code,
                    "noCache": random_number,
                    "streamCode": stream_quality,
                    "ndvr": "N",
                    "videoTypes": "h264|hevc",
                    "audioTypes": "aac|ac3|ec3",
                    "imageRecommend": "Y",
                    "apiKey": self.config["api_key"]["v3"],
                    "screenCode": device["screen_code"] if is_app else "CSSD1200",
                    "networkCode": device["network_code"],
                    "osCode": device["os_code"],
                    "teleCode": device["tele_code"],
                    "deviceId": device["device_id"],
                    "uuid": device["device_id"],
                    "deviceInfo": device.get("device_info", "Pixel 4 XL") if is_app else "webOS",
                    "wm": "Y",
                    "model": device.get("model", "Pixel 4 XL") if is_app else "webOS",
                    "callingFrom": "APP" if is_app else "HTML5",
                },
                headers={
                    "content-type": "application/json",
                    "accept-language": "ko",
                    "authorization": f"Bearer {auth_token}",
                    **({
                        "cookie": f"_tving_token={self.tving_token};",
                    } if self.tving_token and not self.app_token else {}),
                },
            )
            data = res.json()
            if (data.get("body") or {}).get("user", {}).get("id") == "GUEST":
                result_code = ((data.get("body") or {}).get("result") or {}).get("code", "")
                if result_code not in ("000",):
                    return None
            return data
        except Exception as e:
            self.log.warning(f"v2 stream request failed: {e}")
            return None

    def _get_stream_web(self, media_code: str, stream_quality: str, random_number: str) -> Optional[dict]:
        try:
            device: dict = self.config["device"]
            res = self.session.post(
                "https://api.tving.com/v3/media/stream/info",
                params={
                    "screenCode": "CSSD1300",
                    "apiKey": "f0f74595d2f123ffc362362e334aeb52",
                    "networkCode": "CSND0900",
                    "osCode": "CSOD0900",
                    "teleCode": "CSCD0900",
                    "mediaCode": media_code,
                    "streamCode": stream_quality,
                    "deviceId": device["device_id"],
                    "uuid": device["device_id"],
                    "deviceInfo": "PC_Chrome WebView",
                    "model": "PC",
                    "noCache": random_number,
                    "callingFrom": "HTML5",
                    "videoTypes": "h264|hevc|av1",
                    "audioTypes": "aac|ac3|ec3",
                    "wm": "Y",
                },
                headers={
                    "content-type": "application/json",
                    "accept-language": "ko",
                    "cookie": f"_tving_token={self.tving_token};",
                    "authorization": f"Bearer {self.tving_token}",
                },
            )
            return res.json()
        except Exception as e:
            self.log.warning(f"Web stream request failed: {e}")
            return None

    @staticmethod
    def _decrypt_stream(stream: str, vod_code: str, rand_code: str) -> str:
        json_data: dict = json.loads(
            TVING._decrypt_aes(
                stream,
                f"cj*tving/{vod_code[-3:]}/{rand_code[:3]}",
                "6yhlJ4WF9ZIj6I8n",
            )
        )

        url: str = TVING._decrypt_aes(
            json_data.get("broad_url") or json_data.get("url"),
            "kss2lym0kdw1lks3",
            base64.b64decode("KgdyOzcFHgFuRAIzKmEmPA=="),
        )

        return url

    @staticmethod
    def _decrypt_aes(
        data: Union[str, bytes], key: Union[str, bytes], iv: Union[str, bytes]
    ) -> str:
        data = base64.b64decode(data)

        if isinstance(key, str):
            return {}

    def _get_episodes(self, title_id: str) -> list:
        device: dict = self.config["device"]
        screen_code = "CSSD1300" if not getattr(self, "app_token", None) and getattr(self, "tving_token", None) else device["screen_code"]
        res = self.session.get(
            self.config["endpoints"]["episodes"].format(title=title_id),
            params={
                "screenCode": screen_code,
                "networkCode": device["network_code"],
                "osCode": device["os_code"],
                "teleCode": device["tele_code"],
                "apiKey": self.config["api_key"]["v2"],
                "pocType": "APP_X_TVING_5.0.1",
                "order": "new",
                "adult": "all",
                "free": "all",
                "guest": "all",
                "scope": "all",
                "pageNo": "1",
                "pageSize": "100",
                "cacheType": "main",
                "mediaCode": title_id,
            },
        )
        self.is_web_stream = True
        return res.json()["body"]["result"]

    def _get_stream_v3(self, media_code: str, stream_quality: str, random_number: str) -> Optional[dict]:
        try:
            device: dict = self.config["device"]
            auth_token = self.app_token or self.tving_token
            
            # If using app token, we must use the Android device params. 
            # If using web cookie, webOS/CSSD1200 spoofing might still be needed by TVING
            is_app = bool(self.app_token)
            
            res = self.session.post(
                self.config["endpoints"].get("stream_info_v3", self.config["endpoints"]["stream_info"]),
                params={
                    "info": "y",
                    "mediaCode": media_code,
                    "noCache": random_number,
                    "streamCode": stream_quality,
                    "ndvr": "N",
                    "videoTypes": "h264|hevc|av1",
                    "audioTypes": "aac|ac3|ec3",
                    "imageRecommend": "Y",
                    "apiKey": self.config["api_key"]["v3"],
                    "screenCode": device["screen_code"] if is_app else "CSSD1200",
                    "networkCode": device["network_code"],
                    "osCode": device["os_code"],
                    "teleCode": device["tele_code"],
                    "deviceId": device["device_id"],
                    "uuid": device["device_id"],
                    "deviceInfo": device.get("device_info", "Pixel 4 XL") if is_app else "webOS",
                    "wm": "Y",
                    "model": device.get("model", "Pixel 4 XL") if is_app else "webOS",
                    "callingFrom": "APP" if is_app else "HTML5",
                },
                headers={
                    "content-type": "application/json",
                    "accept-language": "ko",
                    **({
                        "Auth-Token": getattr(self, "auth_token", ""),
                        "Authorization": f"Bearer {getattr(self, 'tving_token', '')}",
                        "Access-Token": getattr(self, "app_token", ""),
                    } if is_app and getattr(self, "auth_token", None) else {
                        "authorization": f"Bearer {auth_token}",
                    }),
                    **({
                        "cookie": f"_tving_token={self.tving_token};",
                    } if self.tving_token and not self.app_token else {}),
                },
            )
            data = res.json()
            # Reject if still a GUEST response (cookie auth on locked content)
            if (data.get("body") or {}).get("user", {}).get("id") == "GUEST":
                result_code = ((data.get("body") or {}).get("result") or {}).get("code", "")
                if result_code not in ("000",):
                    return None
            self.is_web_stream = False
            return data
        except Exception as e:
            self.log.warning(f"v3 stream request failed: {e}")
            return None

    def _get_stream_v2(self, media_code: str, stream_quality: str, random_number: str) -> Optional[dict]:
        try:
            device: dict = self.config["device"]
            auth_token = self.app_token or self.tving_token
            is_app = bool(self.app_token)
            
            res = self.session.post(
                self.config["endpoints"]["stream_info"],
                params={
                    "info": "y",
                    "mediaCode": media_code,
                    "noCache": random_number,
                    "streamCode": stream_quality,
                    "ndvr": "N",
                    "videoTypes": "h264|hevc",
                    "audioTypes": "aac|ac3|ec3",
                    "imageRecommend": "Y",
                    "apiKey": self.config["api_key"]["v3"],
                    "screenCode": device["screen_code"] if is_app else "CSSD1200",
                    "networkCode": device["network_code"],
                    "osCode": device["os_code"],
                    "teleCode": device["tele_code"],
                    "deviceId": device["device_id"],
                    "uuid": device["device_id"],
                    "deviceInfo": device.get("device_info", "Pixel 4 XL") if is_app else "webOS",
                    "wm": "Y",
                    "model": device.get("model", "Pixel 4 XL") if is_app else "webOS",
                    "callingFrom": "APP" if is_app else "HTML5",
                },
                headers={
                    "content-type": "application/json",
                    "accept-language": "ko",
                    "authorization": f"Bearer {auth_token}",
                    **({
                        "cookie": f"_tving_token={self.tving_token};",
                    } if self.tving_token and not self.app_token else {}),
                },
            )
            data = res.json()
            if (data.get("body") or {}).get("user", {}).get("id") == "GUEST":
                result_code = ((data.get("body") or {}).get("result") or {}).get("code", "")
                if result_code not in ("000",):
                    return None
            return data
        except Exception as e:
            self.log.warning(f"v2 stream request failed: {e}")
            return None

    def _get_stream_web(self, media_code: str, stream_quality: str, random_number: str) -> Optional[dict]:
        try:
            device: dict = self.config["device"]
            res = self.session.post(
                "https://api.tving.com/v3/media/stream/info",
                params={
                    "screenCode": "CSSD1300",
                    "apiKey": "f0f74595d2f123ffc362362e334aeb52",
                    "networkCode": "CSND0900",
                    "osCode": "CSOD0900",
                    "teleCode": "CSCD0900",
                    "mediaCode": media_code,
                    "streamCode": stream_quality,
                    "deviceId": device["device_id"],
                    "uuid": device["device_id"],
                    "deviceInfo": "PC_Chrome WebView",
                    "model": "PC",
                    "noCache": random_number,
                    "callingFrom": "HTML5",
                    "videoTypes": "h264|hevc|av1",
                    "audioTypes": "aac|ac3|ec3",
                    "wm": "Y",
                },
                headers={
                    "content-type": "application/json",
                    "accept-language": "ko",
                    "cookie": f"_tving_token={self.tving_token};",
                    "authorization": f"Bearer {self.tving_token}",
                },
            )
            return res.json()
        except Exception as e:
            self.log.warning(f"Web stream request failed: {e}")
            return None

    @staticmethod
    def _decrypt_stream(stream: str, vod_code: str, rand_code: str) -> str:
        json_data: dict = json.loads(
            TVING._decrypt_aes(
                stream,
                f"cj*tving/{vod_code[-3:]}/{rand_code[:3]}",
                "6yhlJ4WF9ZIj6I8n",
            )
        )

        url: str = TVING._decrypt_aes(
            json_data.get("broad_url") or json_data.get("url"),
            "kss2lym0kdw1lks3",
            base64.b64decode("KgdyOzcFHgFuRAIzKmEmPA=="),
        )

        return url

    @staticmethod
    def _decrypt_aes(
        data: Union[str, bytes], key: Union[str, bytes], iv: Union[str, bytes]
    ) -> str:
        data = base64.b64decode(data)

        if isinstance(key, str):
            key = key.encode()
        if isinstance(iv, str):
            iv = iv.encode()

        cipher = AES.new(key=key, iv=iv, mode=AES.MODE_CBC)
        return unpad(cipher.decrypt(data), AES.block_size).decode()

    def get_chapters(self, title):
        return []

    def certificate(self, challenge=None, **_):
        if hasattr(self, "ctx") and "playready" in str(type(self.ctx.obj.cdm).__module__).lower():
            return None
        if not challenge:
            return None
        return self.license(challenge=challenge, **_)

    def license(self, challenge, **_):
        if hasattr(self, "ctx") and "playready" in str(type(self.ctx.obj.cdm).__module__).lower():
            return self.get_playready_license(challenge, **_)

        if not getattr(self, "license_url", None):
            return None
        r = self.session.post(
            url=self.license_url, 
            data=challenge,
            headers={self.license_header: self.license_token} if getattr(self, "license_token", None) else {}
        )
        if r.status_code != 200:
            raise ConnectionError(r.content)
        return r.content
