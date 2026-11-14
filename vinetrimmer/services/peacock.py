import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime

import click
import requests

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.utils.regex import find


class Peacock(BaseService):
    """
    Service code for NBC's Peacock streaming service (https://peacocktv.com).

    \b
    Authorization: Cookies
    Security: UHD@-- FHD@L3, doesn't care about releases.

    \b
    Tips: - The library of contents can be viewed without logging in at https://www.peacocktv.com/stream/tv
            See the footer for links to movies, news, etc. A US IP is required to view.
    """

    ALIASES = ["PCOK", "peacock"]
    # GEOFENCE = ["us"]
    TITLE_RE = [
        r"(?:https?://(?:www\.)?peacocktv\.com/watch-online/)?(?P<id>movies/[a-z0-9/.\-]+/[a-f0-9\-]+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch-online/)?(?P<id>tv/[a-z0-9/.\-]+/[a-f0-9\-]+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset/|/?)(?P<id>movies/[a-z0-9/./-]+/[a-f0-9-]+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset/|/?)(?P<id>tv/[a-z0-9/./-]+/[a-f0-9-]+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset/|/?)(?P<id>tv/[a-z0-9-/.]+/\d+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset/|/?)(?P<id>news/[a-z0-9/./-]+/[a-f0-9-]+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset/|/?)(?P<id>news/[a-z0-9-/.]+/\d+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset/|/?)(?P<id>-/[a-z0-9-/.]+/\d+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/stream-tv/)?(?P<id>[a-z0-9-/.]+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset/|/?)(?P<id>sports/[a-z0-9/./-]+/[a-f0-9-]+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset/|/?)(?P<id>sports/[a-z0-9/./-]+/[a-f0-9-]+)",
        r"(?:https?://(?:www\.)?peacocktv\.com/watch/asset/|/?)(?P<id>sports/[a-z0-9-/.]+/\d+)",
    ]

    @staticmethod
    @click.command(name="Peacock", short_help="https://peacocktv.com")
    @click.argument("title", type=str, required=False)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a movie.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Peacock(ctx, **kwargs)

    def __init__(self, ctx, title, movie):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self.movie = movie

        self.profile = ctx.obj.profile

        self.service_config = None
        self.hmac_key = None
        self.tokens = None
        self.license_api = None
        self.license_bt = None
        self.vcodec = ctx.parent.params["vcodec"]
        self.vrange = ctx.parent.params["range_"]

        self.cdm = ctx.obj.cdm

        self.configure()

    def get_titles(self):
        # Title is a slug, e.g. `/tv/the-office/4902514835143843112` or just `the-office`

        if "/" not in self.title:
            r = self.session.get(self.config["endpoints"]["stream_tv"].format(title_id=self.title))
            self.title = find("/watch/asset(/[^']+)", r.text)
            if not self.title:
                raise self.log.exit(" - Title ID not found or invalid")

        if not self.title.startswith("/"):
            self.title = f"/{self.title}"

        # Normalize slug: strip category/genre if present (e.g. /movies/comedy/the-mask/UUID -> /movies/the-mask/UUID)
        genre_match = re.match(r"^/(movies|tv)/(?:[^/]+/)?([^/]+/[a-f0-9-]+)$", self.title)
        if genre_match:
            self.title = f"/{genre_match.group(1)}/{genre_match.group(2)}"

        if self.title.startswith("/movies/"):
            self.movie = True

        res = self.session.get(
            url=self.config["endpoints"]["node"],
            params={
                "slug": self.title,
                "represent": "(items(items))"
            },
            headers={
                "Accept": "*",
                "Referer": f"https://www.peacocktv.com/watch/asset{self.title}",
                "X-SkyOTT-Device": self.config["client"]["device"],
                "X-SkyOTT-Platform": self.config["client"]["platform"],
                "X-SkyOTT-Proposition": self.config["client"]["proposition"],
                "X-SkyOTT-Provider": self.config["client"]["provider"],
                "X-SkyOTT-Territory": self.config["client"]["territory"],
                "X-SkyOTT-Language": "en"
            }
        ).json()

        if self.movie:
            return Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=res["attributes"]["title"],
                year=res["attributes"]["year"],
                original_lang="eng",
                source=self.ALIASES[0],
                service_data=res,
            )
        else:
            titles = []
            for season in res["relationships"]["items"]["data"]:
                for episode in season["relationships"]["items"]["data"]:
                    titles.append(episode)
            return [Title(
                id_=self.title,
                type_=Title.Types.TV,
                name=res["attributes"]["title"],
                year=x["attributes"].get("year"),
                season=x["attributes"].get("seasonNumber"),
                episode=x["attributes"].get("episodeNumber"),
                episode_name=x["attributes"].get("title"),
                original_lang="eng",
                source=self.ALIASES[0],
                service_data=x
            ) for x in titles]

    def get_tracks(self, title):
        drm_system = "WIDEVINE"
        if self.cdm.device.type.name == "PLAYREADY":
            drm_system = "PLAYREADY"

        supported_colour_spaces = ["SDR"]

        if self.vrange == "HDR10":
            self.log.info("Switched dynamic range to HDR10")
            supported_colour_spaces = ["HDR10"]
        if self.vrange == "DV":
            self.log.info("Switched dynamic range to DV")
            supported_colour_spaces = ["DolbyVision"]
            
        formats = title.service_data["attributes"]["formats"]
        fmt_key = "UHD" if self.vcodec == "H265" and "UHD" in formats else ("HD" if "HD" in formats else list(formats.keys())[0])
        content_id = formats[fmt_key]["contentId"]
        variant_id = title.service_data["attributes"]["providerVariantId"]

        sky_headers = {
            # order of these matter!
            "X-SkyOTT-Agent": ".".join([
                self.config["client"]["proposition"].lower(),
                self.config["client"]["device"].lower(),
                self.config["client"]["platform"].lower()
            ]),
            "X-SkyOTT-PinOverride": "false",
            "X-SkyOTT-Provider": self.config["client"]["provider"],
            "X-SkyOTT-Territory": self.config["client"]["territory"],
            "X-SkyOTT-UserToken": self.tokens["userToken"]
        }

        body = json.dumps({
            "device": {
                "capabilities": [
                    {
                        "protection": drm_system,
                        "container": "ISOBMFF",
                        "transport": "DASH",
                        "acodec": "AAC",
                        "vcodec": "H265" if self.vcodec == "H265" else "H264",
                    },
                    {
                        "protection": "NONE",
                        "container": "ISOBMFF",
                        "transport": "DASH",
                        "acodec": "AAC",
                        "vcodec": "H265" if self.vcodec == "H265" else "H264",
                    }
                ],
                "maxVideoFormat": "UHD" if self.vcodec == "H265" else "HD",
                "supportedColourSpaces": supported_colour_spaces,
                "model": self.config["client"]["platform"],
                "hdcpEnabled": "true"
            },
            "client": {
                "thirdParties": ["FREEWHEEL", "YOSPACE"]  # CONVIVA
            },
            "contentId": content_id,
            "providerVariantId": variant_id,
            "parentalControlPin": None,
            "personaParentalControlRating": "18"
        }, separators=(",", ":"))

        try:
            resp = self.session.post(
                url=self.config["endpoints"]["vod"],
                data=body,
                headers=dict(**sky_headers, **{
                    "Accept": "application/vnd.playvod.v1+json",
                    "Content-Type": "application/vnd.playvod.v1+json",
                    "X-Sky-Signature": self.create_signature_header(
                        method="POST",
                        path="/video/playouts/vod",
                        sky_headers=sky_headers,
                        body=body,
                        timestamp=int(time.time())
                    )
                })
            )
            manifest = resp.json()
        except requests.HTTPError as e:
            err_body = e.response.text if e.response is not None else ""
            click.echo(f"\n[!] VOD Playout Error Response:\n{err_body}\n")
            raise
        if "errorCode" in manifest:
            raise self.log.exit(f" - An error occurred: {manifest['description']} [{manifest['errorCode']}]")

        self.license_api = manifest["protection"]["licenceAcquisitionUrl"]
        self.license_bt = manifest["protection"]["licenceToken"]

        tracks = Tracks.from_mpd(
            url=manifest["asset"]["endpoints"][0]["url"],
            session=self.session,
            source=self.ALIASES[0]
        )
        
        # Fix HDR/DV flags
        for track in tracks.videos:
            if self.vrange == "HDR10":
                track.hdr10 = True
                track.hdr = True
            elif self.vrange == "DV":
                track.dolby_vision = True
                track.hdr = True
                
        for track in tracks:
            track.needs_proxy = False

        for track in tracks.audios:
            if track.language.territory == "AD":
                track.language.territory = None
                track.extra = {"audio_description": True}

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, challenge=None, **_):
        return None

    def license(self, challenge, **_):
        lic_headers = {
            "x-skyott-activeterritory": self.config["client"]["territory"],
            "x-skyott-device": self.config["client"]["device"],
            "x-skyott-platform": self.config["client"]["platform"],
            "x-skyott-proposition": self.config["client"]["proposition"],
            "x-skyott-provider": self.config["client"]["provider"],
            "x-skyott-territory": self.config["client"]["territory"],
            "x-skyott-usertoken": self.tokens["userToken"],
        }
        path = "/" + self.license_api.split("://", 1)[1].split("/", 1)[1]
        ts = int(time.time())
        payload_md5 = hashlib.md5(challenge).hexdigest()

        text_headers = ""
        for key in sorted(lic_headers.keys()):
            if key.lower().startswith("x-skyott"):
                text_headers += key.lower() + ": " + lic_headers[key] + "\n"
        headers_md5 = hashlib.md5(text_headers.encode()).hexdigest()

        to_hash = f"POST\n{path}\n\n{self.config['client']['client_sdk']}\n1.0\n{headers_md5}\n{ts}\n{payload_md5}\n"
        sig = base64.b64encode(hmac.new(self.hmac_key, to_hash.encode("utf8"), hashlib.sha1).digest()).decode("utf8")

        headers = {
            "Accept": "*/*",
            "Content-Type": "application/octet-stream",
            "X-Sky-Signature": self.config["security"]["signature_format"].format(
                client=self.config["client"]["client_sdk"],
                signature=sig,
                timestamp=ts
            ),
            **lic_headers
        }

        res = self.session.post(url=self.license_api, data=challenge, headers=headers)
        res.raise_for_status()
        return res.content

    # Service specific functions

    def configure(self):
        self.session.headers.update({
            "Origin": "https://www.peacocktv.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        
        self.log.info("Getting Peacock Client configuration")
        cookie_count = len(self.session.cookies)
        self.log.info(f"Loaded {cookie_count} cookies")

        self.hmac_key = bytes(self.config["security"]["signature_hmac_key_v4"], "utf-8")
        self.log.info("Getting Authorization Tokens")
        self.tokens = self.get_tokens()
        if not self.tokens or not self.tokens.get("userToken"):
            raise self.log.exit(" - No valid userToken found! Please provide cookies or token in cookies/Peacock/token.txt")

        self.log.info("Verifying Authorization Tokens")
        self.verify_tokens()
        
        if self.config["client"]["platform"] != "PC":
            try:
                self.service_config = self.session.get(
                    url=self.config["endpoints"]["config"].format(
                        territory=self.config["client"]["territory"],
                        provider=self.config["client"]["provider"],
                        proposition=self.config["client"]["proposition"],
                        device=self.config["client"]["platform"],
                        version=self.config["client"]["config_version"],
                    )
                ).json()
                self.log.debug("Service config loaded successfully")
            except Exception as e:
                self.log.warning(f"Could not load service config: {e}")
        


    @staticmethod
    def calculate_sky_header_md5(headers):
        if len(headers.items()) > 0:
            headers_str = "\n".join(f"{x[0].lower()}: {x[1]}" for x in headers.items()) + "\n"
        else:
            headers_str = "{}"
        return str(hashlib.md5(headers_str.encode()).hexdigest())

    @staticmethod
    def calculate_body_md5(body):
        return str(hashlib.md5(body.encode()).hexdigest())

    def calculate_signature(self, msg):
        digest = hmac.new(self.hmac_key, bytes(msg, "utf-8"), hashlib.sha1).digest()
        return str(base64.b64encode(digest), "utf-8")

    def create_signature_header(self, method, path, sky_headers, body, timestamp):
        data = "\n".join([
            method.upper(),
            path,
            "",  # important!
            self.config["client"]["client_sdk"],
            "1.0",
            self.calculate_sky_header_md5(sky_headers),
            str(timestamp),
            self.calculate_body_md5(body)
        ]) + "\n"

        signature_hmac = self.calculate_signature(data)

        return self.config["security"]["signature_format"].format(
            client=self.config["client"]["client_sdk"],
            signature=signature_hmac,
            timestamp=timestamp
        )

        # 1. Check direct token file in cookies/Peacock/token.txt
        token_txt = os.path.join(r"d:\temp\vinetrimmer\cookies\Peacock\token.txt")
        if os.path.isfile(token_txt):
            with open(token_txt, encoding="utf-8") as fd:
                content = fd.read().strip()
            if content:
                tok = None
                # Check for key-value lines
                for line in content.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("userToken\t") or line.startswith("userToken:"):
                        tok = line.split(maxsplit=1)[-1].strip()
                        break
                    elif line.startswith("ST-") or line.startswith("eyJ"):
                        tok = line
                        break
                if not tok and "\t" not in content and "\n" not in content:
                    tok = content
                if tok:
                    self.log.info("Using authentication userToken from cookies/Peacock/token.txt")
                    return {"userToken": tok}

        # 2. Check direct token from cookies
        direct_token = (
            self.session.cookies.get("usertoken")
            or self.session.cookies.get("userToken")
            or self.session.cookies.get("x-skyott-usertoken")
        )
        if direct_token:
            self.log.info("Using authenticated userToken from cookies")
            return {"userToken": direct_token}

        # 3. Try to get cached tokens
        tokens_cache_path = self.get_cache("tokens_{profile}_{id}.json".format(
            profile=self.profile,
            id=self.config["client"]["id"]
        ))
        if os.path.isfile(tokens_cache_path):
            with open(tokens_cache_path, encoding="utf-8") as fd:
                tokens = json.load(fd)
            if tokens.get("userToken"):
                self.log.info("Using cached authentication userToken")
                return tokens
            tokens_expiration = tokens.get("tokenExpiryTime", None)
            if tokens_expiration:
                try:
                    expiry_time = datetime.strptime(tokens_expiration, "%Y-%m-%dT%H:%M:%S.%fZ")
                    if expiry_time > datetime.now():
                        self.log.info("Using cached tokens (valid until {})".format(
                            expiry_time.strftime("%Y-%m-%d %H:%M:%S")
                        ))
                        return tokens
                except ValueError:
                    pass  # Fall through to get new tokens

        sky_base_headers = {
            "X-SkyOTT-Device": self.config["client"]["device"],
            "X-SkyOTT-Platform": self.config["client"]["platform"],
            "X-SkyOTT-Proposition": self.config["client"]["proposition"],
            "X-SkyOTT-Provider": self.config["client"]["provider"],
            "X-SkyOTT-Territory": self.config["client"]["territory"],
            "X-SkyOTT-Language": "en",
        }

        # Step 1: Login with credentials to get X-Token (SkyId token)
        x_token = None
        if self.credentials:
            self.log.info("Logging in with credentials...")
            try:
                login_body = json.dumps({
                    "identifier": self.credentials.username,
                    "credentials": self.credentials.password,
                }, separators=(",", ":"))
                ts = int(time.time())
                login_headers = dict(sky_base_headers)
                login_headers.update({
                    "Accept": "application/vnd.bridge.v1+json",
                    "Content-Type": "application/vnd.bridge.v1+json",
                    "Referer": "https://www.peacocktv.com/",
                    "Origin": "https://www.peacocktv.com",
                    "X-SkyOTT-Agent": ".".join([
                        self.config["client"]["proposition"],
                        self.config["client"]["device"],
                        self.config["client"]["platform"],
                    ]).lower(),
                    "X-Sky-Signature": self.create_signature_header(
                        method="POST",
                        path="/signin/service/international",
                        sky_headers={},
                        body=login_body,
                        timestamp=ts,
                    ),
                })
                login_resp = self.session.post(
                    url=self.config["endpoints"]["login"],
                    headers=login_headers,
                    data=login_body,
                    timeout=30,
                )
                if login_resp.status_code == 200:
                    login_data = login_resp.json()
                    # X-Token can be in response body or response header
                    x_token = (
                        login_resp.headers.get("X-Token")
                        or login_resp.headers.get("x-token")
                        or login_data.get("properties", {}).get("token")
                        or login_data.get("token")
                    )
                    if x_token:
                        self.log.info("Login successful, got X-Token")
                    else:
                        self.log.warning(f"Login returned 200 but no X-Token found. Keys: {list(login_data.keys())}")
                else:
                    err = login_resp.json().get("properties", {}).get("code", login_resp.status_code)
                    self.log.warning(f"Login failed (code {err}). Will try with cookies only.")
            except Exception as e:
                self.log.warning(f"Login attempt failed: {e}. Continuing with cookies.")

        # Check for token in cookies
        if not x_token:
            x_token = (
                self.session.cookies.get("X-Token")
                or self.session.cookies.get("x-token")
                or self.session.cookies.get("st")
                or self.session.cookies.get("X-pwa-session-token")
            )

        # Step 2: Get persona ID from cybertron endpoint
        try:
            self.log.info("Getting persona ID from cybertron endpoint...")

            persona_headers = dict(sky_base_headers)
            persona_headers.update({
                "Accept": "application/vnd.bridge.v1+json",
                "Content-Type": "application/vnd.bridge.v1+json",
                "Referer": "https://www.peacocktv.com/",
                "Origin": "https://www.peacocktv.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            })
            if x_token:
                persona_headers["X-Token"] = x_token

            response = self.session.get(
                url=self.config["endpoints"]["personas"],
                headers=persona_headers,
                timeout=30,
            )

            self.log.debug(f"Personas response status: {response.status_code}")

            if response.status_code == 401:
                raise self.log.exit(" - Authentication failed (401). Credentials are wrong or cookies are expired.")

            if response.status_code == 403:
                raise self.log.exit(" - Access forbidden (403). Check your IP address (must be US).")

            if response.status_code != 200:
                self.log.error(f"Cybertron Personas Error {response.status_code}: {response.text}")
                try:
                    error_data = response.json()
                    error_msg = error_data.get("message", f"HTTP {response.status_code}")
                    raise self.log.exit(f" - Personas error: {error_msg} [{response.text}]")
                except Exception:
                    raise self.log.exit(f" - HTTP Error {response.status_code}: {response.reason} [{response.text}]")

            personas = response.json()
            self.log.debug(f"Personas response: {json.dumps(personas, indent=2)[:500]}...")

            if isinstance(personas, list):
                personas_list = personas
            elif "personas" in personas:
                personas_list = personas["personas"]
            elif "data" in personas:
                personas_list = personas["data"]
            else:
                personas_list = [personas]

            if not personas_list:
                raise self.log.exit(" - No personas found in response.")

            if isinstance(personas_list[0], dict):
                persona = (
                    personas_list[0].get("personaId")
                    or personas_list[0].get("id")
                    or personas_list[0].get("persona")
                )
            else:
                persona = str(personas_list[0])

            if not persona:
                raise self.log.exit(" - Could not extract persona ID from response")

            self.log.info(f"Got persona ID: {str(persona)[:12]}...")

        except requests.exceptions.Timeout:
            raise self.log.exit(" - Timeout connecting to Peacock servers")
        except requests.RequestException as e:
            raise self.log.exit(f" - Network error: {str(e)}")
        except Exception as e:
            raise self.log.exit(f" - Failed to get persona ID: {str(e)}")

        # Step 3: Generate device IDs
        device_id = self.config["client"]["id"]
        if device_id == "Jcvf1y0whKOI29vRXcJy":
            device_id = f"web-{hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()[:16]}"

        drm_device_id = self.config["client"]["drm_device_id"]
        if drm_device_id == "UNKNOWN":
            drm_device_id = f"drm-{hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()[:16]}"

        # Step 4: Get SkyOTT tokens
        body = json.dumps({
            "auth": {
                "authScheme": self.config["client"]["auth_scheme"],
                "authIssuer": self.config["client"]["auth_issuer"],
                "provider": self.config["client"]["provider"],
                "providerTerritory": self.config["client"]["territory"],
                "proposition": self.config["client"]["proposition"],
                "personaId": persona,
            },
            "device": {
                "type": self.config["client"]["device"],
                "platform": self.config["client"]["platform"],
                "id": device_id,
                "drmDeviceId": drm_device_id,
            }
        }, separators=(",", ":"))

        self.log.info("Getting authentication tokens...")

        try:
            timestamp = int(time.time())

            token_headers = dict(sky_base_headers)
            token_headers.update({
                "Accept": "application/vnd.tokens.v1+json",
                "Content-Type": "application/vnd.tokens.v1+json",
                "Referer": "https://www.peacocktv.com/",
                "Origin": "https://www.peacocktv.com",
                "X-SkyOTT-Agent": ".".join([
                    self.config["client"]["proposition"],
                    self.config["client"]["device"],
                    self.config["client"]["platform"],
                ]).lower(),
                "X-Sky-Signature": self.create_signature_header(
                    method="POST",
                    path="/auth/tokens",
                    sky_headers=sky_base_headers,
                    body=body,
                    timestamp=timestamp,
                ),
            })
            if x_token:
                token_headers["X-Token"] = x_token

            tokens_response = self.session.post(
                url=self.config["endpoints"]["tokens"],
                headers=token_headers,
                data=body,
                timeout=30,
            )

            if tokens_response.status_code != 200:
                try:
                    error_data = tokens_response.json()
                    error_msg = error_data.get("message", f"HTTP {tokens_response.status_code}")
                    error_code = error_data.get("code", "")
                    raise self.log.exit(f" - Tokens error: {error_msg} [{error_code}]")
                except Exception:
                    raise self.log.exit(f" - HTTP Error {tokens_response.status_code}: {tokens_response.reason}")

            tokens = tokens_response.json()

            if "userToken" not in tokens:
                raise self.log.exit(" - Invalid tokens response: missing 'userToken'")

            self.log.info("Successfully obtained authentication tokens")

        except Exception as e:
            raise self.log.exit(f" - Failed to get tokens: {str(e)}")

        # Cache the tokens
        os.makedirs(os.path.dirname(tokens_cache_path), exist_ok=True)
        with open(tokens_cache_path, "w", encoding="utf-8") as fd:
            json.dump(tokens, fd)

        return tokens

    def verify_tokens(self):
        """Verify the tokens by checking userToken existence"""
        if self.tokens and self.tokens.get("userToken"):
            self.log.info("Authentication userToken active and verified")
            return True
        return False