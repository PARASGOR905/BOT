import base64
import json
import re
import click
import os, sys

import uuid
import xmltodict

from langcodes import Language
from vinetrimmer.objects import AudioTrack, TextTrack, Title, Tracks, VideoTrack
from vinetrimmer.services.BaseService import BaseService
from vinetrimmer.vendor.pymp4.parser import Box


class Mubi(BaseService):
    """
    Made By redd / Edit by superman
    and Widevine Group - Chrome CDM API dont share this


    \b
    Authorization: Credentials 
    Security: UHD@L3, doesn't care about releases.
    """

    ALIASES = ["MUBI"]

    TITLE_RE = [
        r"^(?:https?://(?:www\.)?mubi\.com)?(?:/[a-z]{2}){0,2}/(?P<id>showing)$",
        r'/(?P<id>[^/]+)$',
        r"^(?:https?://(?:www\.)?mubi\.com\/)?(?P<id>[^/]+)$",
    ]

    AUDIO_CODEC_MAP = {
        "AAC": "mp4a",
        "AC3": "ac-3",
        "EC3": "ec-3"
    }

    @staticmethod
    @click.command(name="Mubi", short_help="https://mubi.com/")
    @click.argument("title", type=str, required=False)
   
    @click.pass_context
    def cli(ctx, **kwargs):
        return Mubi(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.parse_title(ctx, title)

        self.vcodec = ctx.parent.params["vcodec"].lower()
        self.acodec = ctx.parent.params["acodec"]
        self.range = ctx.parent.params["range_"]
        self.bearer= None
        self.dtinfo= None
        self.quality = ctx.parent.params["quality"]
        self.headers = {
                "authority": "api.mubi.com",
                "accept": "application/json",
                "accept-language": "en-US",
                "client": self.config["device"]["client_name"],
                "client-version": "25.1",
                "client-device-identifier": self.config["device"]["device_identifier"],
                "client-app": "mubi",
                "client-device-brand": "Google",
                'client-accept-audio-codecs': 'eac3, ac3, aac',
                "client-device-model": self.config["device"]["device_model"],
                "client-device-os": self.config["device"]["device_os"],
                "client-country": "IN",
                "content-type": "application/json; charset=UTF-8",
                "host": "api.mubi.com",
                "connection": "Keep-Alive",
                "accept-encoding": "gzip",
                "user-agent": self.config["device"]["user_agent"],
            }
        if self.vcodec=="vp9":
            self.headers["client-accept-video-codecs"]="vp9"
        elif self.vcodec=="h265":
            self.headers["client-accept-video-codecs"]="hevc"
        elif self.vcodec=="h264":
            self.headers["client-accept-video-codecs"]="h264"
        else:
            self.headers["client-accept-video-codecs"]="vp9,hevc,h264"

        self.configure()

    def get_titles(self):
        # --- Now Showing batch mode ---
        if self.title == "showing":
            self.log.info(" + Fetching MUBI Now Showing list (India)...")
            titles = []
            page = 1
            while True:
                resp = self.session.get(
                    self.config["endpoints"]["showing"],
                    headers=self.headers,
                    params={"page": page, "per_page": 20},
                ).json()

                films = resp if isinstance(resp, list) else resp.get("films", resp.get("showing", []))
                if not films:
                    break

                for film in films:
                    # API may return the film nested under a "film" key
                    f = film.get("film", film)
                    consumable = film.get("consumable", f.get("consumable", {}))
                    availability = consumable.get("availability", "live") if consumable else "live"
                    if availability != "live":
                        continue
                    film_id = f.get("id")
                    if not film_id:
                        continue
                    titles.append(
                        Title(
                            id_=film_id,
                            type_=Title.Types.MOVIE,
                            name=f.get("title", f.get("original_title", str(film_id))),
                            year=f.get("year"),
                            source=self.ALIASES[0],
                            service_data=f,
                        )
                    )
                    self.log.info(f"   + Found: {f.get('title')} ({f.get('year')}) [ID: {film_id}]")

                # Stop if fewer results than a full page were returned
                if len(films) < 20:
                    break
                page += 1

            self.log.info(f" + Total Now Showing titles found: {len(titles)}")
            return titles

        # --- Single film mode ---
        self.log.info(" + Getting Metadata.")
        res = self.session.get(
                self.config["endpoints"]["metadata"].format(title_id=self.title)
                ,headers=self.headers).json()
        try:
            res
        except json.JSONDecodeError:
            raise self.log.exit(f" - Failed to load title metadata: {res.text}")
        
        self.title = res['id']   
        
        return Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=res["title"],
                year=res["year"],
                source=self.ALIASES[0],
                service_data=res,
            )
    
    def create_pssh_from_kid(self, kid: str):
        WV_SYSTEM_ID = [237, 239, 139, 169, 121, 214, 74, 206, 163, 200, 39, 220, 213, 29, 33, 237]
        kid = uuid.UUID(kid).bytes

        init_data = bytearray(b'\x12\x10')
        init_data.extend(kid)
        init_data.extend(b'H\xe3\xdc\x95\x9b\x06')

        pssh = bytearray([0, 0, 0])
        pssh.append(32 + len(init_data))
        pssh[4:] = bytearray(b'pssh')
        pssh[8:] = [0, 0, 0, 0]
        pssh[13:] = WV_SYSTEM_ID
        pssh[29:] = [0, 0, 0, 0]
        pssh[31] = len(init_data)
        pssh[32:] = init_data

        return base64.b64encode(pssh).decode('UTF-8')
    
    def get_pssh_from_mpd(self, mpd_url):
        r = self.session.get(mpd_url, headers=self.headers)

        if r.status_code != 200:
            raise Exception(r.text)

        mpd = xmltodict.parse(r.text, dict_constructor=dict)

        for adaption in mpd['MPD']['Period']['AdaptationSet']:
            if adaption['@mimeType'] == 'video/mp4':
                if 'ContentProtection' in adaption:
                    for protection in adaption['ContentProtection']:
                        if protection['@schemeIdUri'].lower() == 'urn:mpeg:dash:mp4protection:2011':
                            return self.create_pssh_from_kid(protection['@cenc:default_KID'])

    def get_tracks(self, title):
        film_id = title.id  # Title stores as .id (id_ is only the constructor param name)
        res = self.session.post(
            self.config["endpoints"]["viewing"].format(title_id=film_id), headers=self.headers
        ).json()

        # Log full viewing response for debugging
        self.log.debug(f" + Viewing response keys: {list(res.keys())}")
        self.log.info(f" + Viewing response: {json.dumps({k: v for k, v in res.items() if k != 'url'}, indent=2)[:500]}")

        lang = res.get("audio_track_id", "")
        title.original_lang = lang.replace('audio_main_', '')

        # DRMtoday needs the session token from the VIEWING response, not the user login token.
        # Check for a session-specific token in the viewing response.
        if "session" in res and isinstance(res["session"], dict):
            session_token = res["session"].get("token") or res["session"].get("id")
        elif "token" in res:
            session_token = res["token"]
        elif "session_id" in res:
            session_token = res["session_id"]
        else:
            session_token = None

        if session_token:
            self.log.info(f" + Using viewing session token for dt-custom-data")
            # Extract userId from cached dtinfo
            try:
                existing = json.loads(base64.b64decode(self.dtinfo).decode('utf-8'))
                user_id = existing.get("userId", "")
            except Exception:
                user_id = ""
            json_str = {"merchant": "mubi", "sessionId": session_token, "userId": str(user_id)}
            self.dtinfo = base64.b64encode(json.dumps(json_str).encode('utf-8')).decode('utf-8')
            self.log.debug(f" + Updated dt-custom-data with viewing session token")
        
        data = self.session.get(
            self.config["endpoints"]["manifest"].format(title_id=film_id),headers=self.headers
        ).json()

        if self.quality==2160:
            mpd_url = re.sub(r"/default/.*\.mpd$", "/default/2160.mpd", data["url"])
        else:
            mpd_url=data["url"]
        
        pssh = self.get_pssh_from_mpd(mpd_url)
        video_pssh = Box.parse(base64.b64decode(pssh))

        tracks=Tracks.from_mpd(
            url=mpd_url,
            session=self.session,
            source=self.ALIASES[0],
        )

# FIX: Checks whether the pssh attribute exists before accessing it
# And defines both pssh and psshWV for compatibility with dl.py
        for track in tracks.videos:
            if not hasattr(track, 'pssh') or not track.pssh:            
                track.pssh = video_pssh
                track.psshWV = pssh  # Adds psshWV in string format
        
        for track in tracks.audios:
            if not hasattr(track, 'pssh') or not track.pssh:
                track.pssh = video_pssh
                track.psshWV = pssh  # Adds psshWV in string format

        if self.acodec:
            tracks.audios = [x for x in tracks.audios if (x.codec or "")[:4] == self.AUDIO_CODEC_MAP[self.acodec]]

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **kwargs):
        print("DEBUG: certificate() method CALLED!")
        self.log.info(" + Certificate method called")
        return None

    def license(self, challenge, **kwargs):
        """
        License method supporting both PlayReady and Widevine for DRMtoday.
        """
        # Determine if the challenge is PlayReady (XML/SOAP) or Widevine (protobuf binary)
        is_playready = (
            (isinstance(challenge, bytes) and (challenge.strip().startswith(b"<") or b"schemas.microsoft.com" in challenge or b"soap" in challenge[:100].lower()))
            or (isinstance(challenge, str) and (challenge.strip().startswith("<") or "schemas.microsoft.com" in challenge or "soap" in challenge[:100].lower()))
        )

        if is_playready:
            license_url = self.config["endpoints"].get(
                "license_pr", "https://lic.drmtoday.com/license-proxy-headerauth/drmtoday/RightsManager.asmx"
            )
            headers = {
                "dt-custom-data": self.dtinfo,
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": "http://schemas.microsoft.com/DRM/2007/03/protocols/AcquireLicense",
                "User-Agent": self.config["device"].get("user_agent", "okhttp/4.12.0"),
            }
            self.log.info(" + DRM Type: PlayReady")
        else:
            license_url = self.config["endpoints"].get(
                "license_wv", self.config["endpoints"].get("license", "https://lic.drmtoday.com/license-proxy-widevine/cenc/")
            )
            headers = {
                "dt-custom-data": self.dtinfo,
                "Content-Type": "application/octet-stream",
                "User-Agent": self.config["device"].get("user_agent", "okhttp/4.12.0"),
                "Accept": "*/*"
            }
            self.log.info(" + DRM Type: Widevine")

        self.log.info(f" + Requesting License from: {license_url}")
        self.log.debug(f" + Challenge length: {len(challenge) if challenge else 0} bytes")

        # Log dt-custom-data decoded info
        try:
            dtinfo_decoded = json.loads(base64.b64decode(self.dtinfo).decode('utf-8'))
            self.log.info(f" + dt-custom-data: merchant={dtinfo_decoded.get('merchant')}, userId={dtinfo_decoded.get('userId')}, sessionId={str(dtinfo_decoded.get('sessionId', ''))[:20]}...")
        except Exception:
            self.log.debug(f" + dt-custom-data (raw): {self.dtinfo[:60]}...")

        # Disable raise_for_status hook so we can read the error body ourselves
        original_hooks = self.session.hooks.copy()
        self.session.hooks = {"response": []}

        try:
            response = self.session.post(
                url=license_url,
                headers=headers,
                data=challenge,
                timeout=30
            )

            self.log.info(f" + License Response Status: {response.status_code}")

            if response.status_code != 200:
                self.log.error(f" - License request failed! Status Code: {response.status_code}")
                self.log.error(f" - Response Body: {response.text[:1000]}")
                return None

            if not response.content:
                self.log.error(" - License response is empty!")
                return None

            if is_playready:
                # PlayReady returns XML / binary SOAP response
                self.log.info(f" + PlayReady License received: {len(response.content)} bytes")
                return response.content

            # Widevine returns JSON with base64 license
            try:
                license_data = response.json()
                self.log.debug(f" + License JSON keys: {list(license_data.keys())}")

                if 'license' in license_data:
                    license_b64 = license_data['license']
                elif 'payload' in license_data:
                    license_b64 = license_data['payload']
                else:
                    self.log.error(f" - Unexpected JSON structure: {list(license_data.keys())}")
                    return None

                license_bytes = base64.b64decode(license_b64)
                self.log.info(f" + Widevine License received: {len(license_bytes)} bytes")
                return license_bytes

            except json.JSONDecodeError:
                self.log.info(f" + License received: {len(response.content)} bytes (binary)")
                return response.content

        except Exception as e:
            self.log.error(f" - Exception during license request: {type(e).__name__}")
            self.log.error(f" - Error message: {str(e)}")
            import traceback
            self.log.debug(f" - Full traceback:\n{traceback.format_exc()}")
            return None
        finally:
            self.session.hooks = original_hooks
        
    def configure(self):
        tokens_cache_path = self.get_cache("tokens_mubi.json")
        self.log.info(" + Loading Cached Token...")

        # --- Priority 1: Use existing JSON token cache ---
        if os.path.isfile(tokens_cache_path):
            with open(tokens_cache_path, encoding="utf-8") as fd:
                tokens = json.load(fd)
            self.bearer = tokens["authorization"]
            self.dtinfo = tokens["dt-custom-data"]
            self.headers["authorization"] = self.bearer
            self.log.debug(f" + Loaded bearer token: {self.bearer[:50]}...")
            self.log.debug(f" + Loaded dt-custom-data: {self.dtinfo[:50]}...")
            return

        # --- Priority 2: Manual token file (cookies/Mubi/mubi_token.txt) ---
        # This is the most reliable method when API login is blocked.
        # Get your token from browser DevTools → Network → any api.mubi.com request → Authorization header.
        # mubi.py lives in vinetrimmer/services/ — go up one level to reach cookies/Mubi/
        cookie_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "cookies", "Mubi"))
        manual_token_path = os.path.join(cookie_dir, "mubi_token.txt")

        if os.path.isfile(manual_token_path):
            self.log.info(" + Found manual token file, loading...")
            with open(manual_token_path, encoding="utf-8-sig") as fd:  # utf-8-sig strips BOM automatically
                lines = [l.strip().lstrip('\ufeff') for l in fd.readlines() if l.strip() and not l.strip().startswith("#")]

            if len(lines) < 2:
                raise self.log.exit(
                    " - mubi_token.txt must have 2 lines:\n"
                    "   Line 1: Bearer <your_token>\n"
                    "   Line 2: <your_user_id>"
                )

            bearer_line = lines[0]
            user_id     = lines[1]

            if not bearer_line.startswith("Bearer "):
                bearer_line = "Bearer " + bearer_line

            session_token = bearer_line.replace("Bearer ", "").strip()
            json_str = {
                "merchant": "mubi",
                "sessionId": session_token,
                "userId": str(user_id)
            }

            self.bearer = bearer_line
            self.dtinfo = base64.b64encode(json.dumps(json_str).encode("utf-8")).decode("utf-8")
            self.headers["authorization"] = self.bearer

            # Save to cache so we don't need the file next time
            save_data = {"authorization": self.bearer, "dt-custom-data": self.dtinfo}
            os.makedirs(os.path.dirname(tokens_cache_path), exist_ok=True)
            with open(tokens_cache_path, "w", encoding="utf-8") as fd:
                json.dump(save_data, fd)

            self.log.info(" + Manual token loaded and cached successfully.")
            return

        # --- Priority 3: API login (may be blocked by MUBI with error 27) ---
        self.log.info(" + Attempting API login...")
        if not self.credentials or not self.credentials.username:
            self._show_manual_token_instructions()
            raise self.log.exit(" - No credentials and no manual token file found.")

        original_hooks = self.session.hooks.copy()
        self.session.hooks = {"response": []}

        try:
            auth_r = self.session.post(
                url=self.config["endpoints"]["authtok_url"],
                json={"identifier": self.credentials.username, "magic_link": False},
                headers=self.headers
            )

            if auth_r.status_code != 200:
                self._show_manual_token_instructions()
                raise self.log.exit(
                    f" - API login blocked ({auth_r.status_code}): {auth_r.text[:200]}\n"
                    f"   See instructions above to set up mubi_token.txt instead."
                )

            auth_resp = auth_r.json()
            if "auth_request_token" not in auth_resp:
                self._show_manual_token_instructions()
                raise self.log.exit(f" - Unexpected auth response: {auth_resp}")

            login_r = self.session.post(
                url=self.config["endpoints"]["loginurl"],
                json={
                    "auth_request_token": auth_resp["auth_request_token"],
                    "identifier": self.credentials.username,
                    "password": self.credentials.password
                },
                headers=self.headers
            )

            if login_r.status_code != 200:
                self._show_manual_token_instructions()
                raise self.log.exit(f" - Login failed ({login_r.status_code}): {login_r.text[:200]}")

            response = login_r.json()
            if "token" not in response:
                raise self.log.exit(f" - Login response missing token: {response}")

        finally:
            self.session.hooks = original_hooks

        json_str = {
            "merchant": "mubi",
            "sessionId": response["token"],
            "userId": str(response["user"]["id"])
        }
        self.bearer = "Bearer " + response["token"]
        self.dtinfo = base64.b64encode(json.dumps(json_str).encode("utf-8")).decode("utf-8")
        self.headers["authorization"] = self.bearer

        save_data = {"authorization": self.bearer, "dt-custom-data": self.dtinfo}
        os.makedirs(os.path.dirname(tokens_cache_path), exist_ok=True)
        with open(tokens_cache_path, "w", encoding="utf-8") as fd:
            json.dump(save_data, fd)

        self.log.info(" + Token saved to cache.")
        self.log.debug(f" + Bearer: {self.bearer[:50]}...")
        self.log.debug(f" + dt-custom-data: {self.dtinfo[:50]}...")

    def _show_manual_token_instructions(self):
        """Print step-by-step instructions for obtaining the token manually."""
        cookie_dir = os.path.join(os.path.dirname(__file__), "..", "cookies", "Mubi")
        manual_token_path = os.path.normpath(os.path.join(cookie_dir, "mubi_token.txt"))
        print("\n" + "=" * 70)
        print("  MUBI API LOGIN BLOCKED — Manual Token Setup Required")
        print("=" * 70)
        print("  MUBI is blocking automated login. Get your token manually:")
        print()
        print("  1. Open https://mubi.com in your browser and log in.")
        print("  2. Press F12  →  Network tab.")
        print("  3. Filter requests by: api.mubi.com")
        print("  4. Click any request to api.mubi.com.")
        print("  5. Go to 'Request Headers' and find the 'authorization' header.")
        print("     It looks like:  authorization: Bearer eyJhbGci...")
        print()
        print("  6. Also find your User ID:")
        print("     Go to https://api.mubi.com/v3/me (logged in) in your browser.")
        print("     Note down the 'id' field value (a number).")
        print()
        print(f"  7. Create this file:  {manual_token_path}")
        print("     Contents (2 lines):")
        print("       Line 1:  Bearer eyJhbGci...  (the full Authorization value)")
        print("       Line 2:  12345678            (your numeric user ID)")
        print("=" * 70 + "\n")