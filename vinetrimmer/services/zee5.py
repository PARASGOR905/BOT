import base64
import json
import os
import re
import time
import traceback
import uuid
import click

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.objects.tracks import MenuTrack
from vinetrimmer.services.BaseService import BaseService


class Zee5(BaseService):
    """
    Service code for ZEE5 streaming service (https://zee5.com).

    Authorization: TV Device-Code Login  →  https://www.zee5.com/device
    Security: UHD@L3 
    Mobile Login Support Added.

    Login flow (one-time, tokens cached for ~4 days):
      1. Run any VT download command — a device code is requested automatically
      2. Open https://www.zee5.com/device in your browser
      3. Log in to your Zee5 account and enter the code shown in the terminal
      4. Press Enter — the script fetches and caches your user JWT

    Cache: vinetrimmer/cache/ZEE5/zee5_token_<profile>.json
    Delete the cache file to force a fresh login.
    Load Widevine service certificate from a local PEM file if available,
    otherwise fall back to fetching it via the license endpoint.
    Place the cert at: vinetrimmer/services/zee5_certificate.pem

    Made by: BruceWayne AKA ImMarvelGroot
    """

    ALIASES = ["ZEE5", "Z5", "Zee5"]

    _H264_MANIFEST_PREFERENCE = [
        "manifest-connected-ddplus-avc.mpd",
        "manifest-connected.mpd",
        "manifest-high.mpd",
        "manifest.mpd",
        "manifest-mid.mpd",
        "manifest-phone-ddplus-avc.mpd",
        "manifest-phone.mpd",
        "manifest-low.mpd",
    ]

    _H265_MANIFEST_PREFERENCE = [
        "manifest-connected-4k.mpd",
        "manifest-4k-connected-hevc-aac.mpd",
        "manifest-connected-ddplus-hevc.mpd",
        "manifest-connected-hevc.mpd",
        "manifest-hevc.mpd",
        "manifest-mid-hevc.mpd",
        "manifest-phone-ddplus-hevc.mpd",
        "manifest-phone-hevc.mpd",
    ]

    _V2PRIME_PREFERENCE = [
        "manifest-connected-4k.mpd",
        "manifest-connected-ddplus-hevc.mpd",
        "manifest-connected-hevc.mpd",
        "manifest-connected-ddplus-avc.mpd",
    ]

    _ESK_WEB_KEY = "gBQaZLiNdGN9UsCKZaloghz9t9StWLSD"

    TITLE_RE = (
        r"^https?://(?:www\.)?zee5\.com/(?:[a-z]{2}/)?"
        r"(?P<type>movies|tvshows|tv-shows|zee5originals|web-series)/"
        r"(?:details/)?(?P<slug>[a-z0-9-]+)/(?P<id>[\w-]+)"
    )

    @staticmethod
    @click.command(name="Zee5", short_help="https://zee5.com")
    @click.argument("title", type=str, required=False)
    @click.option("-rg", "--region", default="IN", type=str,
                  help="Region / country code (default: IN)")
    @click.option("-lang", "--language", default="en", type=str,
                  help="Preferred content language (default: en)")
    @click.option("-m", "--mobile", default=None, type=str,
                  help="Mobile number for OTP login, e.g. 919876543210 (country code + number)")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Zee5(ctx, **kwargs)

    def __init__(self, ctx, title, region, language, mobile):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self.region = region.upper()
        self.language = language.lower()
        self.mobile = mobile

        _m = re.search(self.TITLE_RE, title or "")
        self.url_type = (_m.group("type") if _m else "").lower()

        assert ctx.parent is not None
        self.vcodec = ctx.parent.params.get("vcodec") or "H265"
        self.acodec = ctx.parent.params.get("acodec") or "AAC"
        self.range = ctx.parent.params.get("range_") or "SDR"
        self.quality = ctx.parent.params.get("quality")

        if self.range != "SDR" and self.vcodec != "H265":
            self.log.info(f" + Switched to H265 for {self.range}")
            self.vcodec = "H265"

        self.profile = ctx.obj.profile

        self.device_id = None
        self.profile_id = None
        self.authorization = None
        self.access_token = None
        self.x_dd_token = None

        self.custom_data = None
        self.nl_token = None
        self._cached_playback = None
        self.original_language = None
        self.license_mpd_url = None
        self.configure()

    def configure(self):
        self.session.headers.update({
            "Origin": "https://www.zee5.com",
            "Referer": "https://www.zee5.com/",
        })

        cache_suffix = f"mobile_{self.mobile}" if self.mobile else self.profile
        cache_path = self.get_cache(f"zee5_token_{cache_suffix}.json")

        if os.path.isfile(cache_path):
            with open(cache_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("exp", 0) > int(time.time()):
                self._load_cache(cached)
                self.log.info(" + Loaded cached tokens (delete cache to re-login)")
                self.log.info(f" + Device ID:  {self.device_id}")
                self.log.info(f" + Profile ID: {self.profile_id}")
                return
            self.log.info(" + Cache expired — checking cookies...")

        # 1. Try loading authentication tokens from cookies
        cookie_token = self.session.cookies.get("token") or self.session.cookies.get("authorization")
        cookie_access_token = self.session.cookies.get("xaccesstoken") or self.session.cookies.get("x-access-token")

        if cookie_token or cookie_access_token:
            self.authorization = cookie_token
            self.access_token = cookie_access_token or cookie_token
            self.profile_id = (
                self.session.cookies.get("profile-id")
                or self._extract_profile_id(cookie_token)
                or str(uuid.uuid4())
            )
            self.device_id = self.session.cookies.get("guestToken") or str(uuid.uuid4())
            self.x_dd_token = self._build_dd_token()
            self.log.info(" + Loaded authentication tokens from cookies")
            self.log.info(f" + Profile ID: {self.profile_id}")
            return

        if self.mobile:
            if not self.device_id:
                self.device_id = str(uuid.uuid4())
            self._mobile_otp_login(cache_path)
        else:
            # Fallback to guest token / platform token if no TV login desired
            self.log.info(" + No cached/cookie tokens found — initializing guest session...")
            self.device_id = str(uuid.uuid4())
            self._fetch_platform_token()
            self.x_dd_token = self._build_dd_token()

    def _make_esk(self, key: str | None = None) -> str:
        ts = int(time.time() * 1000)
        raw = f"{self.device_id}__{key or self.config['esk_key']}__{ts}"
        return base64.b64encode(raw.encode()).decode()

    def _extract_profile_id(self, token: str) -> str:
        return (
            self._jwt_claim(token, "user_id")
            or self._jwt_claim(token, "sub")
            or self.device_id
        )

    def _tv_login(self, cache_path: str):
        self.device_id = str(uuid.uuid4())

        headers = {
            "User-Agent": "okhttp/4.9.3",
            "device_id": self.device_id,
            "token": self.device_id,
            "esk": self._make_esk(),
        }

        self.log.info(" + Requesting AndroidTV device code…")
        gen_res = self.session.post(
            self.config["endpoints"]["device_code"],
            headers=headers,
            params={"device_name": self.config["device"]["name"]},
            timeout=15,
        )
        self.log.debug(f"getcode → HTTP {gen_res.status_code}: {gen_res.text[:300]}")

        if gen_res.status_code not in (200, 201):
            raise ValueError(
                f"device_code request failed (HTTP {gen_res.status_code}): {gen_res.text}"
            )

        device_code = gen_res.json().get("device_code")
        if not device_code:
            raise ValueError(f"No device_code in response: {gen_res.json()}")

        self.log.info("")
        self.log.info("━" * 60)
        self.log.info("  ZEE5 TV LOGIN")
        self.log.info("━" * 60)
        self.log.info("  1. Open  https://www.zee5.com/device  in your browser")
        self.log.info("  2. Log in to your Zee5 account")
        self.log.info(f"  3. Enter this code:  {device_code}")
        self.log.info("━" * 60)
        self.log.info("")
        input("Press Enter after completing pairing on zee5.com/device …")

        self.log.info(" + Fetching user token…")
        headers["esk"] = self._make_esk()

        token_res = self.session.post(
            self.config["endpoints"]["device_token"],
            headers=headers,
            params={
                "device_code": device_code,
                "device_name": self.config["device"]["name"],
            },
            timeout=15,
        )
        self.log.debug(f"device_token → HTTP {token_res.status_code}: {token_res.text[:300]}")

        if token_res.status_code not in (200, 201):
            raise ValueError(
                f"Token fetch failed (HTTP {token_res.status_code}). "
                f"Did you complete the pairing? Response: {token_res.text}"
            )

        token_data = token_res.json()
        self.authorization = token_data.get("token")
        if not self.authorization:
            raise ValueError(
                f"No token in response — please try pairing again. Response: {token_data}"
            )

        self.profile_id = self._extract_profile_id(self.authorization)
        self.log.info(f" + Login successful!  Profile ID: {self.profile_id}")

        self._fetch_platform_token()
        self.x_dd_token = self._build_dd_token()
        self._save_cache(cache_path, token_data)

    def _mobile_otp_login(self, cache_path: str):
        raw = self.mobile.strip().lstrip("+").replace(" ", "").replace("-", "")
        if not raw.isdigit():
            raise ValueError(f"Invalid mobile number '{raw}'. Use digits only, e.g. 919876543210")

        phoneno = raw if (raw.startswith("91") and len(raw) == 12) else "91" + raw

        auth_hdrs = {
            "device_id": self.device_id,
            "esk": self._make_esk(self._ESK_WEB_KEY),
            "x-z5-guest-token": self.device_id,
            "content-type": "application/json",
            "accept": "application/json",
        }
        self.log.info(f" + Sending OTP to {phoneno}…")

        otp_r = self.session.post(
            "https://auth.zee5.com/v1/user/sendotp",
            headers=auth_hdrs,
            json={"phoneno": phoneno},
            timeout=15,
        )
        self.log.debug(f"sendotp → HTTP {otp_r.status_code}: {otp_r.text[:300]}")

        if otp_r.status_code not in (200, 201):
            raise ValueError(f"sendotp failed (HTTP {otp_r.status_code}): {otp_r.text}")

        otp_data = otp_r.json()
        if otp_data.get("code") == 2:
            raise ValueError(f"OTP send error: {otp_data.get('message', otp_data)}")

        self.log.info(f" + OTP sent: {otp_data.get('message', 'SMS dispatched')}")
        self.log.info("")
        self.log.info("━" * 60)
        self.log.info("  ZEE5 MOBILE OTP LOGIN")
        self.log.info("━" * 60)
        self.log.info(f"  OTP sent to: {phoneno}")
        self.log.info("━" * 60)
        self.log.info("")
        otp = input("Enter the OTP received on your phone: ").strip()

        if not otp or not otp.isdigit():
            raise ValueError(f"Invalid OTP entered: '{otp}'")

        self.log.info(" + Verifying OTP…")
        auth_hdrs["esk"] = self._make_esk(self._ESK_WEB_KEY)

        verify_r = self.session.post(
            "https://auth.zee5.com/v1/user/verifyotp",
            headers=auth_hdrs,
            json={
                "phoneno": phoneno,
                "otp": otp,
                "platform": "androidtv",
                "device": "tv",
                "version": "9.3.5",
            },
            timeout=15,
        )
        self.log.debug(f"verifyotp → HTTP {verify_r.status_code}: {verify_r.text[:300]}")

        if verify_r.status_code not in (200, 201):
            raise ValueError(
                f"verifyotp failed (HTTP {verify_r.status_code}): {verify_r.text}"
            )

        verify_data = verify_r.json()
        self.authorization = (
            verify_data.get("access_token")
            or verify_data.get("token")
            or verify_data.get("secureToken")
        )
        if not self.authorization:
            raise ValueError(f"No token in verifyotp response: {verify_data}")

        self.profile_id = self._extract_profile_id(self.authorization)
        self.log.info(f" + Mobile OTP login successful!  Profile ID: {self.profile_id}")

        self._fetch_platform_token()
        self.x_dd_token = self._build_dd_token()
        self._save_cache(cache_path, verify_data)

    def _fetch_platform_token(self):
        ua = self.config["device"]["user_agent"]

        params = {
            "ccode": self.region,
            "lang": "en",
            "version": "4",
            "country": self.region,
            "state": self.config["defaults"].get("state", "MH"),
            "translation": "en",
            "platform_name": "androidtv_app",
            "partner_name": "",
        }
        try:
            r = self.session.get(
                self.config["endpoints"]["platform_token"],
                params=params,
                timeout=10,
                headers={"Accept": "application/json", "User-Agent": ua},
            )
            self.log.debug(f"platform_token [androidtv_app] → {r.status_code}: {r.text[:200]}")
            if r.status_code == 200:
                data = r.json()
                tok = (
                    (data.get("platform_token") or {}).get("token")
                    or data.get("token")
                    or data.get("access_token")
                )
                if tok:
                    self.access_token = tok
                    self.log.info(" + x-access-token fetched [androidtv_app] — 4K manifest enabled")
                    return
        except Exception as exc:
            self.log.debug(f"   platform_token [androidtv_app] → {exc}")

        for platform in ("ctv_android",):
            url = f"{self.config['endpoints']['platform_token_alt']}?platform_name={platform}"
            try:
                r = self.session.get(url, timeout=10, headers={
                    "Accept": "application/json",
                    "Authorization": f"bearer {self.authorization}",
                    "User-Agent": ua,
                })
                self.log.debug(f"   platform_token_alt [{platform}] → {r.status_code}: {r.text[:150]}")
                if r.status_code == 200:
                    tok = r.json().get("token") or r.json().get("access_token")
                    if tok:
                        self.access_token = tok
                        self.log.info(f" + x-access-token fetched from platform_token_alt [{platform}]")
                        return
            except Exception as exc:
                self.log.debug(f"   platform_token_alt [{platform}] → {exc}")

        self.log.warning(" + Could not fetch platform token — using user JWT as fallback")
        self.access_token = self.authorization

    def _load_cache(self, data: dict):
        self.device_id = data.get("device_id")
        self.authorization = data.get("authorization")
        self.access_token = data.get("access_token")
        self.profile_id = (
            self._jwt_claim(self.authorization, "user_id")
            or data.get("profile_id")
            or self.device_id
        )
        self.x_dd_token = self._build_dd_token()

    def _save_cache(self, path: str, raw: dict):
        exp = self._jwt_claim(self.authorization, "exp")
        ttl = int(exp) if exp else int(time.time()) + 345_600

        os.makedirs(os.path.dirname(path), exist_ok=True)
        cache = {
            "authorization": self.authorization,
            "access_token": self.access_token,
            "device_id": self.device_id,
            "profile_id": self.profile_id,
            "exp": ttl,
            "raw": raw,
        }
        with open(path, mode="w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=4)
        self.log.info(f" + Tokens cached → {path}")

    @staticmethod
    def _jwt_claim(token: str | None, claim: str):
        if not token:
            return None
        try:
            part = token.split(".")[1]
            part += "=" * (-len(part) % 4)
            return json.loads(base64.b64decode(part)).get(claim)
        except Exception:
            return None

    def _build_dd_token(self) -> str:
        return (
            "eyJvc19uYW1lIjoiQW5kcm9pZCIsIm9zX3ZlcnNpb24iOiIxMCIsInBsYXRmb3JtX25hbWUiOiJYaWFvbWkiLCJwbGF0"
            "Zm9ybV92ZXJzaW9uIjoiMjkiLCJkZXZpY2VfbmFtZSI6Ik1pVFYtTU9PUTEiLCJhcHBfbmFtZSI6ImN0dl9hb3NwIiwi"
            "YXBwX3ZlcnNpb24iOiI1LjIyLjgiLCJwbGF5ZXJfY2FwYWJpbGl0aWVzIjp7ImF1ZGlvX2NoYW5uZWwiOlsiU1RFUkVP"
            "IiwiRE9MQlkiLCJET0xCWV9BVE1PUyJdLCJ2aWRlb19jb2RlYyI6WyJIMjY0IiwiSEVWQyJdLCJyZXNvbHV0aW9uIjpb"
            "IkhEIiwiRkhEIiwiVUhEIl0sImR5bmFtaWNfcmFuZ2UiOlsiU0RSIiwiSERSIiwiRE9MQllfVklTSU9OIiwiSExHIl19"
            "LCJzZWN1cml0eV9jYXBhYmlsaXRpZXMiOnsiZW5jcnlwdGlvbiI6WyJXSURFVklORV9BRVNfQ1RSIiwiUExBWVJFQURZ"
            "X0FFU19DVFIiLCJXSURFVklORV9BRVNfQ0JDUyJdLCJ3aWRldmluZV9zZWN1cml0eV9sZXZlbCI6WyJMMSJdLCJoZGNw"
            "X3ZlcnNpb24iOlsiSERDUF9WMSJdfX0="
        )

    def _get_headers(self) -> dict:
        dev = self.config["device"]
        return {
            "Authorization": f"bearer {self.authorization}" if self.authorization else "",
            "Content-Type": "application/json; charset=utf-8",
            "X-ACCESS-TOKEN": self.access_token or "",
            "X-User-Type": "premium",
            "X-Z5-AppPlatform": "Android TV",
            "X-Z5-Appversion": "5.40.0",
            "profile-id": self.profile_id or "",
            "Origin": "https://www.zee5.com",
            "Referer": "https://www.zee5.com/",
            "User-Agent": dev["user_agent"],
        }

    @staticmethod
    def _is_show_id(content_id: str) -> bool:
        if not content_id:
            return False
        parts = content_id.split("-")
        return len(parts) >= 2 and parts[1] in ("6", "7")

    def _fetch_episodes(self, show_id: str) -> list:
        url = self.config["endpoints"]["tvshow"].format(show_id=show_id)
        self.log.info(f" + Fetching show data: {url}")

        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
            "Authorization": f"bearer {self.authorization}" if self.authorization else "",
            "x-access-token": self.access_token or "",
            "Origin": "https://www.zee5.com",
            "Referer": "https://www.zee5.com/",
            "User-Agent": self.config["device"]["user_agent"],
        }
        r = self.session.get(
            url,
            params={"translation": "en", "country": self.region},
            headers=headers,
            timeout=30,
        )
        self.log.info(f" + tvshow API → HTTP {r.status_code}")

        if r.status_code != 200 or not r.text.strip():
            raise ValueError(
                f"tvshow API returned HTTP {r.status_code} for show ID: {show_id}. "
                f"Response: {r.text[:300]}"
            )

        try:
            data = r.json()
        except Exception as exc:
            raise ValueError(f"tvshow API returned invalid JSON: {exc}") from exc

        seasons = data.get("seasons") or []
        if not seasons:
            raise ValueError(
                f"tvshow API returned no seasons for show ID: {show_id}. "
                f"Keys present: {list(data.keys())}"
            )

        self.log.info(f" + Found {len(seasons)} season(s)")
        all_episodes = []

        for season in seasons:
            season_num = season.get("orderid") or season.get("index") or 1
            eps = [
                ep for ep in (season.get("episodes") or [])
                if ep.get("asset_subtype") in ("episode", None, "")
                or ep.get("asset_type") == "episode"
            ]
            for ep in eps:
                if not ep.get("season_number"):
                    ep["season_number"] = season_num
            self.log.info(
                f"   Season {season_num}: {len(eps)} episode(s) "
                f"(of {season.get('total_episodes', '?')} total)"
            )
            all_episodes.extend(eps)

        if not all_episodes:
            raise ValueError(
                f"No episodes found for show ID: {show_id}. "
                "Try passing a direct episode ID (0-1-xxxx) instead."
            )

        def _sort_key(ep):
            try:
                s = int(ep.get("season_number") or ep.get("season") or 1)
                e = int(ep.get("episode_number") or ep.get("episode") or 0)
            except (ValueError, TypeError):
                s, e = 1, 0
            return (s, e)

        all_episodes.sort(key=_sort_key)
        self.log.info(f" + Total episodes: {len(all_episodes)}")
        return all_episodes

    def _playback_request(self, content_id: str,
                          extra_params: dict | None = None,
                          body_override: dict | None = None) -> dict:
        if self._is_show_id(content_id):
            raise ValueError(
                f"_playback_request() received show-level ID '{content_id}'. "
                "Call _fetch_episodes() first to resolve individual episode IDs."
            )

        dev = self.config["device"]
        defaults = self.config["defaults"]

        params = {
            "content_id": content_id,
            "show_id": "",
            "device_id": self.device_id,
            "session_id": "",
            "check_parental_control": "false",
            "current_parental_control": "",
            "country": self.region,
            "platform_name": dev["platform"],
            "state": defaults["state"],
            "translation": "en",
            "display_language": "en",
            "user_language": "hi,en",
            "collection_id": "",
            "app_version": dev["app_version"],
            "gender": "",
            "age": "",
            "brand": "ripx",
            "model": "ripx",
            "version": "12",
        }
        if extra_params:
            params.update(extra_params)

        body = {
            "x-access-token": self.access_token or "",
            "Authorization": f"bearer {self.authorization}" if self.authorization else "",
            "X-Z5-Guest-Token": "",
            "x-dd-token": self.x_dd_token or "",
        }
        if body_override:
            body.update(body_override)

        r = self.session.post(
            url=self.config["endpoints"]["playback"],
            params=params,
            headers=self._get_headers(),
            json=body,
        )
        self.log.debug(f" + Playback API → HTTP {r.status_code}")

        if not r.text.strip():
            raise ValueError(
                f"Empty playback response (HTTP {r.status_code}). "
                "Token may have expired — delete the cache file to re-login."
            )
        try:
            data = r.json()
        except Exception as exc:
            raise ValueError(
                f"Non-JSON playback response (HTTP {r.status_code}): {r.text[:500]}"
            ) from exc

        if r.status_code != 200:
            err = data.get("error_msg") or data.get("message") or str(data)
            raise ValueError(f"Playback API error {r.status_code}: {err}")

        return data

    def get_titles(self):
        content_id = self.title
        try:
            if self._is_show_id(content_id):
                self.log.info(f" + Show/series ID detected: {content_id}")
                episodes = self._fetch_episodes(content_id)

                titles = []
                for ep in episodes:
                    ep_id = ep.get("id") or ep.get("asset_id") or ep.get("content_id")
                    if not ep_id:
                        continue

                    ep_title = ep.get("title") or ep.get("name") or "Unknown"
                    show_title = (
                        (ep.get("tvshow_details") or {}).get("title")
                        or (ep.get("show") or {}).get("title")
                        or ep.get("show_title")
                        or ep_title
                    )
                    lang = (
                        ep.get("primary_language")
                        or (ep.get("audio_languages") or [self.language])[0]
                        or self.language
                    )
                    season_num = int(ep.get("season_number") or ep.get("season") or 1)
                    episode_num = int(ep.get("episode_number") or ep.get("episode") or 1)

                    titles.append(Title(
                        id_=ep_id,
                        type_=Title.Types.TV,
                        name=show_title,
                        season=season_num,
                        episode=episode_num,
                        episode_name=ep_title,
                        original_lang=lang,
                        source=self.ALIASES[0],
                        service_data=ep,
                    ))

                if not titles:
                    raise ValueError(f"No valid episode titles for show: {content_id}")

                self.log.info(f" + Returning {len(titles)} episode(s)")
                return titles

            data = self._playback_request(content_id)
            self._cached_playback = data

            asset = data.get("assetDetails", {})
            self.original_language = (
                asset.get("primary_language")
                or (asset.get("audio_languages") or [self.language])[0]
                or self.language
            )

            title_name = asset.get("title") or asset.get("name") or "Unknown"
            year_raw = (asset.get("release_date") or "")[:4]
            year = int(year_raw) if year_raw and year_raw.isdigit() else None
            asset_subtype = (asset.get("asset_subtype") or "").lower()

            self.log.info(f" + asset_subtype='{asset_subtype}', url_type='{self.url_type}'")

            if self.url_type == "movies" or asset_subtype == "movie":
                return Title(
                    id_=content_id,
                    type_=Title.Types.MOVIE,
                    name=title_name,
                    year=year,
                    original_lang=self.original_language,
                    source=self.ALIASES[0],
                    service_data=asset,
                )

            season_num = int(asset.get("season_number") or 1)
            episode_num = int(asset.get("episode_number") or 1)
            show_title = (
                (asset.get("show") or {}).get("title")
                or asset.get("show_title")
                or title_name
            )
            return Title(
                id_=content_id,
                type_=Title.Types.TV,
                name=show_title,
                season=season_num,
                episode=episode_num,
                episode_name=title_name,
                original_lang=self.original_language,
                source=self.ALIASES[0],
                service_data=asset,
            )

        except Exception as exc:
            self.log.error(f"Failed to get title: {exc}")
            self.log.debug(traceback.format_exc())
            raise

    @staticmethod
    def _is_v2prime(mpd_url: str) -> bool:
        return "v2-prime.akamaized.net" in mpd_url or "hdnea=" in mpd_url

    def _get_manifest_preference(self) -> list:
        if self.vcodec == "H264":
            return self._H264_MANIFEST_PREFERENCE + self._H265_MANIFEST_PREFERENCE
        return self._H265_MANIFEST_PREFERENCE + self._H264_MANIFEST_PREFERENCE

    def _build_mpd_candidates(self, mpd_url: str) -> list:
        m = re.search(r"manifest(?:-[^/?]+)?\.mpd", mpd_url)
        if not m:
            return [mpd_url]
        base = mpd_url[:m.start()]
        tail = mpd_url[m.end():]
        candidates = [base + name + tail for name in self._get_manifest_preference()]
        if mpd_url not in candidates:
            candidates.append(mpd_url)
        return candidates

    def _score_manifest(self, url: str) -> int:
        m = re.search(r"manifest(?:-[^/?]+)?\.mpd", url)
        if not m:
            return -1
        pref = self._get_manifest_preference()
        try:
            return len(pref) - pref.index(m.group(0))
        except ValueError:
            return 0

    def _try_v2prime_4k(self, content_id: str, fallback_url: str) -> str:
        if self.vcodec == "H264":
            return fallback_url

        attempts = [
            ("max-dd-token", {}, None),
            ("hisense-model", {"brand": "hisense", "model": "4K Hisense Android TV", "version": "28"}, None),
            ("version-9", {"version": "9"}, None),
        ]

        for desc, extra_params, body_override in attempts:
            try:
                data = self._playback_request(
                    content_id,
                    extra_params=extra_params,
                    body_override=body_override,
                )
                url = (data.get("assetDetails") or {}).get("video_url", {}).get("mpd", "")
                if not url:
                    continue
                m = re.search(r"manifest-[^/?]+\.mpd", url)
                name = m.group(0) if m else "manifest-connected.mpd"
                if name in self._V2PRIME_PREFERENCE:
                    self.log.info(f" + V2PRIME [{desc}]: got {name} ✓")
                    return url
                self.log.debug(f" + V2PRIME [{desc}]: returned {name}")
            except Exception as exc:
                self.log.debug(f" + V2PRIME [{desc}]: {exc}")

        self.log.debug(" + V2PRIME: all attempts returned manifest-connected.mpd — using fallback")
        return fallback_url

    def _select_mpd_url(self, mpd_url: str) -> str:
        if self._is_v2prime(mpd_url):
            m = re.search(r"manifest[^/?]*\.mpd", mpd_url)
            self.log.info(f" + V2PRIME fallback: {m.group(0) if m else 'manifest-connected.mpd'}")
            return mpd_url

        candidates = self._build_mpd_candidates(mpd_url)
        self.log.info(f" + Probing {len(candidates)} manifest candidate(s) (old CDN)…")

        available = []
        if self.x_dd_token:
            self.session.headers["x-dd-token"] = self.x_dd_token
        try:
            for url in candidates:
                try:
                    r = self.session.head(url, allow_redirects=True, timeout=10)
                    _mn = re.search(r"manifest[^/?]*\.mpd", url)
                    self.log.debug(f"   HEAD {r.status_code} — {_mn.group(0) if _mn else url[-50:]}")
                    if r.status_code == 200:
                        available.append(url)
                except Exception as exc:
                    self.log.debug(f"   HEAD error: {exc}")
        finally:
            self.session.headers.pop("x-dd-token", None)

        if available:
            best = max(available, key=self._score_manifest)
            m = re.search(r"manifest[^/?]*\.mpd", best)
            self.log.info(f" + Selected: {m.group(0) if m else 'manifest.mpd'}")
            return best

        self.log.warning(" + No candidate returned 200 — using original SPAPI URL")
        return mpd_url

    def get_tracks(self, title):
        cached_id = (self._cached_playback or {}).get("assetDetails", {}).get("id")
        if self._cached_playback and cached_id == title.id:
            playback_data = self._cached_playback
        else:
            playback_data = self._playback_request(title.id)
            self._cached_playback = playback_data

        try:
            asset = playback_data.get("assetDetails", {})
            video_raw = asset.get("video_url", {})

            if not isinstance(video_raw, dict):
                raise ValueError(
                    f"Unexpected video_url format — expected dict, got "
                    f"{type(video_raw).__name__}: {str(video_raw)[:100]}"
                )
            if not video_raw.get("mpd"):
                raise ValueError(
                    f"Missing 'mpd' key in video_url. Available keys: {list(video_raw.keys())}"
                )

            key_obj = playback_data.get("keyOsDetails", {})
            if isinstance(key_obj, dict):
                key_os = key_obj.get("sdrm", "")
                nl = key_obj.get("nl", "")
            else:
                key_os = str(key_obj)
                nl = ""

            if key_os.startswith("Nagra_"):
                self.custom_data = key_os
                self.nl_token = nl or key_os.removeprefix("Nagra_")
            else:
                self.custom_data = key_os
                self.nl_token = nl or key_os

            self.log.debug(f" + KeyOs: {key_os}")
            self.log.debug(f" + video_url keys: {list(video_raw.keys())}")

            base_mpd_url = video_raw.get("mpd", "")
            if self._is_v2prime(base_mpd_url):
                self.log.debug(f" + [V2PRIME] assetDetails top-level keys: {list(asset.keys())}")

            self.license_mpd_url = base_mpd_url
            best_url = (
                self._try_v2prime_4k(title.id, base_mpd_url)
                if self._is_v2prime(base_mpd_url)
                else self._select_mpd_url(base_mpd_url)
            )

            if self.x_dd_token:
                self.session.headers["x-dd-token"] = self.x_dd_token
            try:
                tracks = Tracks.from_mpd(url=best_url, session=self.session, source=self.ALIASES[0])
            finally:
                self.session.headers.pop("x-dd-token", None)

            orig_lang = self.original_language or self.language
            for t in tracks.audios:
                if t.language in ("und", None, ""):
                    t.language = orig_lang
            for t in tracks.videos:
                t.language = orig_lang
                if self.range == "HDR10":
                    t.hdr10 = True
                elif self.range == "DV":
                    t.dv = True

            return tracks

        except Exception as exc:
            self.log.error(f"Failed to get tracks: {exc}")
            self.log.debug(traceback.format_exc())
            raise

    def get_chapters(self, title):
        chapters = []
        try:
            for count, ms in enumerate(
                (m for m in (title.service_data or {}).get("milestones", []) if m),
                start=1,
            ):
                total_secs = int(ms.get("start_time", 0)) / 1000
                mins, secs = divmod(total_secs, 60)
                hours, mins = divmod(mins, 60)
                chapters.append(MenuTrack(
                    number=count,
                    title=ms.get("type", "Chapter").title(),
                    timecode=f"{int(hours):02d}:{int(mins):02d}:{int(secs):02d}.000",
                ))
        except Exception as exc:
            self.log.debug(f"Chapter extraction failed: {exc}")
            return chapters

    def certificate(self, **kwargs):
        cert_path = os.path.join(os.path.dirname(__file__), "zee5_certificate.pem")
        if os.path.exists(cert_path):
            try:
                with open(cert_path, "rb") as f:
                    raw_bytes = f.read()
                raw_text = raw_bytes.decode("utf-8", errors="replace").strip()
                if raw_text.startswith("-----BEGIN"):
                    b64 = "".join(re.findall(r"^(?!-).*", raw_text, re.MULTILINE))
                    return base64.b64decode(b64)
                return raw_bytes
            except Exception as exc:
                self.log.warning(f"Could not load certificate file: {exc}")
        return self.license(**kwargs)

    def license(self, challenge, **_):
        customdata = self.custom_data or ""
        nl = self.nl_token or ""
        nagra_cd = customdata if customdata.startswith("Nagra_") else f"Nagra_{nl}"

        if isinstance(challenge, memoryview):
            challenge = bytes(challenge)
        elif isinstance(challenge, str):
            challenge = challenge.encode("utf-8")
        elif not isinstance(challenge, bytes):
            challenge = bytes(challenge)

        r = self.session.post(
            url=self.config["endpoints"]["license"],
            headers={
                "origin": "https://www.zee5.com",
                "referer": "https://www.zee5.com/",
                "Content-Type": "text/xml",
                "SOAPAction": "http://schemas.microsoft.com/DRM/2007/03/protocols/AcquireLicense",
                "customdata": nagra_cd,
                "nl": nl,
            },
            data=challenge,
        )
        self.log.debug(f"License (PlayReady) → HTTP {r.status_code}")
        if r.status_code != 200:
            self.log.error(f"License error: {r.text[:300]}")
        return r.content