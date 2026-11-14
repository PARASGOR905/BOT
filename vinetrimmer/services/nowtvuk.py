# Fixed getting movie data

from __future__ import annotations

import os
import base64
import hashlib
import hmac
import json
import sys
import time
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import Any, Optional, Union
from urllib.parse import urlparse
from vinetrimmer.utils.widevine.device import LocalDevice
import click
import requests
try:
    from curl_cffi import requests as cf_requests
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False
from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService
from langcodes import Language

class NowTVUK(BaseService):
    """
    \b
    Service code for Now TV's streaming service (https://nowtv.com)
    Only UK is currently supported

    \b
    Authorization: Cookies
    Robustness:
      Widevine:
        L1: 2160p, 1080p, DDP5.1
        L3: 720p, AAC2.0

    \b
    Tips:
        - Input should be the full NOW TV URL or slug, e.g.:
            https://www.nowtv.com/watch/asset/lanterns/iYEQYZaB7junwAjsp7TWf5
            /house-of-the-dragon/iYEQZ2rcf32XRKvQ5gm2Aq
    """

    ALIASES = ["NOW", "nowtvuk"]
    CDM_SERVICE_NAME = "nowtvuk"
    GEOFENCE = ["gb"]

    TITLE_RE = r"https?://(?:www\.)?nowtv\.com/watch/asset(?:/movies|/tv)?(?P<id>/[a-z0-9-]+/[a-zA-Z0-9]+)"

    @staticmethod
    @click.command(name="NowTVUK", short_help="https://nowtv.com", help=__doc__)
    @click.argument("title", type=str)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a movie.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return NowTVUK(ctx, **kwargs)

    def __init__(self, ctx, title, movie):
        self.parse_title(ctx, title)
        self.movie = movie
        super().__init__(ctx)
        self.cdm = ctx.obj.cdm
        self.license_api = None
        self.skyCEsidismesso01 = None
        self.userToken = None
        self.vcodec = ctx.parent.params["vcodec"]
        self.vrange = ctx.parent.params["range_"]

        self.configure()

    def configure(self):
        self.skyCEsidismesso01 = (
            self.session.cookies.get("skyCEsidmesso01")
            or self.session.cookies.get("skyCEsidexsso01")
        )
        self.userToken = self.get_token()
        if not self.userToken:
            self.log.exit(
                " - Failed to obtain a userToken. "
                "Check your Now TV cookies (skyCEsidmesso01 / skyCEsidexsso01)."
            )
        self.log.debug(f" + userToken obtained: {self.userToken[:30]}...")
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Referer": "https://www.nowtv.com/",
            "Origin": "https://www.nowtv.com",
            "sec-ch-ua": '"Chromium";v="136", "Not=A?Brand";v="8", "Google Chrome";v="136"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        })

    def get_titles(self):
        clean_slug = self.title.lstrip("/")
        from vinetrimmer.config import directories
        cache_dir = os.path.join(str(directories.cache), "NowTVUK")
        os.makedirs(cache_dir, exist_ok=True)
        cache_key = hashlib.md5(clean_slug.encode()).hexdigest()
        cache_file = os.path.join(cache_dir, f"{cache_key}.json")

        res = None
        if os.path.isfile(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    res = json.load(f)
                if res and "attributes" in res:
                    self.log.info(" + Using cached title metadata (skipping Node API)")
            except Exception:
                res = None

        if not res:
            slug_candidates = [f"/{clean_slug}", clean_slug]

            # Impersonation profiles to try in order (newest → less flagged first)
            _IMPERSONATIONS = ["chrome136", "chrome131", "chrome124", "chrome120"]

            def _is_akamai_blocked(resp):
                """Return True if Akamai WAF blocked the request."""
                if resp is None:
                    return False
                if resp.status_code == 403:
                    return True
                # Akamai sometimes returns 200 with an HTML challenge page
                ct = resp.headers.get("Content-Type", "")
                if "text/html" in ct and ("Access Denied" in resp.text or "akamai" in resp.text.lower()):
                    return True
                return False

            # Atom Calypso node API is a public catalog endpoint.
            node_headers = {
                "Accept": "*/*",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                "X-SkyOTT-Device": self.config["client"]["device"],
                "X-SkyOTT-Platform": self.config["client"]["platform"],
                "X-SkyOTT-Proposition": self.config["client"]["proposition"],
                "X-SkyOTT-Provider": self.config["client"]["provider"],
                "X-SkyOTT-Territory": self.config["client"]["territory"],
            }

            node_resp = None
            for candidate_slug in slug_candidates:
                self.log.debug(f" - Trying Node API slug: {candidate_slug!r}")
                node_params = {
                    "slug": candidate_slug,
                    "represent": "(items(items),recs[take=8],collections(items(items[take=8])),trailers)",
                    "features": "upcoming",
                    "contentSegments": (
                        "ENTERTAINMENT,HAYU,KIDS,MOVIES,NEWS,SHORTFORM,SPORTS,"
                        "SPORTS_CORE,SPORTS_ESSENTIALS,SPORTS_EVENTS,"
                        "SPORTS_EVENTS_EXCLUSIVE,SPORTS_EXTRA,SPORTS_EXTRA_EXCLUSIVE,"
                        "SSN,HBOMAX,CINEMA,PEACOCK"
                    ),
                }

                if _CURL_CFFI_AVAILABLE:
                    for impersonate in _IMPERSONATIONS:
                        self.log.debug(f" + Trying curl_cffi impersonation: {impersonate}")
                        try:
                            node_resp = cf_requests.get(
                                url=self.config["endpoints"]["node"],
                                params=node_params,
                                headers=node_headers,
                                proxies=self.session.proxies if self.session.proxies else None,
                                impersonate=impersonate,
                                timeout=10,
                            )
                        except Exception as _ex:
                            self.log.debug(f" - {impersonate} failed: {_ex}")
                            node_resp = None
                            continue
                        if not _is_akamai_blocked(node_resp):
                            break
                        self.log.debug(f" - Akamai blocked with {impersonate}, retrying...")
                        node_resp = None
                else:
                    saved_hooks = self.session.hooks.copy()
                    self.session.hooks = {}
                    try:
                        node_resp = self.session.get(
                            url=self.config["endpoints"]["node"],
                            params=node_params,
                            headers=node_headers,
                        )
                    finally:
                        self.session.hooks = saved_hooks

                if node_resp and node_resp.status_code == 200 and not _is_akamai_blocked(node_resp):
                    break

            # Fallback with X-SkyOTT-UserToken if unauthenticated request returned non-200
            if (not node_resp or node_resp.status_code != 200 or _is_akamai_blocked(node_resp)) and self.userToken:
                self.log.debug(" - Retrying Node API with X-SkyOTT-UserToken...")
                headers_with_token = dict(node_headers)
                headers_with_token["X-SkyOTT-UserToken"] = self.userToken
                for candidate_slug in slug_candidates:
                    node_params["slug"] = candidate_slug
                    if _CURL_CFFI_AVAILABLE:
                        for impersonate in _IMPERSONATIONS:
                            try:
                                node_resp = cf_requests.get(
                                    url=self.config["endpoints"]["node"],
                                    params=node_params,
                                    headers=headers_with_token,
                                    proxies=self.session.proxies if self.session.proxies else None,
                                    impersonate=impersonate,
                                    timeout=30,
                                )
                            except Exception:
                                node_resp = None
                                continue
                            if not _is_akamai_blocked(node_resp):
                                break
                            self.log.debug(f" - Akamai blocked with {impersonate} (token retry), retrying...")
                            node_resp = None
                    if node_resp and node_resp.status_code == 200 and not _is_akamai_blocked(node_resp):
                        break

            # Final Akamai check before parsing
            if _is_akamai_blocked(node_resp):
                self.log.exit(
                    f" - Akamai WAF blocked request to NowTV Node API (HTTP {getattr(node_resp, 'status_code', '?')}).\n"
                    "   All Chrome impersonation profiles were tried and blocked.\n"
                    "   Please switch your VPN/proxy to a different UK server and retry."
                )
                return []

            if not node_resp or not node_resp.content:
                status = getattr(node_resp, "status_code", "No Response")
                self.log.exit(
                    f" - NowTV Node API is unreachable (HTTP {status}).\n"
                    "   This usually means your VPN/proxy IP (datacenter ASN) is blocked at the network level.\n"
                    "   NowTV requires a RESIDENTIAL UK IP — datacenter/VPN IPs are dropped entirely.\n"
                    "   Fix: configure a residential UK proxy in vinetrimmer.yml under 'proxies: gb:'\n"
                    "   then rerun with --force-proxy to bypass the geo-skip logic."
                )
                return []

            try:
                res = node_resp.json()
            except Exception:
                self.log.exit(
                    f" - Node API returned non-JSON (HTTP {node_resp.status_code}): {node_resp.text[:200]}"
                )
                return []

            if node_resp.status_code != 200:
                self.log.exit(
                    f" - Node API HTTP {node_resp.status_code}: "
                    f"{res.get('message') or res.get('description') or node_resp.text[:300]}"
                )
                return []

            # Save to cache
            if res and "attributes" in res:
                try:
                    with open(cache_file, "w", encoding="utf-8") as f:
                        json.dump(res, f)
                except Exception:
                    pass

        if "attributes" not in res:
            error_msg = (
                res.get("message")
                or res.get("description")
                or f"Title not found on NowTV UK: {self.title}"
            )
            self.log.exit(f" - {error_msg}")
            return []

        is_movie = self.movie or "items" not in res.get("relationships", {})

        if is_movie:
            return Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=res["attributes"]["title"],
                year=res["attributes"].get("year"),
                original_lang="eng",  # TODO: Don't assume
                source=self.ALIASES[0],
                service_data=res
            )
        else:
            titles = [
                episode
                for season in res["relationships"]["items"]["data"]
                for episode in season.get("relationships", {}).get("items", {}).get("data", [])
            ]
            return [Title(
                id_=self.title,
                type_=Title.Types.TV,
                name=res["attributes"]["title"],
                season=episode["attributes"].get("seasonNumber", 0),
                episode=episode["attributes"].get("episodeNumber", 0),
                episode_name=episode["attributes"].get("title"),
                original_lang="eng",  # TODO: Don't assume
                source=self.ALIASES[0],
                service_data=episode
            ) for episode in titles]


    def get_tracks(self, title):
        supported_colour_spaces = ["SDR"]

        if self.vrange == "HDR10":
            self.log.info("Switched dynamic range to HDR10")
            supported_colour_spaces = ["HDR10"]
        elif self.vrange == "DV":
            self.log.info("Switched dynamic range to DV")
            supported_colour_spaces = ["DV"]

        formats = title.service_data["attributes"].get("formats", {})
        if "UHD" in formats and "contentId" in formats["UHD"]:
            content_id = formats["UHD"]["contentId"]
        elif "HD" in formats and "contentId" in formats["HD"]:
            content_id = formats["HD"]["contentId"]
        elif "SD" in formats and "contentId" in formats["SD"]:
            content_id = formats["SD"]["contentId"]
        else:
            content_id = title.service_data["attributes"].get("providerVariantId")

        # providerVariantId is required by Sky VOD API to locate the catalogue title
        variant_id = title.service_data["attributes"].get("providerVariantId")
        if not content_id:
            content_id = variant_id

        # FIX: guard against both IDs being absent — avoids a silent null body from the VOD API
        if not content_id:
            self.log.exit(" - Could not find contentId or providerVariantId for this title.")
            return None

        url = self.config["endpoints"]["vod"]

        # FIX: reuse already-fetched self.userToken instead of calling get_token() again
        # (extra round-trip + token could change between calls)
        headers = {
            "accept": "application/vnd.playvod.v1+json",
            "content-type": "application/vnd.playvod.v1+json",
            "x-skyott-activeterritory": self.config["client"]["territory"],
            "x-skyott-device": self.config["client"]["device"],
            "x-skyott-platform": self.config["client"]["platform"],
            "x-skyott-proposition": self.config["client"]["proposition"],
            "x-skyott-provider": self.config["client"]["provider"],
            "x-skyott-territory": self.config["client"]["territory"],
            "x-skyott-usertoken": self.userToken,
        }

        protection = "PLAYREADY" if self.cdm.device.type == LocalDevice.Types.PLAYREADY else "WIDEVINE"

        # Build capabilities based on requested vcodec:
        # Default / H265 → advertise H265 first with H264 fallback
        # H264 → advertise H264 only
        if (self.vcodec or "").upper() == "H264":
            codecs = ["H264"]
        else:
            codecs = ["H265", "H264"]

        capabilities = []
        for vcodec in codecs:
            for acodec in ["EAC3", "AAC"]:
                for container in ["ISOBMFF", "MP4"]:
                    capabilities.append({
                        "transport": "DASH",
                        "protection": protection,
                        "vcodec": vcodec,
                        "acodec": acodec,
                        "container": container,
                    })

        is_hw = (
            getattr(self.cdm.device, "security_level", 3) == 1
            or getattr(self.cdm.device, "security_level", 0) >= 2000
            or getattr(self.cdm.device, "type", None) == LocalDevice.Types.PLAYREADY
        )
        robustness = "HW_SECURE_ALL" if is_hw else "SW_SECURE_DECODE"
        max_format = "UHD" if "UHD" in formats else "HD"

        data = {
            "device": {
                "capabilities": capabilities,
                "model": self.config["client"]["model"],
                "maxVideoFormat": max_format,
                "hdcpEnabled": True,
                "robustness": robustness,
                "supportedColourSpaces": ["DV", "HDR10", "SDR"],
            },
            "contentId": content_id,
            "providerVariantId": variant_id,
            "personaParentalControlRating": 9,
            # FIX: must be JSON null (None), not the string "null"
            "parentalControlPin": None,
        }

        data = json.dumps(data)
        headers["x-sky-signature"] = self.calculate_signature("POST", url, headers, data)

        # Do NOT send session cookies with the VOD request — cookies are ~9KB and cause
        # HTTP 431 "Request Header Fields Too Large". Sky VOD API authenticates via
        # x-skyott-usertoken and x-sky-signature headers, not cookies.
        if _CURL_CFFI_AVAILABLE:
            res_vod = None
            for impersonate in ["chrome136", "chrome131", "chrome124"]:
                try:
                    res_vod = cf_requests.post(
                        url=url,
                        headers=headers,
                        data=data,
                        proxies=self.session.proxies if self.session.proxies else None,
                        impersonate=impersonate,
                        timeout=30,
                    )
                    if res_vod and res_vod.status_code not in (403, 429):
                        break
                except Exception:
                    continue
            if res_vod is None:
                raise RuntimeError("All curl_cffi impersonation attempts failed for VOD API")
        else:
            saved_hooks = self.session.hooks.copy()
            self.session.hooks = {}
            req_session = requests.Session()
            if self.session.proxies:
                req_session.proxies = self.session.proxies
            try:
                res_vod = req_session.post(url, headers=headers, data=data, timeout=30)
            finally:
                self.session.hooks = saved_hooks

        if not res_vod.content:
            self.log.exit(f" - VOD API returned empty response (HTTP {res_vod.status_code}).")
            return None

        try:
            response = res_vod.json()
        except Exception:
            self.log.exit(
                f" - VOD API returned non-JSON (HTTP {res_vod.status_code}): {res_vod.text[:500]}"
            )
            return None

        if res_vod.status_code != 200 or response.get("errorCode"):
            self.log.exit(
                f" - VOD API error ({res_vod.status_code}): "
                f"{response.get('description') or response.get('message') or res_vod.text[:300]}"
            )
            return None

        endpoints = response["asset"].get("endpoints", [])
        self.protection_info = response.get("protection", {})

        # Update userToken if refreshed in VOD response headers
        refreshed_token = res_vod.headers.get("x-skyott-usertoken")
        if refreshed_token:
            self.userToken = refreshed_token

        if not response.get("protection") or not response["protection"].get("licenceAcquisitionUrl"):
            self.log.exit(" - VOD API response missing licenceAcquisitionUrl.")
            return None

        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            self.license_api = response["protection"].get(
                "playreadyLicenceAcquisitionUrl",
                response["protection"].get("licenceAcquisitionUrl")
            )
        else:
            self.license_api = response["protection"].get("licenceAcquisitionUrl")

        # FIX: guard against empty audioTracks list before indexing [0]
        audio_tracks = response["asset"].get("audioTracks", [])
        locale = audio_tracks[0].get("locale", "en-GB") if audio_tracks else "en-GB"

        tracks = None
        for ep in endpoints:
            m_url = ep.get("url")
            if not m_url:
                continue
            try:
                tracks = Tracks.from_mpd(
                    url=m_url,
                    session=self.session,
                    source=self.ALIASES[0]
                )
                break
            except Exception as e:
                self.log.debug(f"Failed to fetch manifest from endpoint {m_url}: {e}")
                continue

        if not tracks:
            if not endpoints:
                self.log.exit(" - VOD API returned no endpoints.")
                return None
            manifest = endpoints[0]["url"]
            tracks = Tracks.from_mpd(
                url=manifest,
                session=self.session,
                source=self.ALIASES[0]
            )

        # Apply HDR flags
        if supported_colour_spaces == ["HDR10"]:
            for track in tracks.videos:
                track.hdr10 = True
        elif supported_colour_spaces == ["DV"]:
            for track in tracks.videos:
                track.dolbyvision = True  # FIX: was misspelled "dolbyvison"

        for track in tracks:
            track.needs_proxy = True

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, challenge=None, **_):
        return None

    def license(self, challenge, **_):
        old_cookies = self.session.cookies.get_dict()
        self.session.cookies.clear()
        try:
            if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
                # First attempt: Clean PlayReady SOAP request without extra Sky headers/cookies
                # (The bootstrap token ?bt=... in license_api already carries DRM authorization)
                pr_headers = {
                    "Accept": "*/*",
                    "Content-Type": "text/xml; charset=utf-8",
                }
                try:
                    res = self.session.post(
                        url=self.license_api,
                        headers=pr_headers,
                        data=challenge,
                    )
                    if res.status_code == 200:
                        return res.content
                except Exception:
                    pass

                # Second attempt: with signature if clean request was rejected
                path = urlparse(self.license_api).path
                lic_headers_base = {
                    "x-skyott-activeterritory": self.config["client"]["territory"],
                    "x-skyott-device": self.config["client"]["device"],
                    "x-skyott-platform": self.config["client"]["platform"],
                    "x-skyott-proposition": self.config["client"]["proposition"],
                    "x-skyott-provider": self.config["client"]["provider"],
                    "x-skyott-territory": self.config["client"]["territory"],
                    "x-skyott-usertoken": self.userToken,
                }
                pr_headers_signed = {
                    "Accept": "*/*",
                    "Content-Type": "text/xml; charset=utf-8",
                    **lic_headers_base,
                }
                pr_headers_signed["X-Sky-Signature"] = self.calculate_signature("POST", path, pr_headers_signed, challenge)
                try:
                    res = self.session.post(
                        url=self.license_api,
                        headers=pr_headers_signed,
                        data=challenge,
                    )
                    res.raise_for_status()
                    return res.content
                except Exception as e:
                    import requests as _req
                    if isinstance(e, _req.HTTPError) and e.response is not None:
                        self.log.error(f"License server {e.response.status_code} body: {e.response.text[:800]}")
                    raise
            else:
                # Widevine attempt 1: base64 text/plain without signature (NowTV standard)
                import base64 as _b64
                b64_challenge = _b64.b64encode(challenge).decode("utf-8") if isinstance(challenge, bytes) else challenge
                try:
                    res = self.session.post(
                        url=self.license_api,
                        headers={"Accept": "*/*", "Content-Type": "text/plain"},
                        data=b64_challenge,
                    )
                    if res.status_code == 200:
                        res_text = res.text
                        if not res_text.startswith("{") and not res_text.startswith("<"):
                            try:
                                return _b64.b64decode(res_text)
                            except Exception:
                                return res.content
                        return res.content
                except Exception:
                    pass

                # Widevine attempt 2: clean binary octet-stream
                try:
                    res = self.session.post(
                        url=self.license_api,
                        headers={"Accept": "*/*", "Content-Type": "application/octet-stream"},
                        data=challenge,
                    )
                    if res.status_code == 200:
                        return res.content
                except Exception:
                    pass

                # Widevine attempt 3: signed headers
                path = urlparse(self.license_api).path
                lic_headers_base = {
                    "x-skyott-activeterritory": self.config["client"]["territory"],
                    "x-skyott-device": self.config["client"]["device"],
                    "x-skyott-platform": self.config["client"]["platform"],
                    "x-skyott-proposition": self.config["client"]["proposition"],
                    "x-skyott-provider": self.config["client"]["provider"],
                    "x-skyott-territory": self.config["client"]["territory"],
                    "x-skyott-usertoken": self.userToken,
                }
                wv_headers = {
                    "Accept": "*/*",
                    "Content-Type": "application/octet-stream",
                    **lic_headers_base,
                }
                client_sdk = self.config["client"]["client_sdk"]
                wv_headers["X-Sky-Signature"] = self.calculate_signature("POST", path, wv_headers, challenge, client_sdk=client_sdk)
                try:
                    res = self.session.post(
                        url=self.license_api,
                        headers=wv_headers,
                        data=challenge,
                    )
                    res.raise_for_status()
                    return res.content
                except Exception as e:
                    import requests as _req
                    if isinstance(e, _req.HTTPError) and e.response is not None:
                        self.log.error(f"License server {e.response.status_code} body: {e.response.text[:800]}")
                    raise
        finally:
            self.session.cookies.update(old_cookies)

    @staticmethod
    def calculate_sky_header(headers: dict) -> str:
        text_headers = ""
        for key in sorted(headers.keys()):
            if key.lower().startswith("x-skyott"):
                text_headers += key + ": " + headers[key] + "\n"
        return hashlib.md5(text_headers.encode()).hexdigest()

    def calculate_signature(self, method: str, url: str, headers: dict, payload: str, client_sdk: str = None) -> str:
        if not client_sdk:
            client_sdk = self.config["client"]["client_sdk"]
        path = "/" + url.split("://", 1)[1].split("/", 1)[1] if "://" in url else url
        timestamp = int(time.time())
        to_hash = (
            "{method}\n{path}\n{response_code}\n{app_id}\n{version}\n{headers_md5}\n"
            "{timestamp}\n{payload_md5}\n"
        ).format(
            method=method,
            path=path,
            response_code="",
            app_id=client_sdk,
            version="1.0",
            headers_md5=self.calculate_sky_header(headers),
            timestamp=timestamp,
            payload_md5=hashlib.md5(payload.encode() if isinstance(payload, str) else payload).hexdigest(),
        )

        signature_key = bytes(self.config["security"]["signature_hmac_key_v4"], "utf-8")
        hashed = hmac.new(signature_key, to_hash.encode("utf8"), hashlib.sha1).digest()
        signature_hmac = base64.b64encode(hashed).decode("utf8")

        return self.config["security"]["signature_format"].format(
            client=client_sdk, signature=signature_hmac, timestamp=timestamp
        )

    def get_token(self) -> str:
        direct_token = (
            self.session.cookies.get("usertoken")
            or self.session.cookies.get("userToken")
            or self.session.cookies.get("x-skyott-usertoken")
        )
        if direct_token:
            self.log.debug("Using authenticated userToken from cookies")
            return direct_token

        url = self.config["endpoints"]["tokens"]

        headers = {
            "accept": "application/vnd.tokens.v1+json",
            "content-type": "application/vnd.tokens.v1+json",
            "x-skyott-device": self.config["client"]["device"],
            "x-skyott-platform": self.config["client"]["platform"],
            "x-skyott-proposition": self.config["client"]["proposition"],
            "x-skyott-provider": self.config["client"]["provider"],
            "x-skyott-territory": self.config["client"]["territory"],
        }

        auth_token = (
            self.session.cookies.get("skyCEsidmesso01")
            or self.session.cookies.get("skyCEsidexsso01")
        )

        # FIX: warn clearly if no auth cookie is present — the token request will fail silently otherwise
        if not auth_token:
            self.log.warning(
                " - No skyCEsidmesso01 / skyCEsidexsso01 cookie found. "
                "Token request will likely fail. Export cookies from a logged-in Now TV session."
            )

        data = {
            "auth": {
                "authScheme": self.config["client"]["auth_scheme"],
                "authToken": auth_token,
                "authIssuer": self.config["client"]["auth_issuer"],
                "provider": self.config["client"]["provider"],
                "providerTerritory": self.config["client"]["territory"],
                "proposition": self.config["client"]["proposition"],
            },
            "device": {
                "type": self.config["client"]["device"],
                "platform": self.config["client"]["platform"],
                "id": self.config["client"]["id"],
                "drmDeviceId": self.config["client"]["drm_device_id"],
            },
        }

        data = json.dumps(data)
        headers["Content-MD5"] = hashlib.md5(data.encode("utf-8")).hexdigest()

        response = self.session.post(url, headers=headers, data=data).json()
        if response.get("message"):
            self.log.error(f"{response['message']}")
            sys.exit(1)

        return response["userToken"]
