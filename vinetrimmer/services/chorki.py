import base64
import hashlib
import hmac
import json
import os
import re
import time
from hashlib import md5

import click
import m3u8
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from vinetrimmer.objects import AudioTrack, TextTrack, Title, Track, Tracks, VideoTrack
from vinetrimmer.services.BaseService import BaseService


class CHORKI(BaseService):
    """
    Service code for Chorki streaming service (https://www.chorki.com).
    """

    ALIASES = ["CHORKI", "chorki"]
    TITLE_RE = [
        r"^(?:https?://(?:www\.)?chorki\.(?:net|com)/(?:[a-z]{2}/)?(?:movie|series|video|videos|details)/)?(?P<id>[a-zA-Z0-9_-]+)",
    ]

    API_BASE = "https://api-static.chorki.net/v1"
    API_DYNAMIC = "https://api-dynamic.chorki.net/v1"

    @staticmethod
    @click.command(name="CHORKI", short_help="https://www.chorki.net")
    @click.argument("title", type=str, required=False)
    @click.option("-m", "--movie", is_flag=True, default=False, help="Title is a movie.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return CHORKI(ctx, **kwargs)

    def __init__(self, ctx, title, movie):
        super().__init__(ctx)
        self.parse_title(ctx, title)

        if "movie" in str(title).lower():
            self.movie = True
        else:
            self.movie = movie

        self.profile = ctx.obj.profile
        self.auth_token = self.get_auth_token()

        self.session.headers.update({
            "accept": "application/json",
            "authorization": f"Bearer {self.auth_token['accessToken']}",
            "origin": "https://www.chorki.net",
            "referer": "https://www.chorki.net/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
        })

    def get_titles(self):
        titles = []
        params = {"country": "IN", "platform": "web", "language": "en"}

        slug = self.title.split("/")[-1]

        # Check if movie
        if self.movie:
            r = self.session.get(f"{self.API_BASE}/movie/{slug}", params=params)
            if r.status_code != 200:
                self.log.exit(f"Failed to load movie details for {slug}: {r.text}")

            data = r.json()["result"]["data"]
            titles.append(Title(
                id_=str(data["id"]),
                type_=Title.Types.MOVIE,
                name=data.get("title") or slug,
                year=data.get("release_date", "")[:4] or "2023",
                original_lang="Bn",
                source=self.ALIASES[0],
                service_data=data
            ))
        else:
            # Series handling: fetch main series details to get series title
            r_series = self.session.get(f"{self.API_BASE}/series/{slug}", params=params)
            series_name = slug
            if r_series.status_code == 200:
                series_data = r_series.json().get("result", {}).get("data", {})
                series_name = series_data.get("title") or slug

            r_seasons = self.session.get(f"{self.API_BASE}/series/{slug}/seasons", params=params)
            if r_seasons.status_code != 200:
                self.log.exit(f"Failed to load series seasons for {slug}: {r_seasons.text}")

            seasons = r_seasons.json().get("result", {}).get("data", {}).get("data", [])

            for s_idx, season in enumerate(seasons):
                sid = season["id"]
                snum = season.get("sequence") or season.get("season_number") or (s_idx + 1)

                ep_params = {"season_id": sid, "per_page": "50", **params}
                r_eps = self.session.get(f"{self.API_BASE}/series/{slug}/episodes", params=ep_params)
                if r_eps.status_code != 200:
                    continue

                eps = r_eps.json().get("result", {}).get("data", {}).get("data", [])
                for ep in eps:
                    ep_num = ep.get("episode_number") or 1
                    titles.append(Title(
                        id_=str(ep["id"]),
                        type_=Title.Types.TV,
                        name=series_name,
                        season=int(snum),
                        episode=int(ep_num),
                        episode_name=ep.get("title"),
                        original_lang="Bn",
                        source=self.ALIASES[0],
                        service_data=ep
                    ))

        return titles

    def get_cdn_hmac_secret(self) -> str:
        if hasattr(self, "_cdn_hmac_secret") and self._cdn_hmac_secret:
            return self._cdn_hmac_secret
        try:
            _K1 = bytes([71, 48, 84, 33, 80, 52, 84, 35, 95, 81, 51, 89])
            _K2 = bytes([71, 48, 84, 33, 80, 52, 84, 35, 95, 86, 76, 48, 67, 75])
            _F1 = bytes([121, 116, 105, 114, 117, 99, 101, 115]).decode("utf-8")
            _F2 = bytes([115, 114, 101, 121, 97, 108, 112, 95, 110, 100, 99, 95, 104, 116, 117, 97, 95, 110, 101, 107, 111, 116]).decode("utf-8")

            params = {"country": "IN", "platform": "web", "language": "en"}
            r = self.session.get(f"{self.API_BASE}/settings", params=params)
            if r.status_code == 200:
                encrypted_token = r.json().get("result", {}).get("data", {}).get(_F1, {}).get(_F2)
                if encrypted_token:
                    key = hashlib.sha256(_K1).hexdigest()[:32].encode("utf-8")
                    iv = hashlib.sha256(_K2).hexdigest()[:32].encode("utf-8")[:16]
                    ciphertext = base64.b64decode(encrypted_token)
                    cipher = AES.new(key, AES.MODE_CBC, iv)
                    secret = unpad(cipher.decrypt(ciphertext), AES.block_size).decode("utf-8")
                    self._cdn_hmac_secret = secret
                    return secret
        except Exception as e:
            self.log.warning(f" - Failed to decrypt: {e}")
        return ""

    def get_vpsid_headers(self, kms_url: str) -> dict:
        hmac_secret = self.get_cdn_hmac_secret()
        match = re.search(r"https?://[^/$]+(.*)", kms_url)
        pathname = match.group(1) if match else kms_url

        now = int(time.time())
        st_dt = time.gmtime(now - 300)
        et_dt = time.gmtime(now + 14400)  # 4 hours expiration window

        vst = time.strftime("%Y%m%d%H%M%S", st_dt)
        vet = time.strftime("%Y%m%d%H%M%S", et_dt)

        if hmac_secret:
            to_sign = f"{pathname}?st={vst}&et={vet}"
            vpsid = hmac.new(hmac_secret.encode("utf-8"), to_sign.encode("utf-8"), hashlib.sha1).hexdigest()[:20]
        else:
            vpsid = md5(str(now).encode()).hexdigest()[:20]

        return {"vst": vst, "vet": vet, "vpsid": vpsid}

    def get_tracks(self, title: Title) -> Tracks:
        manifest_url = title.service_data.get("url")
        if not manifest_url:
            self.log.exit("No streaming URL found in content details")

        master = m3u8.load(manifest_url)
        tracks = Tracks()

        if master.playlists:
            first_pl = master.playlists[0]
            var_uri = ("" if re.match(r"^https?://", first_pl.uri) else (first_pl.base_uri or "")) + first_pl.uri
            try:
                r_var = self.session.get(var_uri)
                if r_var.status_code == 200:
                    km = re.search(r'#EXT-X-KEY:METHOD=AES-128,URI="([^"]+)"', r_var.text)
                    if km:
                        kms_url = km.group(1)
                        v_headers = self.get_vpsid_headers(kms_url)
                        self.session.headers.update(v_headers)
            except Exception as e:
                self.log.warning(f" - Failed to extract: {e}")

        # Map audio bitrates and codecs from master playlists
        audio_bitrate_map = {}
        audio_codec_map = {}
        for pl in master.playlists:
            if pl.stream_info:
                b = pl.stream_info.average_bandwidth or pl.stream_info.bandwidth
                if not pl.stream_info.resolution:
                    if pl.uri:
                        if b:
                            audio_bitrate_map[pl.uri] = b
                        if pl.stream_info.codecs:
                            audio_codec_map[pl.uri] = pl.stream_info.codecs
                elif pl.stream_info.codecs:
                    for c in pl.stream_info.codecs.split(","):
                        c_clean = c.strip().lower()
                        if "mp4a" in c_clean or "aac" in c_clean:
                            if pl.stream_info.audio:
                                audio_codec_map[pl.stream_info.audio] = "aac"
                                if b:
                                    audio_bitrate_map[pl.stream_info.audio] = b

        # Build Video Tracks (only for playlists with resolution)
        for x in master.playlists:
            if not x.stream_info or not x.stream_info.resolution:
                continue
            uri = ("" if re.match(r"^https?://", x.uri) else (x.base_uri or "")) + x.uri
            codecs_str = x.stream_info.codecs or "avc1"
            codec = codecs_str.split(",")[0].split(".")[0]
            tracks.add(
                VideoTrack(
                    id_=md5(str(x).encode()).hexdigest()[:7],
                    source=self.ALIASES[0],
                    url=uri,
                    codec=codec,
                    language=None,
                    bitrate=x.stream_info.average_bandwidth or x.stream_info.bandwidth,
                    width=x.stream_info.resolution[0],
                    height=x.stream_info.resolution[1],
                    fps=x.stream_info.frame_rate,
                    descriptor=Track.Descriptor.M3U,
                    extra=x
                )
            )

        # Build Audio Tracks
        for x in master.media:
            if x.type == "AUDIO":
                uri = ("" if re.match(r"^https?://", x.uri or "") else (x.base_uri or "")) + (x.uri or "")
                bitrate = (
                    audio_bitrate_map.get(uri) or
                    audio_bitrate_map.get(x.uri) or
                    audio_bitrate_map.get(x.group_id) or
                    audio_bitrate_map.get(x.name) or
                    128000
                )
                raw_codec = audio_codec_map.get(x.uri) or audio_codec_map.get(x.group_id) or x.group_id or "aac"
                raw_codec_lower = str(raw_codec).lower()
                if "mp4a" in raw_codec_lower or "aac" in raw_codec_lower:
                    codec = "aac"
                elif "ec-3" in raw_codec_lower or "eac3" in raw_codec_lower:
                    codec = "eac3"
                elif "ac-3" in raw_codec_lower or "ac3" in raw_codec_lower:
                    codec = "ac3"
                else:
                    codec = "aac"

                tracks.add(
                    AudioTrack(
                        id_=md5(str(x).encode()).hexdigest()[:6],
                        source=self.ALIASES[0],
                        url=uri,
                        codec=codec,
                        language=x.language or "bn",
                        bitrate=bitrate,
                        channels=x.channels,
                        descriptor=Track.Descriptor.M3U,
                        extra=x
                    )
                )

        # Build Subtitles
        subtitles = title.service_data.get("subtitles") or []
        for sub in subtitles:
            sub_url = sub.get("url")
            sub_lang = sub.get("language") or "en"
            if sub_url:
                tracks.add(
                    TextTrack(
                        id_=md5(sub_url.encode()).hexdigest()[:6],
                        source=self.ALIASES[0],
                        url=sub_url,
                        codec="vtt",
                        language=sub_lang,
                        forced=False,
                        sdh=False
                    )
                )

        return tracks

    def get_chapters(self, title):
        return []

    # Service specific functions

    def get_auth_token(self):
        token_cache_path = self.get_cache("token_{profile}.json".format(profile=self.profile))
        if os.path.isfile(token_cache_path):
            try:
                with open(token_cache_path, encoding="utf-8") as fd:
                    token = json.load(fd)
                if token.get("accessToken"):
                    self.log.info(" + Using cached auth tokens...")
                    return token
            except Exception:
                pass

        if not self.credentials or not getattr(self.credentials, "username", None):
            try:
                from vinetrimmer.config import credentials as global_credentials
                raw_cred = global_credentials.get("CHORKI") or global_credentials.get("Chorki") or global_credentials.get("chorki")
                if raw_cred:
                    from vinetrimmer.objects import Credential
                    self.credentials = Credential.loads(raw_cred) if isinstance(raw_cred, str) else Credential(*raw_cred)
            except Exception:
                pass

        if self.credentials and getattr(self.credentials, "token", None):
            token = {"accessToken": self.credentials.token.replace("Bearer ", "").strip()}
            return self.save_auth_token(token, token_cache_path)

        # get new token
        self.log.info(" + Logging into Chorki")
        token = self.login()
        return self.save_auth_token(token, token_cache_path)

    @staticmethod
    def save_auth_token(token, to):
        os.makedirs(os.path.dirname(to), exist_ok=True)
        with open(to, "w", encoding="utf-8") as fd:
            json.dump(token, fd, indent=2)
        return token

    def login(self):
        if self.credentials and getattr(self.credentials, "username", None) and getattr(self.credentials, "password", None):
            params = {"country": "IN", "platform": "web", "language": "en"}
            headers = {
                "accept": "application/json",
                "content-type": "application/json",
                "origin": "https://www.chorki.net",
                "referer": "https://www.chorki.net/",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
            }

            r = self.session.post(
                "https://api-dynamic.chorki.net/v3/auth/login",
                params=params,
                headers=headers,
                json={"email": self.credentials.username, "password": self.credentials.password},
            )
            if r.status_code in (200, 201):
                res = r.json()
                token = res.get("result", {}).get("token") or res.get("token")
                if token:
                    return {"accessToken": token}
                else:
                    self.log.warning(f" - Token not found in login response: {r.text}")
            else:
                self.log.warning(f" - Credentials login failed ({r.status_code}): {r.text}")

        token_input = click.prompt("Enter Chorki Bearer Token (from browser request headers)", type=str)
        token_str = token_input.strip().replace("Bearer ", "")

        if not token_str:
            raise self.log.exit("- No Bearer Token provided.")

        return {"accessToken": token_str}