from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone

import click
import m3u8

from vinetrimmer.objects import Title, Tracks, AudioTrack
from vinetrimmer.services.BaseService import BaseService


def _parse_lang(label):
    if not label:
        return None
    try:
        from langcodes import Language
        return str(Language.find(label))
    except Exception:
        return None


def _generate_pallycon_token(content_uuid: str, access_key: str, secret_key: str, drm_type: str = "Widevine") -> str:
    """
    Generate a PallyCon V2 license token.
    - secret_key (32 bytes) is used as the AES-256-CBC encryption key.
    - access_key is used as the HMAC-SHA256 signing key.
    Reference: https://developers.pallycon.com/multidrm/license/license-token/
    """
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad as crypto_pad

    # 1. Build the license rule
    license_rule = json.dumps({"playback_policy": {"persistent": False}}, separators=(',', ':'))

    # 2. AES-256-CBC encrypt the license rule
    site_key = secret_key.encode("utf-8")          # must be 32 bytes
    aes_iv   = b"0123456789abcdef"                  # standard PallyCon IV
    cipher   = AES.new(site_key, AES.MODE_CBC, aes_iv)
    encrypted_rule = base64.b64encode(
        cipher.encrypt(crypto_pad(license_rule.encode("utf-8"), AES.block_size))
    ).decode("utf-8")

    # 3. HMAC-SHA256 over: access_key + drm_type + site_id + user_id + cid + token + timestamp
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    site_id   = ""           # not required for partner integration
    user_id   = "waves_user"
    hash_input = f"{access_key}{drm_type}{site_id}{user_id}{content_uuid}{encrypted_rule}{timestamp}"
    hash_b64   = base64.b64encode(
        hmac.new(access_key.encode("utf-8"), hash_input.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    # 4. Assemble and base64-encode the final JSON payload
    payload = {
        "drm_type":  drm_type,
        "site_id":   site_id,
        "user_id":   user_id,
        "cid":       content_uuid,
        "token":     encrypted_rule,
        "timestamp": timestamp,
        "hash":      hash_b64,
    }
    return base64.b64encode(json.dumps(payload, separators=(',', ':')).encode("utf-8")).decode("utf-8")


class Waves(BaseService):
    """
    Service code for Waves OTT platform (https://www.wavespb.com).

    Authorization: None (Cookies optional for premium content)
    Security: Widevine DRM / PlayReady DRM
    """

    ALIASES = ["WAVES", "waves", "wavespb"]
    GEOFENCE = []
    TITLE_RE = [
        r"^(?P<id>https?://\S+\.(?:mpd|m3u8)(?:\?\S*)?)$",
        r"^(?P<id>https?://(?:www\.)?wavespb\.com/\S+)$",
    ]

    @staticmethod
    @click.command(name="Waves", short_help="https://www.wavespb.com")
    @click.argument("title", type=str)
    @click.option("--high-audio/--no-high-audio", "high_audio", default=False, help="Use HLS FairPlay manifest with Widevine key to get 576 kbps audio.")
    @click.option("-wi", "--waves-id", default=None, type=str, help="Waves content ID (numeric) to fetch PallyCon credentials when using a direct MPD URL for partner content.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Waves(ctx, **kwargs)

    def __init__(self, ctx, title: str, high_audio: bool = False, waves_id: str = None):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self.token = None
        self.high_audio = high_audio
        self.waves_id = waves_id
        # PallyCon credentials for Lionsgate partner content
        self._pallycon_access_key = None
        self._pallycon_secret_key = None
        self._pallycon_content_uuid = None

    def get_titles(self) -> Title | list[Title]:
        # 1. Direct manifest URL input (.mpd or .m3u8)
        if re.search(r"\.(?:mpd|m3u8)", self.title, re.IGNORECASE):
            self.log.debug(" + Using direct manifest URL: %s", self.title)
            name = "Waves_Video"
            path_part = self.title.split("?")[0].rstrip("/")
            match_segment = re.search(r"([^/]+)\.(?:mpd|m3u8)", path_part, re.IGNORECASE)
            if match_segment:
                name = match_segment.group(1)

            # If user supplied --waves-id, fetch PallyCon credentials for Lionsgate partner content
            pallycon_data = {}
            if self.waves_id:
                pallycon_data = self._fetch_pallycon_credentials(self.waves_id)

            return Title(
                id_=name,
                type_=Title.Types.MOVIE,
                name=name,
                source=self.ALIASES[0],
                service_data={"manifest_url": self.title, **pallycon_data},
            )

        # 2. Webpage watch URL
        if "wavespb.com" in self.title:
            self.log.debug(" + Loading webpage/API details for: %s", self.title)
            
            # Extract content ID from the URL (last digits in path)
            match = re.search(r"/(\d+)(?:\?|$)", self.title)
            if not match:
                raise ValueError(f"Could not extract content ID from URL: {self.title}")
            content_id = match.group(1)
            
            url = "https://www.wavespb.com/web/dashboard/ajax_data_details"
            headers = {
                "Origin": "https://www.wavespb.com",
                "Referer": self.title,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
            }
            r = self.session.post(url, headers=headers, data={"id": content_id})
            res = r.json()
            if not res.get("status") or not res.get("data"):
                raise ValueError(f"API returned failure for ID {content_id}: {res.get('message')}")
                
            data = res["data"]
            title_name = data.get("title") or "Waves_Video"
            year = int(data.get("released_on") or 0)
            lang = _parse_lang(data.get("language_title"))
            
            season_list = data.get("season") or []
            is_show = len(season_list) > 0 and not (
                data.get("skip_season") == 1 or 
                data.get("category_title") == "Movies" or 
                (len(season_list) == 1 and len(season_list[0].get("videos") or []) <= 1)
            )
            
            if is_show:
                titles = []
                for index, season in enumerate(season_list):
                    season_num = int(season.get("number") or index + 1)
                    videos = season.get("videos") or []
                    for video in videos:
                        if video.get("is_trailer") == 1:
                            continue
                        ep_num = int(video.get("episode") or video.get("number") or 1)
                        ep_title = video.get("title")
                        
                        titles.append(Title(
                            id_=str(video.get("id")),
                            type_=Title.Types.TV,
                            name=title_name,
                            year=year,
                            season=season_num,
                            episode=ep_num,
                            episode_name=ep_title,
                            original_lang=lang,
                            source=self.ALIASES[0],
                            service_data={
                                "vdcId": video.get("vdc_id"),
                                "mediaId": str(video.get("id")),
                                "is_drm_protected": video.get("is_drm_protected"),
                                "file_url": video.get("file_url")
                            }
                        ))
                return titles
            else:
                # Movie / Single video
                video = {}
                if len(season_list) > 0 and len(season_list[0].get("videos") or []) > 0:
                    video = season_list[0]["videos"][0]
                
                video_id = str(video.get("id") or data.get("id"))
                vdc_id = video.get("vdc_id") or data.get("vdc_id")
                is_drm = video.get("is_drm_protected")
                if is_drm is None:
                    is_drm = data.get("is_drm_protected")
                
                owned_by = data.get("owned_by") or video.get("owned_by") or 0
                
                return Title(
                    id_=video_id,
                    type_=Title.Types.MOVIE,
                    name=title_name,
                    year=year,
                    original_lang=lang,
                    source=self.ALIASES[0],
                    service_data={
                        "vdcId": vdc_id,
                        "mediaId": video_id,
                        "is_drm_protected": is_drm,
                        "file_url": video.get("file_url") or data.get("file_url"),
                        "owned_by": owned_by,
                        # PallyCon credentials for partner (Lionsgate) DRM
                        "pallycon_access_key": data.get("access_key"),
                        "pallycon_secret_key": data.get("secret_key"),
                        "pallycon_content_uuid": data.get("contentId"),
                        "pallycon_license_url": data.get("website_url"),
                    }
                )

        if not self.title.startswith("http"):
            raise ValueError("Input must be a wavespb.com watch URL or a direct .mpd/.m3u8 manifest link.")

    def _fetch_pallycon_credentials(self, waves_content_id: str) -> dict:
        """Fetch PallyCon DRM credentials from the Waves API for a given numeric content ID."""
        url = "https://www.wavespb.com/web/dashboard/ajax_data_details"
        headers = {
            "Origin": "https://www.wavespb.com",
            "Referer": "https://www.wavespb.com/",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
        }
        try:
            r = self.session.post(url, headers=headers, data={"id": waves_content_id})
            res = r.json()
            if res.get("status") and res.get("data"):
                data = res["data"]
                self.log.info(" + Fetched PallyCon credentials for Waves ID %s (%s)", waves_content_id, data.get("title", ""))
                return {
                    "pallycon_access_key":   data.get("access_key"),
                    "pallycon_secret_key":   data.get("secret_key"),
                    "pallycon_content_uuid": data.get("contentId"),
                    "pallycon_license_url":  data.get("website_url"),
                }
        except Exception as e:
            self.log.warning(" + Could not fetch PallyCon credentials for Waves ID %s: %s", waves_content_id, e)
        return {}

    def get_tracks(self, title: Title) -> Tracks:
        manifest_url = title.service_data.get("manifest_url")
        vdc_id = title.service_data.get("vdcId")
        media_id = title.service_data.get("mediaId")
        is_drm_protected = title.service_data.get("is_drm_protected")
        file_url = title.service_data.get("file_url")
        owned_by = title.service_data.get("owned_by") or 0

        # If no manifest_url is in service_data, fetch it using getDrmData API
        if not manifest_url:
            if (is_drm_protected == 1 or owned_by != 0) and vdc_id:
                self.log.debug(" + Fetching DRM data for media ID: %s, VDC ID: %s", media_id, vdc_id)
                url = "https://www.wavespb.com/web/home/getDrmData"
                headers = {
                    "Origin": "https://www.wavespb.com",
                    "Referer": "https://www.wavespb.com/",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
                }
                data = {
                    "vdcId": vdc_id,
                    "mediaId": media_id,
                    "is_audio": "0"
                }
                r = self.session.post(url, headers=headers, data=data)
                res = r.json()
                if not res.get("status") or not res.get("file_url"):
                    raise ValueError(f"Failed to fetch DRM data for media ID {media_id}: {res}")
                
                manifest_url = res["file_url"]
                self.token = res.get("token")  # Store token on instance so license request can access it
                self.log.debug(" + Obtained manifest URL: %s", manifest_url)
                # Diagnostic: decode and log the token contents so we can see what PallyCon receives
                if self.token:
                    try:
                        import base64, json as _json
                        token_bytes = base64.b64decode(self.token + "==")  # add padding
                        token_data = _json.loads(token_bytes)
                        self.log.debug(
                            " + [DRM Token] drm_type=%s  site_id=%s  user_id=%s  cid=%s  timestamp=%s",
                            token_data.get("drm_type"), token_data.get("site_id"),
                            token_data.get("user_id"), token_data.get("cid"),
                            token_data.get("timestamp"),
                        )
                    except Exception as _te:
                        self.log.debug(" + [DRM Token] Could not decode token: %s", _te)

            else:
                if not file_url:
                    raise ValueError(f"No file_url or DRM data found for media ID: {media_id}")
                manifest_url = file_url
                self.log.debug(" + Using media URL: %s", manifest_url)

        if self.high_audio and (is_drm_protected == 1 or owned_by != 0) and vdc_id:
            self.log.debug(" + [--high-audio] mode active. Initializing HLS FairPlay manifest flow...")
            
            # 1. Initialize DASH manifest session & get Widevine keys
            dash_manifest_url = manifest_url
            if "/v1/session/" in dash_manifest_url:
                r = self.session.post(dash_manifest_url)
                res = r.json()
                if "manifestUrl" in res:
                    from urllib.parse import urljoin
                    dash_manifest_url = urljoin(dash_manifest_url, res["manifestUrl"])
                else:
                    raise ValueError(f"Failed to initialize DASH manifest session: {res}")
            
            dash_tracks = Tracks.from_mpd(
                url=dash_manifest_url,
                session=self.session,
                source=self.ALIASES[0]
            )
            
            # 2. Fetch HLS manifest URL using Safari UA via a fresh requests call (bypassing session state)
            safari_ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
            url_drm = "https://www.wavespb.com/web/home/getDrmData"
            headers_hls = {
                "Origin": "https://www.wavespb.com",
                "Referer": "https://www.wavespb.com/",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "User-Agent": safari_ua
            }
            data_hls = {
                "vdcId": vdc_id,
                "mediaId": media_id,
                "is_audio": "0"
            }
            
            # Resolve proxy from session if force_proxy is true or needs_proxy is true
            # Let's check self.session.proxies
            proxy = next(iter(self.session.proxies.values()), None)
            proxies = {"all": proxy} if proxy else None
            
            import requests
            r_hls = requests.post(url_drm, headers=headers_hls, data=data_hls, proxies=proxies)
            res_hls = r_hls.json()
            if not res_hls.get("status") or not res_hls.get("file_url"):
                raise ValueError(f"Failed to fetch HLS DRM data: {res_hls}")

            # NOTE: do NOT update self.token from res_hls.
            # This getDrmData call uses a Safari UA, targeting the FairPlay/HLS
            # flow.  The server returns a FairPlay token, NOT a Widevine token.
            # Overwriting self.token with it breaks the Widevine cert request
            # (PallyCon rejects the FairPlay token → JSON 7129).  Keep the DASH
            # token that was stored above (via self.session.post / Chrome UA).

            hls_manifest_url = res_hls["file_url"]
            if "/v1/session/" in hls_manifest_url:
                r = requests.post(hls_manifest_url, headers={"User-Agent": safari_ua}, proxies=proxies)
                res = r.json()
                if "manifestUrl" in res:
                    from urllib.parse import urljoin
                    hls_manifest_url = urljoin(hls_manifest_url, res["manifestUrl"])
                else:
                    raise ValueError(f"Failed to initialize HLS manifest session: {res}")
            
            # 3. Parse HLS manifest and get the highest quality variant
            r_hls_m = requests.get(hls_manifest_url, headers={"User-Agent": safari_ua}, proxies=proxies)
            self.log.debug(f"HLS manifest response status: {r_hls_m.status_code}")
            self.log.debug(f"HLS manifest response body: {r_hls_m.text[:500]}")

            master = m3u8.loads(r_hls_m.text, uri=hls_manifest_url)
            if not master.playlists:
                raise ValueError(f"No variant playlists found in the HLS master manifest. Response: {r_hls_m.text[:200]}")
            
            highest_playlist = master.playlists[-1]
            hls_variant_url = highest_playlist.absolute_uri
            
            # 4. Extract DASH audio PSSH and KID
            dash_audio = next(iter(dash_tracks.audios), None)
            if not dash_audio:
                dash_audio = next((t for t in dash_tracks.videos + dash_tracks.audios if t.pssh), None)
            if not dash_audio:
                raise ValueError("Could not find any encrypted DASH audio/video track to retrieve Widevine keys.")
            
            # 5. Create AudioTrack combining HLS url and DASH keys, monkey-patching download method
            from langcodes import Language
            hls_audio = AudioTrack(
                id_=dash_audio.id,
                source=self.ALIASES[0],
                url=hls_variant_url,
                codec="aac",
                language=dash_audio.language,
                bitrate=576000,
                encrypted=True,
                pssh=dash_audio.pssh,
                pr_pssh=dash_audio.pr_pssh,
                kid=dash_audio.kid,
            )
            import types
            hls_audio.download = types.MethodType(_hls_download, hls_audio)
            
            # Map languages for DASH tracks and HLS audio track
            for track in dash_tracks.videos + dash_tracks.audios + [hls_audio]:
                from langcodes import Language as Lang
                if track.language in (Lang.get("und"), Lang.get("none"), Lang.get("en"), Lang.get("eng"), None) and title.original_lang:
                    track.language = Lang.get(title.original_lang)
            
            # Return video and subtitle tracks from DASH, and custom high audio from HLS
            res_tracks = Tracks()
            res_tracks.videos = dash_tracks.videos
            res_tracks.audios = [hls_audio]
            res_tracks.subtitles = dash_tracks.subtitles
            return res_tracks

        # Check if manifest URL is a session initialization URL (requires POST request)
        if "/v1/session/" in manifest_url:
            self.log.debug(" + Initializing manifest session via POST...")
            r = self.session.post(manifest_url)
            res = r.json()
            if "manifestUrl" in res:
                from urllib.parse import urljoin
                manifest_url = urljoin(manifest_url, res["manifestUrl"])
                self.log.debug(" + Initialized manifest URL: %s", manifest_url)
            else:
                raise ValueError(f"Failed to initialize manifest session: {res}")

        is_dash = ".mpd" in manifest_url.split("?")[0].lower()

        if is_dash:
            tracks = Tracks.from_mpd(
                url=manifest_url,
                session=self.session,
                source=self.ALIASES[0]
            )
        else:
            r = self.session.get(manifest_url)
            master = m3u8.loads(r.text, uri=manifest_url)
            tracks = Tracks.from_m3u8(
                master=master,
                source=self.ALIASES[0]
            )

        # Map 'und', 'none', 'en' audio languages to the title's original language (e.g. Hindi)
        from langcodes import Language as Lang
        for track in tracks.audios:
            if track.language in (Lang.get("und"), Lang.get("none"), Lang.get("en"), None) and title.original_lang:
                track.language = Lang.get(title.original_lang)

        return tracks

    def get_chapters(self, title: Title) -> list:
        return []

    def _refresh_drm_token(self, vdc_id: str, media_id: str) -> str | None:
        """
        Re-fetch a fresh DRM token from getDrmData.

        The cert request may have consumed the previous single-use token.
        Calling getDrmData again immediately before the license challenge
        guarantees we present a live, unconsumed token to PallyCon.
        """
        try:
            url = "https://www.wavespb.com/web/home/getDrmData"
            headers = {
                "Origin": "https://www.wavespb.com",
                "Referer": "https://www.wavespb.com/",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            }
            data = {"vdcId": vdc_id, "mediaId": media_id, "is_audio": "0"}
            r = self.session.post(url, headers=headers, data=data)
            res = r.json()
            if res.get("status") and res.get("token"):
                self.log.debug(" + Refreshed DRM token for license request")
                return res["token"]
            self.log.warning(" + getDrmData token refresh returned no token: %s", res)
        except Exception as e:
            self.log.warning(" + Could not refresh DRM token: %s", e)
        return None

    def certificate(self, challenge: bytes, title=None, track=None, **_):
        """
        Fetch the PallyCon / Waves service certificate so the CDM can encrypt
        the license challenge with the server's own public key (privacy mode).

        Without this, the CDM falls back to the generic Google Widevine
        privacy cert, which PallyCon's server cannot decrypt, resulting in:
          errorCode 7129 – SERVICE_CERTIFICATE_NOT_FOUND

        The Waves license endpoint requires the pallycon-customdata-v2 token
        even for cert requests (returns 500 without it).  The token is
        potentially single-use, so license() always re-fetches a fresh token
        from getDrmData immediately before sending the actual license challenge,
        ensuring the cert request here does not starve the license step.
        """
        license_url = self.config["endpoints"]["license"]

        headers = {
            "Content-Type": "application/octet-stream",
            "Origin": "https://www.wavespb.com",
            "Referer": "https://www.wavespb.com/",
        }
        # The Waves/PallyCon endpoint requires the token even for cert fetches.
        if hasattr(self, "token") and self.token:
            headers["pallycon-customdata-v2"] = self.token

        self.log.debug(" + Fetching service certificate from: %s", license_url)
        try:
            r = self.session.post(url=license_url, data=challenge, headers=headers)
            self.log.debug(" + Certificate Response Status: %s", r.status_code)
            self.log.debug(
                " + Certificate Response (first 64 bytes hex): %s",
                r.content[:64].hex() if r.content else "<empty>"
            )
            if r.status_code == 200 and r.content and not r.content.lstrip().startswith(b"{"):
                # Only return the response if it looks like binary protobuf data,
                # not a JSON error payload.
                return r.content
            elif r.status_code == 200 and r.content.lstrip().startswith(b"{"):
                self.log.warning(
                    " + Certificate endpoint returned JSON error (not a cert): %s",
                    r.content[:200]
                )
        except Exception as e:
            self.log.warning(" + Could not fetch service certificate: %s – falling back to common cert", e)


        # Fall back to None → dl.py will use the common_privacy_cert
        return None

    def license(self, challenge: bytes, title: Title = None, track: Track = None, **_) -> bytes:
        self.log.debug(" + License Request Challenge (B64): %s", base64.b64encode(challenge).decode())
        
        # Check if manifest URL is from Lionsgate Play
        manifest_url = ""
        if title and title.service_data:
            manifest_url = title.service_data.get("manifest_url") or title.service_data.get("file_url") or ""
        if not manifest_url and track and hasattr(track, "original_url"):
            manifest_url = track.original_url or ""
            
        is_lionsgate = "lionsgateplay" in manifest_url.lower()
        
        if is_lionsgate:
            # Build PallyCon V2 token for Lionsgate DRM
            pallycon_token = None
            if title and title.service_data:
                access_key   = title.service_data.get("pallycon_access_key")
                secret_key   = title.service_data.get("pallycon_secret_key")
                content_uuid = title.service_data.get("pallycon_content_uuid")
                license_url  = title.service_data.get("pallycon_license_url") or "https://li-drm-lb.lionsgateplay.com/drm/lgin_prasar_bharti"
                if access_key and secret_key and content_uuid:
                    try:
                        pallycon_token = _generate_pallycon_token(content_uuid, access_key, secret_key)
                        self.log.info(" + Generated PallyCon V2 token for content: %s", content_uuid)
                    except Exception as e:
                        self.log.warning(" + Could not generate PallyCon token: %s", e)
                else:
                    self.log.warning(
                        " + Missing PallyCon credentials for Lionsgate DRM.\n"
                        "   Re-run with --waves-id <numeric_id> to auto-fetch credentials,\n"
                        "   e.g.: vt dl --keys Waves --waves-id 24458 \"<mpd_url>\""
                    )
                    license_url = "https://li-drm-lb.lionsgateplay.com/drm/lgin_prasar_bharti"
            else:
                license_url = "https://li-drm-lb.lionsgateplay.com/drm/lgin_prasar_bharti"
            self.log.info(" + Routing license request to Lionsgate Play DRM server: %s", license_url)
        else:
            license_url = self.config["endpoints"]["license"]
            pallycon_token = None
            # Re-fetch a fresh DRM token immediately before sending the license
            # challenge.  The cert request (above) may have consumed the previous
            # single-use token, causing PallyCon to return error 7139 when the
            # same stale token is presented for the actual license request.
            if title and title.service_data:
                vdc_id  = title.service_data.get("vdcId")
                media_id = title.service_data.get("mediaId")
                if vdc_id and media_id:
                    fresh = self._refresh_drm_token(vdc_id, media_id)
                    if fresh:
                        self.token = fresh

        headers = {
            "Content-Type": "application/octet-stream",
            "Origin": "https://www.wavespb.com",
            "Referer": "https://www.wavespb.com/",
        }
        # Add PallyCon token if available (covers both Waves own DRM and Lionsgate partner DRM)
        if pallycon_token:
            headers["pallycon-customdata-v2"] = pallycon_token
        elif hasattr(self, "token") and self.token:
            headers["pallycon-customdata-v2"] = self.token
            
        self.log.debug(" + License Request Headers: %s", headers)

        try:
            if is_lionsgate:
                # For Lionsgate: first try with any configured proxy (ALL_PROXY / HTTPS_PROXY),
                # then fall back to curl_cffi Chrome impersonation which uses a different
                # network stack and may bypass ISP-level TCP blocks.
                import os
                proxy = (
                    os.environ.get("ALL_PROXY")
                    or os.environ.get("HTTPS_PROXY")
                    or os.environ.get("https_proxy")
                    or os.environ.get("all_proxy")
                )
                proxies = {"https": proxy, "http": proxy} if proxy else None
                try:
                    r = self.session.post(
                        url=license_url,
                        data=challenge,
                        headers=headers,
                        proxies=proxies,
                        timeout=30,
                    )
                except Exception as req_err:
                    self.log.warning(" + Standard request failed (%s), retrying with curl_cffi...", req_err)
                    try:
                        from curl_cffi import requests as cffi_requests
                        cffi_headers = dict(headers)
                        r = cffi_requests.post(
                            url=license_url,
                            data=challenge,
                            headers=cffi_headers,
                            impersonate="chrome120",
                            proxies={"https": proxy} if proxy else None,
                            timeout=30,
                        )
                    except Exception as cffi_err:
                        self.log.error(" + curl_cffi also failed: %s", cffi_err)
                        self.log.error(
                            " + The Lionsgate DRM server is unreachable from your network.\n"
                            "   Set $env:ALL_PROXY='socks5://127.0.0.1:1080' with a working\n"
                            "   SOCKS5 proxy, or connect to a mobile hotspot and try again."
                        )
                        raise req_err  # raise original error
            else:
                r = self.session.post(
                    url=license_url,
                    data=challenge,
                    headers=headers
                )
            self.log.debug(" + License Response Status: %s", r.status_code)
            self.log.debug(" + License Response Content: %s", r.text)
        except Exception as e:
            if hasattr(e, "response") and e.response is not None:
                self.log.error(" + License Response Status: %s", e.response.status_code)
                self.log.error(" + License Response Content: %s", e.response.text)
            raise e
        
        if r.status_code != 200:
            raise ConnectionError(
                f"License server returned status code {r.status_code}: {r.content}"
            )
        return r.content


def _hls_download(self, out, name=None, headers=None, proxy=None):
    import os
    import re
    import requests
    import shutil
    import subprocess

    os.makedirs(out, exist_ok=True)
    re_name = (name or "{type}_{id}_{enc}").format(
        type="AudioTrack",
        id=self.id,
        enc="dec"
    )
    save_name = re_name + ".ts"
    save_path = os.path.join(out, save_name)

    if not self.key:
        raise ValueError("No content key found for HLS decryption.")

    key_bytes = bytes.fromhex(self.key)
    key_filename = f"key_{self.id}.bin"
    key_path = os.path.join(out, key_filename)
    with open(key_path, "wb") as f:
        f.write(key_bytes)

    # Fetch HLS variant playlist
    proxies = {"all": proxy} if proxy else None
    r = requests.get(self.url, headers=headers, proxies=proxies)
    r.raise_for_status()
    playlist_content = r.text

    from urllib.parse import urljoin
    # Rewrite #EXT-X-KEY and resolve relative URIs
    lines = []
    for line in playlist_content.splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            lines.append(line)
            continue
        if line_stripped.startswith("#EXT-X-KEY"):
            iv_match = re.search(r",IV=(0x[0-9a-fA-F]+)", line_stripped)
            iv_str = f",IV={iv_match.group(1)}" if iv_match else ""
            line = f'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="{key_filename}"{iv_str}'
        elif line_stripped.startswith("#EXT-X-MAP:URI="):
            map_match = re.search(r'URI="([^"]+)"', line_stripped)
            if map_match:
                abs_map_uri = urljoin(self.url, map_match.group(1))
                line = line.replace(map_match.group(1), abs_map_uri)
        elif not line_stripped.startswith("#"):
            if not (line_stripped.startswith("http://") or line_stripped.startswith("https://")):
                line = urljoin(self.url, line_stripped)
        lines.append(line)
    modified_playlist = "\n".join(lines)

    playlist_filename = f"playlist_{self.id}.m3u8"
    playlist_path = os.path.join(out, playlist_filename)
    with open(playlist_path, "w", encoding="utf-8") as f:
        f.write(modified_playlist)

    # Run ffmpeg to download and decrypt the HLS playlist, mapping only audio
    executable = shutil.which("ffmpeg")
    if not executable:
        raise EnvironmentError("ffmpeg executable not found...")

    # We need headers string
    headers_list = []
    if headers:
        for k, v in headers.items():
            headers_list.append(f"{k}: {v}")
    headers_str = "\r\n".join(headers_list) + "\r\n"

    arguments = [
        executable,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-protocol_whitelist", "file,crypto,data,http,https,tcp,tls",
        "-allowed_extensions", "ALL",
        "-i", playlist_path,
        "-map", "0:a",      # Only extract audio stream
        "-c:a", "copy",     # Copy audio track without transcoding
        save_path
    ]

    env = os.environ.copy()
    if proxy:
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy

    try:
        print("Downloading & Decrypting HLS SAMPLE-AES stream via FFmpeg...")
        subprocess.run(arguments, env=env, check=True)
    finally:
        # Clean up temp files
        if os.path.exists(playlist_path):
            try:
                os.remove(playlist_path)
            except:
                pass
        if os.path.exists(key_path):
            try:
                os.remove(key_path)
            except:
                pass

    self.encrypted = False
    self._location = save_path
    return save_path

