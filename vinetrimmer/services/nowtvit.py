from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
from urllib.parse import urlparse

import click
from langcodes import Language
from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils.widevine.device import LocalDevice


class NowTVIT(BaseService):
    """
    Service code for Now TV Italia (https://www.nowtv.it/).

    \b
    Authorization: Cookies
    Security: UHD@L1 (PlayReady), FHD@L3 (Widevine)

    Requires an IT IP (use --proxy it)
    """

    ALIASES = ["NOWTV", "nowtvit", "NowTVIT"]

    TITLE_RE = r"https?://(?:www\.)?nowtv\.it/watch(?P<id>/[a-zA-Z0-9_/-]+)"

    @staticmethod
    @click.command(name="NowTVIT", short_help="https://nowtv.it", help=__doc__)
    @click.argument("title", type=str)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a movie.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return NowTVIT(ctx, **kwargs)

    def __init__(self, ctx, title, movie):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self.movie = movie
        self.cdm = ctx.obj.cdm
        self.license_api = None
        self.vcodec = ctx.parent.params.get("vcodec")
        self.vrange = ctx.parent.params.get("range_")
        
        self.skyCEsidismesso01 = None
        self.configure()

    def configure(self):
        import curl_cffi.requests
        new_session = curl_cffi.requests.Session(impersonate="chrome")
        new_session.headers.update(self.session.headers)
        new_session.cookies.update(self.session.cookies.get_dict())
        if "all" in self.session.proxies:
            proxy = self.session.proxies["all"]
            new_session.proxies = {"http": proxy, "https": proxy}
        elif self.session.proxies:
            new_session.proxies = self.session.proxies
        self.session = new_session


        
        if not self.skyCEsidismesso01:
            self.skyCEsidismesso01 = self.session.cookies.get("skyCEsidismesso01")

    def get_titles(self):
        old_cookies = self.session.cookies.get_dict()
        self.session.cookies.clear()
        try:
            res = self.session.get(
                url="https://eu.api.atom.nowtv.com/adapter-calypso/v3/query/node",
                params={
                    "slug": self.title,
                    "represent": "(items(items),recs[take=8],collections(items(items[take=8])),trailers)",
                    "features": "upcoming",
                    "contentSegments": "ENTERTAINMENT,HAYU,KIDS,MOVIES,NEWS,SHORTFORM,SPORTS,SPORTS_CORE,SPORTS_ESSENTIALS,SPORTS_EVENTS,SPORTS_EVENTS_EXCLUSIVE,SPORTS_EXTRA,SPORTS_EXTRA_EXCLUSIVE,SSN,HBOMAX,CINEMA,PEACOCK",
                },
                headers={
                    "Accept": "*/*",
                    "X-SkyOTT-Device": self.config["client"]["device"],
                    "X-SkyOTT-Platform": self.config["client"]["platform"],
                    "X-SkyOTT-Proposition": "NOWOTT",
                    "X-SkyOTT-Provider": self.config["client"]["provider"],
                    "X-SkyOTT-Territory": self.config["client"]["territory"],
                },
            ).json()
        finally:
            self.session.cookies.update(old_cookies)

        if "attributes" not in res:
            error_msg = res.get("message") or res.get("description") or f"Title not found on NowTV IT: {self.title}"
            self.log.exit(f" - {error_msg}")
            return []

        has_items = "items" in res.get("relationships", {})
        if self.movie or not has_items:
            is_tv = "seasonNumber" in res.get("attributes", {}) or "episodeNumber" in res.get("attributes", {})
            return [Title(
                id_=self.title,
                type_=Title.Types.TV if is_tv else Title.Types.MOVIE,
                name=res["attributes"]["title"],
                year=res["attributes"].get("year"),
                season=res.get("attributes", {}).get("seasonNumber", 0),
                episode=res.get("attributes", {}).get("episodeNumber", 0),
                episode_name=res["attributes"].get("title") if is_tv else None,
                original_lang="ita",
                source=self.ALIASES[0],
                service_data=res
            )]
        else:
            raw_items = res.get("relationships", {}).get("items", {}).get("data", [])
            episodes = []
            for item in raw_items:
                # Check if item has nested sub-items (e.g. season -> episodes)
                sub_items = item.get("relationships", {}).get("items", {}).get("data", [])
                if sub_items:
                    episodes.extend(sub_items)
                else:
                    episodes.append(item)

            return [Title(
                id_=self.title,
                type_=Title.Types.TV,
                name=res["attributes"]["title"],
                season=episode["attributes"].get("seasonNumber", 0),
                episode=episode["attributes"].get("episodeNumber", 0),
                episode_name=episode["attributes"].get("title"),
                original_lang="ita",
                source=self.ALIASES[0],
                service_data=episode
            ) for episode in episodes]

    def get_tracks(self, title):
        supported_colour_spaces = ["SDR"]
        if self.vrange == "HDR10":
            self.log.info("Switched dynamic range to HDR10")
            supported_colour_spaces = ["HDR10"]
        if self.vrange == "DV":
            self.log.info("Switched dynamic range to DV")
            supported_colour_spaces = ["DV"]

        formats = title.service_data["attributes"].get("formats", {})
        self.log.info(f"Formats available: {formats}")
        if "UHD" in formats and "contentId" in formats["UHD"]:
            content_id = formats["UHD"]["contentId"]
        elif "HD" in formats and "contentId" in formats["HD"]:
            content_id = formats["HD"]["contentId"]
        elif "SD" in formats and "contentId" in formats["SD"]:
            content_id = formats["SD"]["contentId"]
        else:
            content_id = title.service_data["attributes"].get("providerVariantId")
        variant_id = title.service_data["attributes"].get("providerVariantId")
        # FORCE p.sky.com for Android TV payload to allow binary license requests!
        url = self.config["endpoints"]["vod"]
        
        self.log.info(f"Looking up track with content_id: {content_id}, variant_id: {variant_id}")

        headers = {
            "accept": "application/vnd.playvod.v1+json",
            "content-type": "application/vnd.playvod.v1+json",
            "x-skyott-activeterritory": self.config["client"]["territory"],
            "x-skyott-device": "COMPUTER",
            "x-skyott-platform": "PC",
            "x-skyott-proposition": self.config["client"]["proposition"],
            "x-skyott-provider": self.config["client"]["provider"],
            "x-skyott-territory": self.config["client"]["territory"],
            "x-skyott-usertoken": self.get_token(),
        }

        # Set capabilities based on CDM type
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            base_capabilities = [
                {"transport": "DASH", "protection": "PLAYREADY", "vcodec": "H265", "acodec": "EAC3", "container": "TS"},
                {"transport": "DASH", "protection": "PLAYREADY", "vcodec": "H265", "acodec": "EAC3", "container": "ISOBMFF"},
                {"transport": "DASH", "protection": "PLAYREADY", "vcodec": "H264", "acodec": "EAC3", "container": "TS"},
                {"transport": "DASH", "protection": "PLAYREADY", "vcodec": "H264", "acodec": "EAC3", "container": "ISOBMFF"},
                {"transport": "DASH", "protection": "PLAYREADY", "vcodec": "H265", "acodec": "AAC", "container": "TS"},
                {"transport": "DASH", "protection": "PLAYREADY", "vcodec": "H265", "acodec": "AAC", "container": "ISOBMFF"},
                {"transport": "DASH", "protection": "PLAYREADY", "vcodec": "H264", "acodec": "AAC", "container": "TS"},
                {"transport": "DASH", "protection": "PLAYREADY", "vcodec": "H264", "acodec": "AAC", "container": "ISOBMFF"}
            ]
        else:
            base_capabilities = [
                {"transport": "DASH", "protection": "WIDEVINE", "vcodec": "H265", "acodec": "EAC3", "container": "ISOBMFF"},
                {"transport": "DASH", "protection": "WIDEVINE", "vcodec": "H264", "acodec": "EAC3", "container": "ISOBMFF"},
                {"transport": "DASH", "protection": "WIDEVINE", "vcodec": "H265", "acodec": "AAC", "container": "ISOBMFF"},
                {"transport": "DASH", "protection": "WIDEVINE", "vcodec": "H264", "acodec": "AAC", "container": "ISOBMFF"}
            ]
        data = {
            "device": {
                "capabilities": base_capabilities,
                "model": self.config["client"]["model"],
                "maxVideoFormat": "HD",
                "hdcpEnabled": True,
                "robustness": "HW_SECURE_ALL",
                "supportedColourSpaces": ["SDR"],
            },
            "personaParentalControlRating": "7",
            "parentalControlPin": None,
        }

        if content_id and len(content_id) < 20:
            data["contentId"] = content_id
            data["device"]["maxVideoFormat"] = "HD"
        else:
            data["providerVariantId"] = variant_id or content_id

        data_str = json.dumps(data)
        self.log.info(f"VOD Request Payload: {data_str}")
        headers["x-sky-signature"] = self.calculate_signature("POST", url, headers, data_str)

        import curl_cffi.requests
        resp = curl_cffi.requests.post(url, headers=headers, data=data_str, impersonate="chrome")
        self.log.info(f"VOD Response Status: {resp.status_code}")
        self.log.info(f"VOD Response Body: {resp.text[:1000]}")
        response = resp.json()

        if response.get("errorCode"):
            self.log.error(response.get("description"))
            sys.exit(1)

        manifest = response["asset"]["endpoints"][0]["url"]
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            self.license_api = response["protection"].get("playreadyLicenceAcquisitionUrl", response["protection"]["licenceAcquisitionUrl"])
        else:
            self.license_api = response["protection"]["licenceAcquisitionUrl"]
        # Store the refreshed user token from VOD response for use in license requests
        self._usertoken = resp.headers.get("x-skyott-usertoken", "")

        self.log.info(f"DASH: {manifest}")

        tracks = Tracks.from_mpd(
            url=manifest,
            session=self.session,
            source=self.ALIASES[0]
        )

        if supported_colour_spaces == ["HDR10"]:
            for track in tracks.videos:
                track.hdr10 = True
        if supported_colour_spaces == ["DV"]:
            for track in tracks.videos:
                track.dolbyvision = True

        for track in tracks:
            track.needs_proxy = True

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, challenge, **_):
        return None

    def license(self, challenge, **_):
        old_cookies = self.session.cookies.get_dict()
        self.session.cookies.clear()
        try:
            if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
                res = self.session.post(
                    url=self.license_api,
                    data=challenge
                ).content
            else:
                import base64 as _b64
                if isinstance(challenge, bytes):
                    b64_challenge = _b64.b64encode(challenge).decode("utf-8")
                else:
                    b64_challenge = challenge
                
                req_headers = {
                    "Accept": "*/*",
                    "Content-Type": "text/plain"
                }
                # NO SIGNATURE AT ALL
                
                res = self.session.post(
                    url=self.license_api,
                    headers=req_headers,
                    data=b64_challenge
                )
                if not res.ok:
                    self.log.error(f"Widevine License Error {res.status_code}: {res.text}")
                    res.raise_for_status()
                    
                res_text = res.text
                if not res_text.startswith("{") and not res_text.startswith("<"):
                    try:
                        res = _b64.b64decode(res_text)
                    except Exception:
                        res = res.content
                else:
                    res = res.content
        finally:
            self.session.cookies.update(old_cookies)
        return res

    @staticmethod
    def calculate_sky_header(headers: dict) -> str:
        text_headers = ""
        for key in sorted(headers.keys()):
            if key.lower().startswith("x-skyott"):
                text_headers += key + ": " + headers[key] + "\n"
        return hashlib.md5(text_headers.encode()).hexdigest()

    def calculate_signature(self, method: str, url: str, headers: dict, payload, client_sdk_override: str = None) -> str:
        # Generate timestamp once so hash and signature string always match
        timestamp = int(time.time())
        payload_bytes = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        to_hash = (
            "{method}\n{path}\n{response_code}\n{app_id}\n{version}\n{headers_md5}\n"
            "{timestamp}\n{payload_md5}\n"
        ).format(
            method=method,
            path=urlparse(url).path if url.startswith("http") else url,
            response_code="",
            app_id=client_sdk_override or self.config["client"]["client_sdk"],
            version="1.0",
            headers_md5=self.calculate_sky_header(headers),
            timestamp=timestamp,
            payload_md5=hashlib.md5(payload_bytes).hexdigest(),
        )

        signature_key = bytes(self.config["security"]["signature_hmac_key_v4"], "utf-8")
        hashed = hmac.new(signature_key, to_hash.encode("utf8"), hashlib.sha1).digest()
        signature_hmac = base64.b64encode(hashed).decode("utf8")

        return self.config["security"]["signature_format"].format(
            client=self.config["client"]["client_sdk"],
            signature=signature_hmac,
            timestamp=timestamp,
        )

    def get_persona(self):
        persona_url = 'https://persona-store.sky.com/persona-store/personas'
        headers = {
            'accept': 'application/vnd.persona.v1+json',
            'x-skyid-token': self.skyCEsidismesso01 or "",
            'x-skyott-device': self.config["client"]["device"],
            'x-skyott-platform': self.config["client"]["platform"],
            'x-skyott-proposition': self.config["client"]["proposition"],
            'x-skyott-provider': self.config["client"]["provider"],
            'x-skyott-territory': self.config["client"]["territory"],
            'x-skyott-tokentype': 'SSO',
        }
        
        # Use a clean request to bypass Tomcat 400 Bad Request from global headers
        import curl_cffi.requests
        res = curl_cffi.requests.get(persona_url, headers=headers, impersonate="chrome")
        
        if res.status_code != 200:
            self.log.error(f"Failed to fetch personaId! Server returned {res.status_code}: {res.text}")
            self.log.error("This usually means your skyCEsidismesso01 cookie is expired or invalid. Please login again and export fresh cookies!")
            sys.exit(1)
            
        data = res.json()
        if 'personas' not in data:
            self.log.error(f"Failed to fetch personaId from valid response: {data}")
            sys.exit(1)
        return data['personas'][0]['personaId']

    def get_token(self) -> str:
        url = self.config["endpoints"]["tokens"]

        headers = {
            "accept": "application/vnd.tokens.v1+json",
            "content-type": "application/vnd.tokens.v1+json",
            "x-skyott-device": self.config["client"]["device"],
            "x-skyott-platform": self.config["client"]["platform"],
            "x-skyott-proposition": "NOWOTT",
            "x-skyott-provider": self.config["client"]["provider"],
            "x-skyott-territory": self.config["client"]["territory"],
        }

        data = {
            "auth": {
                "authScheme": self.config["client"]["auth_scheme"],
                "authToken": self.skyCEsidismesso01,
                "authIssuer": self.config["client"]["auth_issuer"],
                "personaId": self.get_persona(),
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

        import curl_cffi.requests
        resp = curl_cffi.requests.post(url, headers=headers, data=data, impersonate="chrome")
        try:
            response = resp.json()
        except json.JSONDecodeError:
            self.log.error(f"Failed to decode token response. Status: {resp.status_code}")
            self.log.error(f"Response: {resp.text[:500]}")
            sys.exit(1)

        if response.get("message"):
            self.log.error(f"{response['message']}")
            sys.exit(1)
        elif response.get("errorCode"):
            self.log.error(f"{response.get('errorCode')}: {response.get('description', '')}")
            sys.exit(1)

        return response["userToken"]
