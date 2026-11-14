from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import uuid
from hashlib import md5
from typing import Any, Optional, Union

import click
import m3u8
import requests
from click import Context
from langcodes import Language

from vinetrimmer.objects import MenuTrack, TextTrack, Title, Tracks
from vinetrimmer.services.BaseService import BaseService


class Hoichoi(BaseService):
    """
    Service code for Hoichoi streaming service (https://www.hoichoi.tv/).

    \b
    Authorization: TV Registration
    Security: FHD@L3 UHD@L3
    """

    ALIASES = ["HoiChoi", "hoichoi", "HOICHOI"]
    TITLE_RE = [
        r"^(?:https?://(?:www\.)?hoichoi\.tv/(?:[a-z]{2}/)?(?:movies|shows|movie|series)/)?(?P<id>[a-zA-Z0-9_-]+)",
        r"^(?P<id>[a-zA-Z0-9_-]+)",
    ]

    API_BASE = "https://prod-contents-api.hoichoi.dev/contents/api/v1/contents"
    LICENSE_API = "https://prod-api.hoichoi.dev/contents/api/v1/entitlement"

    @staticmethod
    @click.command(name="Hoichoi", short_help="https://www.hoichoi.tv/")
    @click.argument("title", type=str)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a Movie.")
    @click.option(
        "-l", "--login", "login_method",
        type=click.Choice(["tv", "phone"], case_sensitive=False),
        default="tv",
        help="Login method: 'tv' (TV Registration) or 'phone' (JIO_STB Mobile OTP).",
    )
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> Hoichoi:
        return Hoichoi(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str, movie: bool, login_method: str = "tv"):
        super().__init__(ctx)
        self.parse_title(ctx, title)

        title_lower = str(title).lower()
        if "movie" in title_lower:
            self.movie = True
        elif any(k in title_lower for k in ("shows", "show", "series")):
            self.movie = False
        else:
            self.movie = movie

        self.login_method = (login_method or "tv").lower()
        self.platform = "JIO_STB" if self.login_method == "phone" else "APPLE_TV"

        assert ctx.parent is not None
        self.vcodec = ctx.parent.params.get("vcodec")
        self.acodec = ctx.parent.params.get("acodec")
        self.range = ctx.parent.params.get("range_")

        self.profile = ctx.obj.profile
        self.cdm = getattr(ctx.obj, "cdm", None)
        cdm_device = getattr(self.cdm, "device", self.cdm)
        self.playready = "PLAYREADY" in str(getattr(cdm_device, "type", "")).upper()

        self.token = {}
        self.device_id = None
        self.user_id = None
        self.configure()

    def get_titles(self) -> Union[Title, list[Title]]:
        slug = self.title.rstrip("/").split("/")[-1]
        permalinks = [f"/movies/{slug}", f"/shows/{slug}"] if self.movie else [f"/shows/{slug}", f"/movies/{slug}"]
        if (self.title.startswith("/movies/") or self.title.startswith("/shows/")) and self.title not in permalinks:
            permalinks.insert(0, self.title)

        data = None
        for permalink in permalinks:
            self.log.info(f" + Fetching metadata for {permalink}...")
            try:
                res = self.session.get(
                    self.API_BASE,
                    params={"platform": self.platform, "permalink": permalink, "language": "english", "local": "true", "deviceId": self.device_id},
                    timeout=15,
                )
                if res.status_code == 200:
                    rj = res.json()
                    item = rj[0] if isinstance(rj, list) and rj else rj
                    if isinstance(item, dict) and (item.get("title") or item.get("contentId")):
                        data = item
                        break
            except Exception as e:
                self.log.debug(f"Error fetching {permalink}: {e}")

        if not data:
            self.log.exit(f" - Failed to load title details for '{self.title}'")

        content_type = (data.get("contentType") or "").lower()
        if not ("series" in content_type or "show" in content_type or bool(data.get("seasons"))):
            lang = (data.get("audioLanguages") or ["bn"])[0].lower()
            return Title(
                id_=data.get("contentId") or slug,
                type_=Title.Types.MOVIE,
                name=data.get("title") or slug,
                year=data.get("releaseYear"),
                original_lang=lang,
                source=self.ALIASES[0],
                service_data=data,
            )

        show_title = data.get("title") or slug
        titles = []
        for s_idx, season in enumerate(data.get("seasons", [])):
            season_num = season.get("seasonNumber") or (s_idx + 1)
            for e_idx, ep in enumerate(season.get("episodes", [])):
                ep_num = ep.get("episodeNumber") or ep.get("sequenceNumber") or (e_idx + 1)
                lang = (ep.get("audioLanguages") or ["bn"])[0].lower()
                titles.append(
                    Title(
                        id_=ep.get("contentId") or f"{slug}_s{season_num}_e{ep_num}",
                        type_=Title.Types.TV,
                        name=show_title,
                        season=season_num,
                        episode=ep_num,
                        episode_name=ep.get("title") or f"Episode {ep_num}",
                        year=ep.get("releaseYear") or data.get("releaseYear"),
                        original_lang=lang,
                        source=self.ALIASES[0],
                        service_data=ep,
                    )
                )
        return titles

    def get_entitlement(self, content_id: str, drm: bool = True) -> dict:
        headers = {"content-type": "application/octet-stream", "x-hoichoi-platform": self.platform}
        access_token = self.extract_access_token(self.token)
        if access_token:
            headers["authorization"] = f"Bearer {access_token}"

        platform = "PLAYREADY" if getattr(self, "playready", False) else "WIDEVINE"
        mode = "DRM" if drm else "NON_DRM"

        res = self.session.post(
            url=self.LICENSE_API,
            params={
                "contentId": content_id,
                "action": "STREAMING",
                "platform": platform,
                "language": "english",
                "licenseKey": "false",
                "mode": mode,
                "appPlatform": self.platform,
                "appVersion": "0.0.0",
                "deviceId": self.device_id,
            },
            headers=headers,
            data="{}",
        )
        if res.status_code != 200:
            if drm:
                self.log.info(" - DRM entitlement unavailable, falling back to NON_DRM mode...")
                return self.get_entitlement(content_id, drm=False)
            self.log.exit(f"Failed to get streaming entitlement ({res.status_code}): {res.text}")

        data = res.json()
        stream_key = data.get("streamKey") or data.get("key") or ""

        return {
            "stream_key": str(stream_key),
            "stream_policy": data.get("streamPolicy"),
            "video_details": data.get("videoDetails"),
            "raw": data,
        }

    def get_tracks(self, title: Title) -> Tracks:
        content_data = title.service_data
        content_id = content_data.get("contentId") or title.id

        entitlement = self.get_entitlement(content_id)
        stream_key = entitlement.get("stream_key") or ""
        stream_policy = entitlement.get("stream_policy") or ""

        if isinstance(entitlement.get("video_details"), dict) and entitlement["video_details"].get("renditions"):
            content_data = {**content_data, **entitlement["video_details"]}

        renditions = content_data.get("renditions", [])
        if not renditions:
            self.log.exit(f"No renditions found for {title.id}")

        # Select rendition matching chosen vcodec (e.g. av1, vp9, h264, hevc)
        selected_rendition = None
        if self.vcodec:
            vc = self.vcodec.lower()
            aliases = {
                "av1": ["av1", "av01"],
                "vp9": ["vp9", "vp09"],
                "hevc": ["hevc", "h265", "h.265"],
                "h265": ["hevc", "h265", "h.265"],
                "avc": ["avc", "h264", "h.264"],
                "h264": ["avc", "h264", "h.264"],
            }.get(vc, [vc])

            for r in renditions:
                name_codec = f"{r.get('name', '')} {r.get('codec', '')}".lower()
                if any(a in name_codec for a in aliases):
                    selected_rendition = r
                    break

            if not selected_rendition:
                available = [r.get("codec") or r.get("name") for r in renditions]
                self.log.warning(f" - Codec '{self.vcodec}' not found in renditions {available}, falling back to default")

        if not selected_rendition:
            selected_rendition = next(
                (r for r in renditions if any(a in f"{r.get('name', '')} {r.get('codec', '')}".lower() for a in ["avc", "h264"])),
                renditions[0]
            )

        # Extract manifest URL (prefer .mpd, fallback to .m3u8 or mainManifestUrl)
        alt_urls = selected_rendition.get("alternateManifestUrls", [])
        main_url = selected_rendition.get("mainManifestUrl", "")
        all_urls = [u for u in [main_url] + alt_urls if u]

        manifest_raw = next((u for u in all_urls if ".mpd" in u), None) or next((u for u in all_urls if ".m3u8" in u), main_url or (alt_urls[0] if alt_urls else ""))

        if not manifest_raw:
            self.log.exit(f"No manifest URL found in selected rendition: {selected_rendition}")

        platform = self.platform
        if stream_policy:
            try:
                platform = json.loads(base64.b64decode(stream_policy).decode("utf-8")).get("platform", self.platform)
            except Exception:
                pass

        mpd_params = {
            "CMCD": "ot=m,sf=d,su,tv.hoichoi-dec=193",
            "userId": self.user_id,
            "sessionId": str(uuid.uuid4()),
            "deviceId": self.device_id,
            "appVersion": "0.0.0",
            "platform": platform,
            "contentId": content_id,
        }
        if stream_key:
            mpd_params["stream_key"] = stream_key
        if stream_policy:
            mpd_params["stream_policy"] = stream_policy

        qs = urllib.parse.urlencode(mpd_params)
        manifest_url = f"{manifest_raw}&{qs}" if "?" in manifest_raw else f"{manifest_raw}?{qs}"

        if ".m3u8" in manifest_raw.lower():
            r = self.session.get(manifest_url)
            if r.status_code != 200:
                self.log.exit(f"Failed to fetch m3u8 manifest ({r.status_code}): {r.text}")
            master_m3u8 = m3u8.loads(r.text, uri=manifest_url)
            tracks = Tracks.from_m3u8(master=master_m3u8, source=self.ALIASES[0])
        else:
            tracks = Tracks.from_mpd(url=manifest_url, session=self.session, source=self.ALIASES[0])
        for track in tracks:
            track.needs_proxy = True

        for cap in content_data.get("closedCaptions") or []:
            sub_url = cap.get("vttFile") or cap.get("srtFile")
            if sub_url:
                tracks.add(
                    TextTrack(
                        id_=md5(sub_url.encode()).hexdigest()[:6],
                        source=self.ALIASES[0],
                        url=sub_url,
                        codec="vtt" if ".vtt" in sub_url else "srt",
                        language=Language.get("en"),
                        forced=False,
                        sdh=False,
                    )
                )
        return tracks

    def certificate(self, challenge: bytes, title: Optional[Title] = None, **kwargs: Any) -> Optional[bytes]:
        return self.license(challenge, title=title, **kwargs)

    def license(self, challenge: Union[bytes, str], title: Optional[Title] = None, **_: Any) -> Union[bytes, str]:
        content_id = (
            title.service_data.get("contentId")
            if title and isinstance(getattr(title, "service_data", None), dict)
            else getattr(title, "id", "")
        ) or ""

        challenge_bytes = challenge.encode("utf-8") if isinstance(challenge, str) else challenge
        is_playready = (
            getattr(self, "playready", False)
            or challenge_bytes.strip().startswith(b"<")
            or b"PlayReady" in challenge_bytes
            or b"soap" in challenge_bytes
        )
        platform = "PLAYREADY" if is_playready else "WIDEVINE"

        headers = {"content-type": "application/octet-stream", "x-hoichoi-platform": self.platform}
        access_token = self.extract_access_token(self.token)
        if access_token:
            headers["authorization"] = f"Bearer {access_token}"

        res = self.session.post(
            url=self.LICENSE_API,
            params={
                "contentId": content_id,
                "action": "STREAMING",
                "platform": platform,
                "language": "english",
                "licenseKey": "true",
                "mode": "DRM",
                "appPlatform": self.platform,
                "appVersion": "0.0.0",
                "CMCD": "ot=k,sf=d,su",
            },
            headers=headers,
            data=challenge_bytes,
        )

        if res.status_code != 200:
            self.log.exit(f"Failed to fetch license: {res.status_code} {res.text}")

        try:
            data = res.json()
            payload = data.get("data") or data.get("license") or data.get("licenseKey")
            if payload and isinstance(payload, str):
                return payload if is_playready else base64.b64decode(payload)
        except Exception:
            pass

        return base64.b64encode(res.content).decode("ascii") if is_playready else res.content

    @staticmethod
    def extract_access_token(token_data: Union[dict, str, None]) -> str:
        if isinstance(token_data, str):
            return token_data[7:] if token_data.strip().startswith("Bearer ") else token_data.strip()
        if isinstance(token_data, dict):
            data_field = token_data.get("data")
            sources = [data_field, token_data] if isinstance(data_field, dict) else [token_data]
            for src in sources:
                if isinstance(src, dict):
                    for k in ("sAccessToken", "accessToken", "token", "jwt", "authorizationToken"):
                        val = src.get(k)
                        if isinstance(val, str) and val.strip():
                            return val.strip()[7:] if val.strip().startswith("Bearer ") else val.strip()
        return ""

    @classmethod
    def extract_user_id(cls, token_data: Union[dict, str, None]) -> str:
        if isinstance(token_data, dict):
            for src in (token_data, token_data.get("data"), token_data.get("user")):
                if isinstance(src, dict):
                    uid = src.get("userId") or src.get("id")
                    if uid and str(uid).strip():
                        return str(uid).strip()
        tok = cls.extract_access_token(token_data) if not isinstance(token_data, str) else token_data
        if tok and "." in tok:
            try:
                p = tok.split(".")[1]
                p += "=" * ((4 - len(p) % 4) % 4)
                payload = json.loads(base64.urlsafe_b64decode(p).decode("utf-8"))
                uid = payload.get("userId") or payload.get("sub") or payload.get("rsub")
                if uid and str(uid).strip():
                    return str(uid).strip()
            except Exception:
                pass
        return ""

    def generate_device_id(self) -> str:
        seed = f"AFTKM-{uuid.uuid4()}-{time.time()}"
        return md5(seed.encode("utf-8")).hexdigest()[:16]

    def configure(self) -> None:
        self.token = self.get_auth_token()
        self.device_id = self.token.get("deviceId") or self.generate_device_id()
        self.user_id = self.extract_user_id(self.token)

        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
            "Origin": "https://hoichoi.tv",
            "Referer": "https://hoichoi.tv/",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
            "sec-ch-ua": '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
        })
        access_token = self.extract_access_token(self.token)
        if access_token:
            self.session.headers["Authorization"] = f"Bearer {access_token}"

    @classmethod
    def is_token_expired(cls, token_data: Union[dict, str, None]) -> bool:
        if isinstance(token_data, dict):
            exp = (
                token_data.get("data", {}).get("tokenExpiry")
                if isinstance(token_data.get("data"), dict)
                else token_data.get("tokenExpiry")
            )
            if exp and isinstance(exp, (int, float)):
                return time.time() >= (exp - 60)
        tok = cls.extract_access_token(token_data) if not isinstance(token_data, str) else token_data
        if tok and "." in tok:
            try:
                p = tok.split(".")[1]
                p += "=" * ((4 - len(p) % 4) % 4)
                payload = json.loads(base64.urlsafe_b64decode(p).decode("utf-8"))
                exp = payload.get("exp")
                if exp and isinstance(exp, (int, float)):
                    return time.time() >= (exp - 60)
            except Exception:
                pass
        return False

    def refresh_token(self) -> Optional[dict]:
        refresh_token_val = (
            self.token.get("data", {}).get("sRefreshToken")
            or self.token.get("data", {}).get("refreshToken")
            or self.token.get("sRefreshToken")
            or self.token.get("refreshToken")
        )
        if not refresh_token_val:
            return None

        #self.log.info(" + Refreshing Hoichoi auth token...")
        headers = {
            "authorization": f"Bearer {refresh_token_val}",
            "x-bypass-proxy": "true",
            "x-hoichoi-siteid": "hoichoitv",
            "rid": "anti-csrf",
            "content-type": "application/json",
            "origin": "https://hoichoi.tv",
            "referer": "https://hoichoi.tv/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
        }

        try:
            res = requests.post(
                "https://prod-api.hoichoi.dev/core/api/v1/auth/session/refresh",
                headers=headers,
                json={},
                timeout=15,
            )
            if res.status_code in (200, 201):
                res_data = res.json() if res.text else {}
                new_tokens = res_data.get("data") if isinstance(res_data.get("data"), dict) else res_data
                if isinstance(new_tokens, dict) and self.extract_access_token(new_tokens):
                    if not isinstance(self.token.get("data"), dict):
                        self.token["data"] = {}
                    # Updates sAccessToken, sRefreshToken (if rotated), tokenExpiry, etc.
                    self.token["data"].update(new_tokens)

                    user_id = self.extract_user_id(self.token)
                    if user_id:
                        self.token["userId"] = user_id

                    cache_path = self.get_cache(f"token_{self.profile}.json")
                    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                    with open(cache_path, "w", encoding="utf-8") as fd:
                        json.dump(self.token, fd, indent=2)
                    self.log.info(" + Token refreshed successfull")
                    return self.token
        except Exception as e:
            self.log.warning(f" - Token refresh error: {e}")

        return None

    def get_auth_token(self) -> dict:
        token_cache_path = self.get_cache(f"token_{self.profile}.json")
        if os.path.isfile(token_cache_path):
            try:
                with open(token_cache_path, encoding="utf-8") as fd:
                    token = json.load(fd)
                self.token = token
                if isinstance(token, dict) and self.extract_access_token(token):
                    if self.is_token_expired(token):
                        self.log.info(" + Access token expired, attempting refresh...")
                        refreshed = self.refresh_token()
                        if refreshed:
                            return refreshed
                    else:
                        self.log.info(" + Using cached auth tokens...")
                        return token
                if isinstance(token, str) and token.strip():
                    self.log.info(" + Using cached auth tokens...")
                    raw_tok = token.strip()
                    return {"userId": self.extract_user_id(raw_tok), "deviceId": self.generate_device_id(), "data": {"sAccessToken": raw_tok}}
            except Exception:
                pass

        self.log.info(" + Logging into Hoichoi...")
        token = self.login()
        os.makedirs(os.path.dirname(token_cache_path), exist_ok=True)
        with open(token_cache_path, "w", encoding="utf-8") as fd:
            json.dump(token, fd, indent=2)
        return token

    def login(self) -> dict:
        if getattr(self, "login_method", "tv") == "phone":
            return self.login_phone()
        return self.login_tv()

    def login_phone(self) -> dict:
        device_id = self.generate_device_id()
        phone_input = click.prompt("Enter your mobile number (e.g. 9876543210)").strip()
        phone_number = phone_input if phone_input.startswith("+") else f"+91{phone_input}"

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Linux; Android 11; JIO4KSTB Build/RTT0.210829.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/118.0.5993.80 Safari/537.36 JioSTB/3.2.0",
            "x-bypass-proxy": "true",
            "x-hoichoi-platform": "JIO_STB",
            "x-hoichoi-siteid": "hoichoitv",
            "x-tenant-id": "hoichoitv",
            "Origin": "https://tv.hoichoi.tv",
            "Referer": "https://tv.hoichoi.tv/",
        }

        send_payload = {
            "phoneNumber": phone_number,
            "acceptTermsAndConditions": True,
            "otpInWhatsApp": False,
            "platform": "JIO_STB",
            "deviceId": device_id,
        }

        res = requests.post("https://prod-api.hoichoi.dev/core/api/v1/auth/signinup/code", headers=headers, json=send_payload, timeout=15)
        if res.status_code not in (200, 201):
            self.log.exit(f"- Failed to send OTP ({res.status_code}): {res.text}")

        data = res.json()
        pre_auth_session_id = (
            data.get("preAuthSessionId")
            or (data.get("data", {}).get("preAuthSessionId") if isinstance(data.get("data"), dict) else None)
            or data.get("sessionId")
        )
        server_device_id = (
            data.get("deviceId")
            or (data.get("data", {}).get("deviceId") if isinstance(data.get("data"), dict) else None)
            or device_id
        )

        if not pre_auth_session_id:
            self.log.exit(f"- Pre-auth session ID missing in response: {res.text}")

        otp_code = click.prompt("Enter the 6-digit OTP received on your phone").strip()

        verify_payload = {
            "preAuthSessionId": pre_auth_session_id,
            "userInputCode": otp_code,
            "deviceId": server_device_id,
            "platform": "JIO_STB",
        }

        verify_res = requests.post("https://prod-api.hoichoi.dev/core/api/v1/auth/signinup/code/consume", headers=headers, json=verify_payload, timeout=15)
        if verify_res.status_code not in (200, 201):
            self.log.exit(f"- Verification failed ({verify_res.status_code}): {verify_res.text}")

        rj = verify_res.json()
        tokens = rj.get("data") if isinstance(rj.get("data"), dict) else rj
        user_id = self.extract_user_id(tokens)
        self.log.info(" + OTP login successful!")
        return {
            "userId": user_id,
            "deviceId": server_device_id,
            "data": tokens,
        }

    def login_tv(self) -> dict:
        device_id = self.generate_device_id()
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip;q=1.0,deflate;q=0.9",
            "Content-Type": "application/json",
            "Connection": "Keep-Alive",
            "User-Agent": "com.apple.tv/1.0 iOS/16.1 AppleTV/16.1 model/AppleTV6,2 hwp/t8011 build/20K71 (3; dt:163)", #Mozilla/5.0 (Linux; Android 12; BRAVIA 4K VH2 Build/STT2.221208.001) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.5735.196 Safari/537.36
            "x-bypass-proxy": "true",
            "x-hoichoi-platform": "APPLE_TV",
            "x-hoichoi-siteid": "hoichoitv",
            "x-tenant-id": "hoichoitv",
        }
        params = {"appPlatform": "APPLE_TV", "appVersion": "1.6.1", "lang": "english"}
        json_data = {"deviceId": device_id, "platform": "APPLE_TV"}

        r_code = requests.post("https://prod-api.hoichoi.dev/core/api/v1/user-device/generate-activation-code", params=params, headers=headers, json=json_data)
        if r_code.status_code != 200:
            self.log.exit(f"- Failed to generate TV activation code ({r_code.status_code}): {r_code.text}")

        res = r_code.json()
        code = res.get("activationCode") or (res.get("data", {}).get("activationCode") if isinstance(res.get("data"), dict) else None) or res.get("code")
        expires_in = res.get("expiresIn") or (res.get("data", {}).get("expiresIn") if isinstance(res.get("data"), dict) else None) or 300
        if not code:
            self.log.exit(f"- Activation code missing in response: {r_code.text}")

        self.log.info(f"Go to https://hoichoi.tv/settings/devices and enter: {code}")

        start_time = time.time()
        timeout = int(expires_in) if expires_in else 300
        while time.time() - start_time < timeout:
            time.sleep(3)
            try:
                poll_res = requests.post("https://prod-api.hoichoi.dev/core/api/v1/user-device/activate-device", params=params, headers=headers, json=json_data, timeout=10)
                if poll_res.status_code in (200, 201):
                    rj = poll_res.json() if poll_res.text else {}
                    data = rj.get("data") if isinstance(rj.get("data"), dict) else rj
                    if isinstance(data, dict) and self.extract_access_token(data):
                        user_id = data.get("userId") or self.extract_user_id(data)
                        self.log.info(" + Verified successfully!")
                        return {
                            "userId": user_id,
                            "deviceId": device_id,
                            "data": data,
                        }
            except Exception:
                pass

        self.log.exit("- TV login timed out. Please run the command again to generate a new code.")
