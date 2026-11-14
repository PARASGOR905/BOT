from __future__ import annotations

import os
import re
import json
import time
import base64
import hashlib
import requests
import click
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Any

from langcodes import Language
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.objects import Title, Tracks, TextTrack, MenuTrack
from vinetrimmer.config import config
from vinetrimmer.parsers.mpd import parse as parse_mpd


class UPLAY(BaseService):
    """
    UltraPlay (https://www.ultraplay.in)
    DRM: Widevine L3
    Last Updated: 02-01-2026
    """

    ALIASES = ["UPLAY", "uplay", "ULTRAPLAY", "ultraplay"]
    PROVIDER_ID = "ultrahin"
    AES_KEY = "eWJtYsmvjzPZvcOW"
    
    # Caching CDN for catalog metadata to bypass CMS authentication constraints
    CDN_BASE = "https://ultraplaycachedcdn.mobiotics.com/prodv3/"

    # -------------------------------------------------
    # CLI
    # -------------------------------------------------
    @staticmethod
    @click.command(name="UPLAY", short_help="https://www.ultraplay.in")
    @click.argument("title", type=str)
    @click.option("-m", "--movie", is_flag=True, help="Input is a movie URL.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return UPLAY(ctx, **kwargs)

    # -------------------------------------------------
    # INIT
    # -------------------------------------------------
    def __init__(self, ctx, title: str, movie: bool):
        self.title = title
        self.movie = movie

        super().__init__(ctx)

        assert ctx.parent is not None

        self.vcodec = ctx.parent.params["vcodec"]
        self.acodec = ctx.parent.params["acodec"]
        self.range = ctx.parent.params["range_"]

        self.profile_id = ctx.obj.profile

        self.token: str
        self.license_api: Optional[str] = None

    # -------------------------------------------------
    # HELPER: REGISTRATION
    # -------------------------------------------------
    def register_device(self) -> str:
        import random
        device_id = "".join(random.choices("0123456789", k=16))
        payload = {
            "deviceid": device_id,
            "devicetype": "PC",
            "deviceos": "WINDOWS",
            "providerid": self.PROVIDER_ID,
            "timestamp": int(time.time()),
            "appversion": "46.4.0"
        }
        json_str = json.dumps(payload, separators=(',', ':'))
        sha1_hash = hashlib.sha1(json_str.encode('utf-8')).hexdigest()

        key_bytes = self.AES_KEY.encode('utf-8')
        cipher = AES.new(key_bytes, AES.MODE_ECB)
        padded_data = pad(json_str.encode('utf-8'), AES.block_size)
        encrypted_bytes = cipher.encrypt(padded_data)

        b64_str = base64.b64encode(encrypted_bytes).decode('utf-8')
        enc_str = b64_str.replace('+', '-').replace('/', '_').replace('=', ',')

        device_url = f"https://vsms.mobiotics.com/prodv3/subscriberv2/v1/device/register/{self.PROVIDER_ID}"
        res = self.session.post(
            device_url,
            params={"hash": sha1_hash},
            data=enc_str,
            headers={
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                "origin": "https://www.ultraplay.in",
                "referer": "https://www.ultraplay.in/"
            }
        )
        res.raise_for_status()
        return res.json()["success"]

    # -------------------------------------------------
    # HELPER: SLUG RESOLUTION
    # -------------------------------------------------
    def resolve_slug(self):
        slug = self.title
        if "/" in slug:
            slug = slug.rstrip("/").split("/")[-1]

        # If it looks like a 12-char alphanumeric object ID, do not resolve
        if re.fullmatch(r"[a-zA-Z0-9]{12}", slug):
            self.title = slug
            return

        self.log.info(f"Resolving slug '{slug}' to object ID...")
        query = slug.replace("-", " ")
        
        # Search content
        search_url = f"{self.CDN_BASE}subscriber/v1/content/search"
        try:
            res = self.session.get(
                search_url,
                params={"query": query, "page": 1, "size": 20},
                headers={"x-provider-id": self.PROVIDER_ID}
            )
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                self.log.info(f"DEBUG: Request headers sent: {dict(e.response.request.headers)}")
                self.log.info(f"DEBUG: Response body: {e.response.text}")
                self.log.info("Token expired during slug resolution, re-authenticating")
                self.authenticate(force=True)
                try:
                    res = self.session.get(
                        search_url,
                        params={"query": query, "page": 1, "size": 20},
                        headers={"x-provider-id": self.PROVIDER_ID}
                    )
                except requests.exceptions.HTTPError as e_retry:
                    self.log.info(f"DEBUG: Retry Request headers: {dict(e_retry.response.request.headers)}")
                    self.log.info(f"DEBUG: Retry Response body: {e_retry.response.text}")
                    raise e_retry
            else:
                raise

        if res.status_code == 200:
            data = res.json()
            items = data.get("data") or []
            # Look for exact slug match
            for item in items:
                item_title = item.get("title") or ""
                item_default = item.get("defaulttitle") or ""
                if (item_title.lower().replace(" ", "-") == slug.lower() or 
                        item_default.lower().replace(" ", "-") == slug.lower() or
                        item.get("objectid") == slug):
                    self.title = item["objectid"]
                    self.log.info(f"Resolved slug '{slug}' to object ID: {self.title}")
                    return
            
            # Fallback to first item if no exact slug match
            if items:
                self.title = items[0]["objectid"]
                self.log.info(f"No exact slug match, using first search item object ID: {self.title}")
                return
        
        raise ValueError(f"Failed to resolve slug '{slug}' to content ID")

    # -------------------------------------------------
    # TITLES
    # -------------------------------------------------
    def get_titles(self):
        # Ensure token (use cache first)
        if not getattr(self, "token", None):
            if not self.credentials:
                self.authenticate()
            else:
                try:
                    self.token = self.get_token()
                    self.session.headers["X-SESSION"] = self.token["success"]
                    self.session.headers["Authorization"] = f"Bearer {self.token['success']}"
                except Exception:
                    self.authenticate()

        # Resolve slug/URL to object ID if necessary
        self.resolve_slug()

        content_id = self.title

        try:
            res = self.session.get(
                f"{self.CDN_BASE}subscriber/v1/content/{content_id}",
                params={"displaylanguage": "eng"},
                headers={"x-provider-id": self.PROVIDER_ID}
            )
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                self.log.info("Token expired, re-authenticating")
                self.authenticate(force=True)
                res = self.session.get(
                    f"{self.CDN_BASE}subscriber/v1/content/{content_id}",
                    params={"displaylanguage": "eng"},
                    headers={"x-provider-id": self.PROVIDER_ID}
                )
            else:
                raise

        try:
            data = res.json()
        except Exception:
            raise ValueError(f"Failed to load title metadata: {res.text}")

        titles = []

        # MOVIE
        if self.movie:
            titles.append(
                Title(
                    content_id,
                    Title.Types.MOVIE,
                    data.get("title") or data.get("defaulttitle") or "Unknown Title",
                    year=int(data.get("productionyear") or 2025),
                    source=self.ALIASES[0],
                    service_data=data,
                )
            )
            return titles

        # SERIES
        series_id = data["objectid"]
        total_seasons = int(data.get("seasoncount", 0))

        for season in range(1, total_seasons + 1):
            eps = self.session.get(
                f"{self.CDN_BASE}subscriber/v1/content",
                params={
                    "objecttype": "CONTENT",
                    "seriesid": series_id,
                    "seasonnum": season,
                    "pagesize": 50,
                    "page": 1,
                    "displaylanguage": "eng",
                },
                headers={"x-provider-id": self.PROVIDER_ID}
            ).json()

            for ep in eps.get("data", []):
                titles.append(
                    Title(
                        ep["objectid"],
                        Title.Types.TV,
                        ep.get("seriesname") or "Unknown Series",
                        season=int(ep["seasonnum"]),
                        episode=int(ep["episodenum"]),
                        episode_name=ep.get("title") or f"Episode {ep.get('episodenum')}",
                        source=self.ALIASES[0],
                        service_data=ep,
                    )
                )

        return titles

    # -------------------------------------------------
    # TRACKS
    # -------------------------------------------------
    def get_tracks(self, title: Title) -> Tracks:
        data = title.service_data

        dash = next(
            c for c in data["contentdetails"]
            if c.get("streamtype") == "DASH"
        )

        self.packageid = dash["packageid"]
        self.availabilityid = dash["availabilityset"][0]
        self.content_id = data["objectid"]

        # Playback initialization must hit the primary CMS API
        try:
            res = self.session.post(
                f"https://vcms.mobiotics.com/prodv3/subscriber/v1/content/package/{self.content_id}",
                data={
                    "availabilityid": self.availabilityid,
                    "packageid": self.packageid,
                },
                headers={"x-provider-id": self.PROVIDER_ID}
            )
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                try:
                    err_data = e.response.json()
                    if err_data.get("errorcode") == 8862 or "login" in err_data.get("reason", "").lower():
                        self.log.exit(" - This content requires a subscription. Please configure your UPLAY credentials in vinetrimmer.yml.")
                except Exception:
                    pass
            raise

        try:
            payload = res.json()
            self.log.debug(json.dumps(payload, indent=2))
            mpd_url = payload["streamfilename"]
        except Exception:
            raise ValueError(f"Failed to load title manifest: {res.text}")

        if dash.get("drmscheme", [None])[0] == "WIDEVINE":
            self.drm_token = self.get_drm_token()

        mpd_res = self.session.get(mpd_url)
        mpd_res.raise_for_status()

        tracks = parse_mpd(
            url=mpd_url,
            source=self.ALIASES[0],
            session=self.session,
        )

        lang = (
            getattr(title, "language", None)
            or (data.get("contentlanguage") or [None])[0]
            or "und"
        )

        for track in tracks.audios:
            if str(track.language) == "en" and str(lang) != "en":
                track.language = lang

        def is_blank_vtt(text: str) -> bool:
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("WEBVTT"):
                    continue
                if line.startswith("NOTE"):
                    continue
                if "-->" in line:
                    return False
            return True

        valid_subs = []

        for sub in tracks.subtitles:
            try:
                r = self.session.get(
                    sub.url,
                    headers={"Accept": "text/vtt"},
                    timeout=10,
                )

                self.log.debug(f"Subtitle HTTP {r.status_code}")
                self.log.debug(r.text[:200])

                if r.status_code == 200 and is_blank_vtt(r.text):
                    self.log.info(" + Skipping blank subtitle")
                    self.log.info(sub.url)
                    continue

                sub.codec = "vtt"

            except Exception as e:
                self.log.error(f"Subtitle fetch failed: {e}")
                continue

            valid_subs.append(sub)

        tracks.subtitles = valid_subs

        for track in tracks:
            track.needs_proxy = False

        try:
            self.session.delete(
                f"https://vcms.mobiotics.com/prodv3/subscriber/v1/stream/content/{self.content_id}",
                data={"devicetype": "PC"},
                headers={"x-provider-id": self.PROVIDER_ID}
            )
        except Exception:
            pass

        return tracks

    # -------------------------------------------------
    # CHAPTERS
    # -------------------------------------------------
    def get_chapters(self, title: Title) -> List[MenuTrack]:
        skips = (title.service_data or {}).get("skip")
        if not skips:
            return []

        chapters: List[MenuTrack] = []
        idx = 1

        chapters.append(
            MenuTrack(
                number=idx,
                title="Start",
                timecode="00:00:00.000",
            )
        )

        for skip in skips:
            skip_type = skip.get("skiptype")
            if not skip_type:
                continue

            name = skip_type.title()

            start = int(skip.get("start") or 0)
            if start > 0:
                idx += 1
                chapters.append(
                    MenuTrack(
                        number=idx,
                        title=f"{name} Start",
                        timecode=datetime.utcfromtimestamp(start).strftime("%H:%M:%S.%f")[:-3],
                    )
                )

            end = int(skip.get("end") or 0)
            if end > 0:
                idx += 1
                chapters.append(
                    MenuTrack(
                        number=idx,
                        title=f"{name} End",
                        timecode=datetime.utcfromtimestamp(end).strftime("%H:%M:%S.%f")[:-3],
                    )
                )

        return chapters

    # -------------------------------------------------
    # DRM
    # -------------------------------------------------
    def certificate(self, **_):
        return "CAUSxwUKwQIIAxIQFwW5F8wSBIaLBjM6L3cqjBiCtIKSBSKOAjCCAQoCggEBAJntWzsyfateJO/DtiqVtZhSCtW8yzdQPgZFuBTYdrjfQFEEQa2M462xG7iMTnJaXkqeB5UpHVhYQCOn4a8OOKkSeTkwCGELbxWMh4x+Ib/7/up34QGeHleB6KRfRiY9FOYOgFioYHrc4E+shFexN6jWfM3rM3BdmDoh+07svUoQykdJDKR+ql1DghjduvHK3jOS8T1v+2RC/THhv0CwxgTRxLpMlSCkv5fuvWCSmvzu9Vu69WTi0Ods18Vcc6CCuZYSC4NZ7c4kcHCCaA1vZ8bYLErF8xNEkKdO7DevSy8BDFnoKEPiWC8La59dsPxebt9k+9MItHEbzxJQAZyfWgkCAwEAAToUbGljZW5zZS53aWRldmluZS5jb20SgAOuNHMUtag1KX8nE4j7e7jLUnfSSYI83dHaMLkzOVEes8y96gS5RLknwSE0bv296snUE5F+bsF2oQQ4RgpQO8GVK5uk5M4PxL/CCpgIqq9L/NGcHc/N9XTMrCjRtBBBbPneiAQwHL2zNMr80NQJeEI6ZC5UYT3wr8+WykqSSdhV5Cs6cD7xdn9qm9Nta/gr52u/DLpP3lnSq8x2/rZCR7hcQx+8pSJmthn8NpeVQ/ypy727+voOGlXnVaPHvOZV+WRvWCq5z3CqCLl5+Gf2Ogsrf9s2LFvE7NVV2FvKqcWTw4PIV9Sdqrd+QLeFHd/SSZiAjjWyWOddeOrAyhb3BHMEwg2T7eTo/xxvF+YkPj89qPwXCYcOxF+6gjomPwzvofcJOxkJkoMmMzcFBDopvab5tDQsyN9UPLGhGC98X/8z8QSQ+spbJTYLdgFenFoGq47gLwDS6NWYYQSqzE3Udf2W7pzk4ybyG4PHBYV3s4cyzdq8amvtE/sNSdOKReuHpfQ="

    def license(self, *, challenge, **_: Any) -> str:
        if isinstance(challenge, str):
            chal = challenge
        else:
            chal = base64.b64encode(challenge).decode("utf-8")

        json_data = {
            "payload": chal,
            "contentid": self.content_id,
            "providerid": self.PROVIDER_ID,
            "timestamp": int(time.time()),
            "drmscheme": "WIDEVINE",
            "customdata": {
                "packageid": self.packageid,
                "drmtoken": self.drm_token,
            },
        }

        res = self.session.post(
            "https://vdrm.mobiotics.com/prod/proxy/v1/license/widevine",
            json=json_data,
            headers={
                "authority": "vdrm.mobiotics.com",
                "accept": "*/*",
                "content-type": "application/json",
                "dnt": "1",
                "origin": "https://www.ultraplay.in",
                "referer": "https://www.ultraplay.in/",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/138.0.0.0 Safari/537.36"
                ),
                "X-SESSION": self.token["success"],
            },
        )

        data = res.json()
        return data["body"]

    # -------------------------------------------------
    # AUTH
    # -------------------------------------------------
    def authenticate(self, force: bool = False, **_):
        self.session.headers.update({
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "origin": "https://www.ultraplay.in" if self.PROVIDER_ID == "ultrahin" else "https://www.ultrajhakaas.com",
            "referer": "https://www.ultraplay.in/" if self.PROVIDER_ID == "ultrahin" else "https://www.ultrajhakaas.com/",
        })

        if not self.credentials:
            self.log.info("No credentials provided, using guest authentication")
            token_cache_path = self.get_cache("token_guest.json")
            guest_token = None
            if not force and os.path.isfile(token_cache_path):
                try:
                    with open(token_cache_path, encoding="utf-8") as fd:
                        cached_data = json.load(fd)
                    guest_token = cached_data.get("token")
                    if guest_token:
                        self.log.info("Using cached guest token")
                except Exception as e:
                    self.log.warning(f"Failed to read cached guest token: {e}")

            if not guest_token:
                self.log.info("Registering new guest device token...")
                guest_token = self.register_device()
                try:
                    os.makedirs(os.path.dirname(token_cache_path), exist_ok=True)
                    with open(token_cache_path, "w", encoding="utf-8") as fd:
                        json.dump({"token": guest_token, "timestamp": time.time()}, fd)
                except Exception as e:
                    self.log.warning(f"Failed to cache guest token: {e}")

            self.token = {"success": guest_token}
            self.session.headers.pop("X-SESSION", None)
            self.session.headers["Authorization"] = f"Bearer {guest_token}"
            return

        self.token = self.get_token()
        self.session.headers["X-SESSION"] = self.token["success"]
        self.session.headers["Authorization"] = f"Bearer {self.token['success']}"

        self.profile_id = self.get_profile_id()
        self.activate_profile()

    # -------------------------------------------------
    # TOKEN / PROFILE
    # -------------------------------------------------
    def get_token(self) -> dict:
        profile_key = self.profile_id or "default"
        token_cache_path = self.get_cache(
            f"token_{profile_key}.json"
        )

        if os.path.isfile(token_cache_path):
            try:
                with open(token_cache_path, encoding="utf-8") as fd:
                    token = json.load(fd)

                if token.get("refreshtoken"):
                    self.log.info("Refreshing cached UltraPlay token")
                    token = self.refresh(token["refreshtoken"])
                    self.save_token(token, token_cache_path)
                else:
                    self.log.info("Using cached UltraPlay token")

                return token

            except Exception as e:
                self.log.warning(f"Cached token invalid, re-login required: {e}")

        self.log.info("Performing fresh UltraPlay login")
        token = self.login()

        self.save_token(token, token_cache_path)
        return token

    def save_token(self, token: dict, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fd:
            json.dump(token, fd)

    def get_profile_id(self) -> str:
        return self.session.get(
            "https://vsms.mobiotics.com/prodv3/subscriberv2/v1/subscriber"
        ).json()["profileid"]

    def activate_profile(self):
        self.session.put(
            f"https://vsms.mobiotics.com/prodv3/subscriberv2/v1/profile/{self.profile_id}"
        )

    def get_drm_token(self) -> str:
        self.session.post(
            f"https://vcms.mobiotics.com/prodv3/subscriber/v1/stream/content/{self.content_id}",
            data={"devicetype": "PC"},
            headers={"x-provider-id": self.PROVIDER_ID}
        )

        res = self.session.post(
            "https://vcms.mobiotics.com/prodv3/subscriber/v1/content/drmtoken",
            data={
                "contentid": self.content_id,
                "packageid": self.packageid,
                "availabilityid": self.availabilityid,
                "drmscheme": "WIDEVINE",
                "seclevel": "SW",
            },
            headers={"x-provider-id": self.PROVIDER_ID}
        ).json()

        return res["success"]

    def refresh(self, refresh_token: str) -> dict:
        res = self.session.get(
            "https://vsms.mobiotics.com/prodv3/subscriberv2/v1/refreshtoken",
            headers={
                "authorization": f"Bearer {refresh_token}",
                "origin": "https://www.ultraplay.in",
                "referer": "https://www.ultraplay.in/",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            },
        )

        if res.status_code == 403:
            self.log.exit("UltraPlay refresh blocked (403)")

        return res.json()

    def login(self) -> dict:
        if not self.credentials:
            self.log.exit("No credentials provided")

        device_token = self.register_device()

        login = self.session.get(
            "https://vsms.mobiotics.com/prodv3/subscriberv2/v1/login",
            params={
                "email": self.credentials.username,
                "password": self.credentials.password,
                "devicetype": "PC",
                "deviceos": "WINDOWS",
                "country": "IN",
            },
            headers={
                "authorization": f"Bearer {device_token}",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            },
        )

        if login.status_code != 200:
            self.log.exit(f"Login failed: {login.text}")

        return login.json()

    # -------------------------------------------------
    # HELPERS
    # -------------------------------------------------
    def _norm_lang(self, lang) -> Language:
        if isinstance(lang, list) and lang:
            return Language.get(lang[0])
        if isinstance(lang, str):
            return Language.get(lang)
        return Language.get("und")

    def _ts(self, sec: int) -> str:
        return datetime.utcfromtimestamp(int(sec)).strftime("%H:%M:%S.%f")[:-3]

    def _is_blank_sub(self, url: str) -> bool:
        try:
            return requests.get(url, timeout=5).text.strip() == "WEBVTT"
        except Exception:
            return False
