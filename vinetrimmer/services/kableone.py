from __future__ import annotations

import binascii
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import click
import m3u8
from langcodes import Language

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.objects.tracks import Track
from vinetrimmer.services.BaseService import BaseService


S_BOX = [
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
]
INV_S_BOX = [0] * 256
for i, value in enumerate(S_BOX):
    INV_S_BOX[value] = i


class KableOne(BaseService):
    """
    Service code for KableOne (https://www.kableone.com).

    Authorization: Cookies
    Security: AES-128 HLS signed URLs
    """
    ALIASES = ["KABLEONE", "kableone", "KableOne"]
    GEOFENCE = []
    TITLE_RE = [
        r"^https?://(?:www\.)?kableone\.com/(?:Movies|Webseries)/(?:Moviecontent|Webseriescontent)/(?P<id>\d+)(?:[/?#].*)?$",
        r"^https?://(?:www\.)?kableone\.com/Watch/Content/(?P<id>\d+)(?:[/?#].*)?$",
        r"^https?://(?:www\.)?kableone\.com/Watch/Play\?(?:.*&)?id=(?P<id>\d+)(?:[/?#].*)?$",
        r"^(?:movie|episode):(?P<id>\d+)$",
        r"^series:(?P<id>\d+)$",
        r"^(?P<id>\d+)$",
    ]

    # User-Agents for platforms that receive higher-bitrate manifests from KableOne's backend
    PLATFORM_UA = {
        "ios": (
            "KableOne/3.2.1 CFNetwork/1568.200.51 Darwin/24.1.0"
        ),
        "android": (
            "Dalvik/2.1.0 (Linux; U; Android 14; Pixel 8 Build/AD1A.240905.004) "
            "KableOne/3.2.1"
        ),
        "androidtv": (
            "Dalvik/2.1.0 (Linux; U; Android 12; SHIELD Android TV Build/SQ3A.220705.003.A1) "
            "KableOne/3.2.1 (AndroidTV)"
        ),
    }

    @staticmethod
    @click.command(name="KableOne", short_help="https://www.kableone.com")
    @click.argument("title", type=str)
    @click.option("--series", is_flag=True, default=False, help="Download every published episode from this webseries.")
    @click.option("--manifest-lang", type=str, default=None, help="Force master_<lang>.m3u8, for example pa or hi.")
    @click.option(
        "--platform",
        type=click.Choice(["ios", "android", "androidtv"], case_sensitive=False),
        default=None,
        help="Spoof app platform UA to fetch highest-bitrate manifest (ios / android / androidtv).",
    )
    @click.pass_context
    def cli(ctx, **kwargs):
        return KableOne(ctx, **kwargs)

    def __init__(self, ctx, title: str, series: bool = False, manifest_lang: str | None = None, platform: str | None = None):
        self.title = title
        super().__init__(ctx)
        parsed = self.parse_title(ctx, title) or {}

        self.media_id = parsed.get("id") or self.title
        self.series_id = self.media_id if str(title).lower().startswith("series:") else None
        self.download_series = bool(series or self.series_id)
        self.manifest_lang = (manifest_lang or "").lower() or None
        self.platform = (platform or "").lower() or None
        self.wanted = ctx.parent.params.get("wanted")

        self._setup_headers()

    def _setup_headers(self):
        self.session.headers.update(self.config.get("headers") or {})
        self.session.headers.update({
            "Origin": self.config["endpoints"]["site"],
            "Referer": f"{self.config['endpoints']['site']}/",
        })

    def get_titles(self):
        if self.download_series:
            return self._get_series_titles()
        return self._title_from_detail(self._media_details(self.media_id))

    def _get_series_titles(self):
        if not self.series_id:
            detail = self._media_details(self.media_id)
            item = detail.get("mediaDetails", {}).get("detail") or {}
            self.series_id = item.get("webSeriesID") or item.get("webSeriesId")
        if not self.series_id:
            self.log.exit(" - This KableOne item does not expose a webSeriesID")

        data = self._webseries_details(self.series_id)
        details = data.get("webseriesDetails") or {}
        series = details.get("detail") or {}
        series_name = series.get("webseriesName") or f"KableOne-{self.series_id}"
        titles = []
        for season_index, season in enumerate(details.get("seasons") or [], start=1):
            season_number = _number_from_text(season.get("seasonName"), season_index)
            for episode_index, episode in enumerate(season.get("episodes") or [], start=1):
                if not episode.get("isMediaPublished", True):
                    continue
                ep_season, ep_number = _episode_numbers(episode, season_number, episode_index)
                if self.wanted and f"{ep_season}x{ep_number}" not in self.wanted:
                    continue
                titles.append(Title(
                    id_=str(episode.get("mediaID") or ""),
                    type_=Title.Types.TV,
                    name=series_name,
                    year=int(episode.get("releasedYear") or 0),
                    season=ep_season,
                    episode=ep_number,
                    episode_name=episode.get("title"),
                    original_lang=_first_language(episode.get("cultures")) or "pa",
                    source=self.ALIASES[0],
                    service_data=episode,
                ))
        if not titles:
            self.log.exit(" - No KableOne episodes matched")
        return titles

    def _title_from_detail(self, details):
        item = details.get("mediaDetails", {}).get("detail") or {}
        if not item:
            self.log.exit(f" - KableOne returned empty metadata for {self.media_id}")
        is_tv = (item.get("category") or "").lower() == "web series"
        if is_tv:
            season, episode = _episode_numbers(item, 1, 1)
            return Title(
                id_=str(item.get("mediaID") or self.media_id),
                type_=Title.Types.TV,
                name=item.get("webSeriesName") or item.get("title") or f"KableOne-{self.media_id}",
                year=int(item.get("releasedYear") or 0),
                season=season,
                episode=episode,
                episode_name=item.get("title"),
                original_lang=_first_language(item.get("cultures")) or "pa",
                source=self.ALIASES[0],
                service_data=item,
            )
        return Title(
            id_=str(item.get("mediaID") or self.media_id),
            type_=Title.Types.MOVIE,
            name=item.get("title") or f"KableOne-{self.media_id}",
            year=int(item.get("releasedYear") or 0),
            original_lang=_first_language(item.get("cultures")) or "pa",
            source=self.ALIASES[0],
            service_data=item,
        )

    def get_tracks(self, title):
        manifest = self._m3u8_url(title.id)
        text = self.session.get(manifest, timeout=60).text
        master = m3u8.loads(text, uri=manifest)
        tracks = Tracks.from_m3u8(master, source=self.ALIASES[0])

        for track in tracks.videos + tracks.audios + tracks.subtitles:
            track.original_url = None
            track.encrypted = False

        for subtitle in tracks.subtitles:
            subtitle.url = self._expand_subtitle_playlist(subtitle.url, manifest)
            subtitle.descriptor = Track.Descriptor.URL
            subtitle.original_url = None

        return tracks

    def license(self, *_args, **_kwargs):
        return None

    def refresh_tracks(self, title):
        manifest = self._m3u8_url(title.id)
        for track in title.tracks:
            track.original_url = None
            if isinstance(track.url, list):
                track.url = [self._update_query(u, manifest) for u in track.url]
            elif isinstance(track.url, str):
                track.url = self._update_query(track.url, manifest)

    def _update_query(self, url, new_master_url):
        new_query = urlsplit(new_master_url).query
        if not new_query:
            return url
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))

    def _media_details(self, media_id):
        return self.session.get(
            self.config["endpoints"]["related_items"],
            params={"mediaId": media_id},
            timeout=30,
        ).json()

    def _webseries_details(self, series_id):
        return self.session.get(
            self.config["endpoints"]["webseries_details"],
            params={"webSeriesID": series_id},
            timeout=30,
        ).json()

    def _m3u8_url(self, media_id):
        self.session.headers.update({"Referer": f"{self.config['endpoints']['site']}/Watch/Play?id={media_id}"})

        original_ua = self.session.headers.get("User-Agent")
        platform_ua = self.PLATFORM_UA.get(self.platform) if self.platform else None

        # At most 2 attempts: requested platform UA first, then web UA as fallback.
        # Avoid spraying multiple UAs in the same session — servers track UA per session
        # and will reject or return non-JSON once they detect rapid UA switching.
        attempts = [(self.platform, platform_ua), ("web", original_ua)] if platform_ua else [("web", original_ua)]

        payload = None
        for label, ua in attempts:
            if ua:
                self.session.headers.update({"User-Agent": ua})
            try:
                resp = self.session.get(
                    self.config["endpoints"]["vod"],
                    params={"id": media_id, "isTrailer": "false", "_t": int(time.time() * 1000)},
                    timeout=30,
                )
                body = resp.text.strip()
                if not body:
                    self.log.warning(f" ! [{label}] VOD returned empty body (HTTP {resp.status_code}) — skipping")
                    continue
                try:
                    data = resp.json()
                except Exception:
                    # Not JSON — log a snippet so the caller can see what the server actually said
                    self.log.warning(
                        f" ! [{label}] VOD returned non-JSON (HTTP {resp.status_code}): {body[:300]!r} — skipping"
                    )
                    continue
                if not data.get("url"):
                    self.log.warning(f" ! [{label}] VOD payload missing 'url' key: {data} — skipping")
                    continue
                if label != "web":
                    self.log.info(f" + High-bitrate manifest obtained via {label} UA")
                payload = data
                break
            finally:
                # Restore original UA after every attempt so the session stays consistent
                if original_ua is not None:
                    self.session.headers.update({"User-Agent": original_ua})
                else:
                    self.session.headers.pop("User-Agent", None)

        if not payload:
            self.log.exit(" - KableOne stream endpoint did not return a URL")

        url = _decrypt_url(payload["url"])
        if self.manifest_lang:
            url = re.sub(r"master_[a-z]{2}\.m3u8", f"master_{self.manifest_lang}.m3u8", url, count=1)
        return url

    def _expand_subtitle_playlist(self, playlist_url, master_url):
        playlist_url = _with_master_query(playlist_url, master_url)
        text = self.session.get(playlist_url, timeout=60).text
        playlist = m3u8.loads(text, uri=playlist_url)
        segments = []
        for segment in playlist.segments:
            uri = urljoin(segment.base_uri or playlist_url, segment.uri)
            segments.append(_with_master_query(uri, master_url))
        return segments or playlist_url

    def _probe_fps(self, master, master_url):
        try:
            top_pl = master.playlists[0] if master.playlists else None
            if not top_pl:
                return None
            pl_url = _with_master_query(top_pl.absolute_uri, master_url)
            pl_text = self.session.get(pl_url, timeout=10).text
            pl_parsed = m3u8.loads(pl_text, uri=pl_url)
            if not pl_parsed.segments:
                return None

            first_seg_uri = _with_master_query(pl_parsed.segments[0].absolute_uri, pl_url)
            key_bytes = None
            if pl_parsed.keys and pl_parsed.keys[0] and pl_parsed.keys[0].uri:
                key_uri = _with_master_query(pl_parsed.keys[0].absolute_uri, pl_url)
                key_bytes = self.session.get(key_uri, timeout=10).content

            seg_resp = self.session.get(first_seg_uri, headers={"Range": "bytes=0-262143"}, timeout=10)
            seg_data = seg_resp.content
            if key_bytes and len(key_bytes) == 16:
                from Crypto.Cipher import AES
                enc_len = (len(seg_data) // 16) * 16
                if enc_len > 0:
                    cipher = AES.new(key_bytes, AES.MODE_CBC, iv=b"\x00" * 16)
                    seg_data = cipher.decrypt(seg_data[:enc_len])

            from vinetrimmer.utils.subprocess import ffprobe
            probe = ffprobe(seg_data)
            if probe and "streams" in probe:
                for st in probe["streams"]:
                    if st.get("codec_type") == "video":
                        r_fps = st.get("r_frame_rate") or st.get("avg_frame_rate")
                        if r_fps and "/" in r_fps:
                            num, den = r_fps.split("/")
                            if int(den) > 0:
                                return round(int(num) / int(den), 3)
        except Exception as e:
            self.log.debug(f"Failed to dynamically probe FPS: {e}")
        return None


def _decrypt_url(encrypted_hex_or_js):
    if "CryptoJS" in encrypted_hex_or_js:
        hex_strings = re.findall(r"'([a-fA-F0-9]{32,})'", encrypted_hex_or_js)
        if not hex_strings:
            raise ValueError("Could not find ciphertext hex string in JS payload")
        ciphertext_hex = max(hex_strings, key=len)
        
        key_match = re.search(r"CryptoJS\.enc\.Utf8\.parse\(\s*'([^']+)'\s*\)", encrypted_hex_or_js)
        if not key_match:
            raise ValueError("Could not find CryptoJS key in JS payload")
        key = key_match.group(1).encode("utf-8")
        
        encrypted = binascii.unhexlify(ciphertext_hex)
    else:
        key = b"11A1764225B11AA1"
        encrypted = binascii.unhexlify(encrypted_hex_or_js.strip())
        
    return _decrypt_aes_128_ecb(encrypted, key).rstrip(b"\x00").decode("utf-8")



def _xtime(value):
    return ((value << 1) ^ 0x1B) & 0xFF if value & 0x80 else value << 1


def _aes_mul(a, b):
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


def _expand_key(key):
    expanded = list(key)
    rcon = 1
    while len(expanded) < 176:
        temp = expanded[-4:]
        if len(expanded) % 16 == 0:
            temp = temp[1:] + temp[:1]
            temp = [S_BOX[x] for x in temp]
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        for x in temp:
            expanded.append(expanded[-16] ^ x)
    return expanded


def _decrypt_aes_128_ecb(data, key):
    expanded = _expand_key(key)
    output = bytearray()
    for offset in range(0, len(data), 16):
        state = list(data[offset:offset + 16])
        _add_round_key(state, expanded[160:176])
        for round_index in range(9, 0, -1):
            _inv_shift_rows(state)
            _inv_sub_bytes(state)
            _add_round_key(state, expanded[16 * round_index:16 * (round_index + 1)])
            _inv_mix_columns(state)
        _inv_shift_rows(state)
        _inv_sub_bytes(state)
        _add_round_key(state, expanded[:16])
        output.extend(state)
    return bytes(output)


def _add_round_key(state, round_key):
    for i in range(16):
        state[i] ^= round_key[i]


def _inv_sub_bytes(state):
    for i in range(16):
        state[i] = INV_S_BOX[state[i]]


def _inv_shift_rows(state):
    original = state[:]
    for row in range(4):
        for col in range(4):
            state[row + 4 * col] = original[row + 4 * ((col - row) % 4)]


def _inv_mix_columns(state):
    for col in range(4):
        i = 4 * col
        a0, a1, a2, a3 = state[i:i + 4]
        state[i] = _aes_mul(a0, 14) ^ _aes_mul(a1, 11) ^ _aes_mul(a2, 13) ^ _aes_mul(a3, 9)
        state[i + 1] = _aes_mul(a0, 9) ^ _aes_mul(a1, 14) ^ _aes_mul(a2, 11) ^ _aes_mul(a3, 13)
        state[i + 2] = _aes_mul(a0, 13) ^ _aes_mul(a1, 9) ^ _aes_mul(a2, 14) ^ _aes_mul(a3, 11)
        state[i + 3] = _aes_mul(a0, 11) ^ _aes_mul(a1, 13) ^ _aes_mul(a2, 9) ^ _aes_mul(a3, 14)


def _number_from_text(value, default):
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else int(default)


def _episode_numbers(item, season_default, episode_default):
    marker = item.get("smiSubtitleUrl") or ""
    match = re.search(r"S\s*(\d+)\s*E\s*(\d+)", marker, re.IGNORECASE)
    if match:
        return int(match.group(1)), int(match.group(2))
    return int(season_default), int(episode_default)


def _first_language(value):
    value = (value or "").split(",", 1)[0].strip()
    if not value:
        return None
    try:
        return str(Language.find(value))
    except Exception:
        try:
            return str(Language.get(value))
        except Exception:
            return None


def _audio_codec(master):
    for playlist in master.playlists:
        for codec in (playlist.stream_info.codecs or "").split(","):
            codec = codec.strip()
            if codec.split(".")[0].lower() in {"mp4a", "aac", "ac-3", "ec-3"}:
                return codec
    return None


def _with_master_query(url, master_url):
    master_query = urlsplit(master_url).query
    if not master_query:
        return url
    parsed = urlsplit(url)
    if parsed.query:
        return url
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, master_query, parsed.fragment))
