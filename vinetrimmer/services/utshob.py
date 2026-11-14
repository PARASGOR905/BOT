from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urljoin

import click
import m3u8
from Cryptodome.Cipher import AES
from langcodes import Language

from vinetrimmer.objects import Title, Tracks
from vinetrimmer.services.BaseService import BaseService


_KMS_KEY_SEED = "G0T!P4T#_Q3Y"
_KMS_IV_SEED = "G0T!P4T#_VL0CK"

_TOKEN_FIELD_RE = re.compile(
    r'\\?"sreyalp_ndc_htua_nekot\\?"\s*:\s*\\?"([^"\\]+)\\?"'
)


def _aes_cbc_decrypt_token(token_b64: str) -> str:
    key = hashlib.sha256(_KMS_KEY_SEED.encode()).hexdigest()[:32].encode()
    iv = hashlib.sha256(_KMS_IV_SEED.encode()).hexdigest()[:32].encode()
    cipher = AES.new(key, AES.MODE_CBC, iv[:16])
    pt = cipher.decrypt(base64.b64decode(token_b64))
    pad = pt[-1]
    if 1 <= pad <= 16 and all(b == pad for b in pt[-pad:]):
        pt = pt[:-pad]
    return pt.decode("utf-8", errors="replace")


def _aes_cbc_decrypt_segment(data: bytes, key: bytes, iv: bytes) -> bytes:
    pt = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    pad = pt[-1]
    if 1 <= pad <= 16 and all(b == pad for b in pt[-pad:]):
        pt = pt[:-pad]
    return pt


def _make_kms_headers(url: str, secret_key: str) -> dict[str, str]:
    m = re.match(r"(https?://.*?)([/$].*)", url)
    path_with_query = m.group(2) if m else (urlsplit(url).path or "/")

    vst_date = datetime.now(timezone.utc) - timedelta(minutes=5)
    vet_date = vst_date + timedelta(minutes=10)
    vst = vst_date.strftime("%Y%m%d%H%M%S")
    vet = vet_date.strftime("%Y%m%d%H%M%S")
    payload = f"{path_with_query}?st={vst}&et={vet}"
    vpsid = hmac.new(
        secret_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()[:20]
    return {"vpsid": vpsid, "vst": vst, "vet": vet}


class Utshob(BaseService):
    """
    Service code for Utshob (https://utshob.live).

    \b
    Version: 1.0.0
    Authorization: None
    Security: AES-128 encrypted HLS + KMS-protected key URIs
    """

    ALIASES = ["UTSHOB", "utshob", "Utshob"]
    TITLE_RE = r"^(?:https?://(?:www\.)?utshob\.live/)?(?P<path>(?:movies?|series?)/[^?#]+)"

    PARALLEL_SEGMENTS = 16
    DEFAULT_AUDIO_LANG = "bn"

    @staticmethod
    @click.command(name="Utshob", short_help="https://utshob.live")
    @click.argument("title", type=str, required=False)
    @click.option(
        "-l", "--lang", "--audio-lang",
        default=None,
        help="Audio language (default: bn).",
    )
    @click.pass_context
    def cli(ctx, **kwargs):
        return Utshob(ctx, **kwargs)

    def __init__(self, ctx, title, lang=None):
        global_alang = ctx.parent.params.get("alang") if (ctx and ctx.parent) else None
        if isinstance(global_alang, list) and global_alang and global_alang != ["orig"]:
            fallback = str(global_alang[0])
        else:
            fallback = self.DEFAULT_AUDIO_LANG
        self.audio_lang = lang or fallback or self.DEFAULT_AUDIO_LANG

        super().__init__(ctx)
        self._parse_title(title)
        self._kms_secret: str | None = None

    def _parse_title(self, title: str):
        if not title:
            self.title = None
            return
        m = re.match(self.TITLE_RE, title.strip())
        self.title = m.group("path").strip("/") if m else title.strip("/")

    def _safe_language(self) -> Language:
        code = (self.audio_lang or self.DEFAULT_AUDIO_LANG).strip() or self.DEFAULT_AUDIO_LANG
        try:
            return Language.get(code)
        except Exception:
            return Language.get("bn")

    # ------------------------------------------------------------------ #
    #  KMS secret
    # ------------------------------------------------------------------ #

    def _extract_kms_secret(self, html: str) -> str | None:
        m = _TOKEN_FIELD_RE.search(html)
        if not m:
            self.log.warning(" - Could not find sreyalp_ndc_htua_nekot in page")
            return None
        try:
            secret = _aes_cbc_decrypt_token(m.group(1))
        except Exception as e:
            self.log.warning(f" - Failed to decrypt KMS token: {e}")
            return None
        if not secret:
            self.log.warning(" - Decrypted KMS secret is empty")
            return None
        self.log.info(" + KMS secret derived from page token")
        return secret

    # ------------------------------------------------------------------ #
    #  Titles
    # ------------------------------------------------------------------ #

    def get_titles(self):
        if not self.title:
            raise self.log.exit(" - No title provided.")

        api_slug = self.title.strip("/")
        matched_prefix = None
        for prefix in ("movies/", "movie/", "series/"):
            if api_slug.startswith(prefix):
                api_slug = api_slug[len(prefix):]
                matched_prefix = prefix.rstrip("/")
                break

        is_series = matched_prefix == "series"
        lang_obj = self._safe_language()

        page_url = (
            f"https://utshob.live/series/{api_slug}"
            if is_series
            else f"https://utshob.live/movies/{api_slug}"
        )

        self.log.info(f" + Fetching page: {page_url}")
        try:
            r = self.session.get(page_url)
            r.raise_for_status()
        except Exception as e:
            raise self.log.exit(f" - Failed to fetch page: {e}")

        html = r.text
        self._kms_secret = self._extract_kms_secret(html)

        try:
            with open("utshob_debug.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass

        title_name = api_slug.replace("-", " ").title()
        m = re.search(r"<title>([^<]+)</title>", html)
        if m:
            candidate = m.group(1).strip()
            if candidate and candidate.lower() != "utshob":
                title_name = candidate

        year = None
        m = re.search(r'"release_date"\s*:\s*"(\d{4})', html)
        if m:
            year = int(m.group(1))

        seen: set[str] = set()
        videos: list[dict] = []
        for v in self._iter_video_objects(html):
            key = v.get("slug") or v.get("id") or v.get("url")
            if not key or key in seen:
                continue
            seen.add(key)
            videos.append(v)

        if is_series and videos:
            titles: list[Title] = []
            for v in videos:
                ep_url = v.get("url") or ""
                if ".m3u8" not in ep_url:
                    continue
                ep_num = v.get("episode_number")
                if not isinstance(ep_num, int):
                    continue
                season = 1
                m = re.search(r"-season-(\d+)-", v.get("slug") or "")
                if m:
                    season = int(m.group(1))
                titles.append(Title(
                    id_=f"{api_slug}-s{season:02d}e{ep_num:02d}",
                    type_=Title.Types.TV,
                    name=title_name,
                    year=year,
                    season=season,
                    episode=ep_num,
                    episode_name=f"Episode {ep_num}",
                    original_lang=lang_obj,
                    source=self.ALIASES[0],
                    service_data={
                        "manifest": ep_url.rstrip("\\/\"', \t\r\n"),
                        "slug": api_slug,
                    },
                ))
            if titles:
                titles.sort(key=lambda t: (t.season or 1, t.episode or 0))
                self.log.info(f" + Found {len(titles)} episodes")
                return titles

        manifest = None
        for v in videos:
            url = v.get("url") or ""
            if ".m3u8" in url:
                manifest = url.rstrip("\\/\"', \t\r\n")
                break
        if not manifest:
            manifest = self._extract_manifest_regex(html)
        if not manifest:
            raise self.log.exit(" - Could not find a manifest URL")

        self.log.info(f" + Found manifest: {manifest}")

        return Title(
            id_=api_slug,
            type_=Title.Types.MOVIE,
            name=title_name,
            year=year,
            original_lang=lang_obj,
            source=self.ALIASES[0],
            service_data={"manifest": manifest, "slug": api_slug},
        )

    # ------------------------------------------------------------------ #
    #  RSC payload parsing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _iter_video_objects(html: str):
        push_re = re.compile(
            r'self\.__next_f\.push\(\s*\[\s*\d+\s*,\s*"((?:\\.|[^"\\])*)"\s*\]\s*\)',
            re.DOTALL,
        )
        for m in push_re.finditer(html):
            try:
                decoded = json.loads(f'"{m.group(1)}"')
            except json.JSONDecodeError:
                continue
            yield from Utshob._scan_balanced_objects(decoded)

    @staticmethod
    def _scan_balanced_objects(text: str):
        i, n = 0, len(text)
        while i < n:
            if text[i] != "{":
                i += 1
                continue
            depth, in_string, escape, start, j = 0, False, False, i, i
            while j < n:
                c = text[j]
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_string = not in_string
                elif not in_string:
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            break
                j += 1
            if j >= n or depth != 0:
                i = start + 1
                continue
            blob = text[start:j + 1]
            if '"object":"video"' in blob or '"episode_number"' in blob:
                try:
                    obj = json.loads(blob)
                    if isinstance(obj, dict):
                        yield obj
                except json.JSONDecodeError:
                    pass
            i = start + 1

    @staticmethod
    def _extract_manifest_regex(html: str) -> str | None:
        for pattern in (
            r'"url"\s*:\s*"(https?://[^"]+?\.m3u8[^"]*)"',
            r'"file"\s*:\s*"(https?://[^"]+?\.m3u8[^"]*)"',
            r'"source"\s*:\s*"(https?://[^"]+?\.m3u8[^"]*)"',
            r'(https?://[^\s"\'<>]+?\.m3u8[^\s"\'<>]*)',
        ):
            m = re.search(pattern, html)
            if m:
                return m.group(1).rstrip("\\/\"', \t\r\n")
        return None

    # ------------------------------------------------------------------ #
    #  KMS key
    # ------------------------------------------------------------------ #

    def _fetch_key_from_kms(self, key_uri: str) -> bytes | None:
        if not self._kms_secret:
            self.log.warning(" - No KMS secret")
            return None
        headers = _make_kms_headers(key_uri, self._kms_secret)
        try:
            r = self.session.get(key_uri, headers=headers)
            r.raise_for_status()
        except Exception as e:
            self.log.warning(f" - Failed to fetch AES-128 key: {e}")
            return None
        self.log.info(
            f" + AES-128 key fetched ({len(r.content)} bytes) from "
            f"{urlsplit(key_uri).path}"
        )
        return r.content

    # ------------------------------------------------------------------ #
    #  HLS downloader
    # ------------------------------------------------------------------ #

    def _download_hls(self, media_url: str, out_dir, save_name: str) -> str | None:
        out_dir_str = str(out_dir)
        save_name = str(save_name)
        os.makedirs(out_dir_str, exist_ok=True)

        try:
            r = self.session.get(media_url)
            r.raise_for_status()
            media = m3u8.loads(r.text, uri=media_url)
        except Exception as e:
            self.log.warning(f" - Failed to load media playlist: {e}")
            return None

        if not media.segments:
            self.log.warning(" - No segments in media playlist")
            return None

        self.log.info(f" + Media playlist: {len(media.segments)} segments")

        key_bytes: bytes | None = None
        key_iv: bytes | None = None
        first_key = media.keys[0] if media.keys else None

        if first_key and first_key.method == "AES-128" and first_key.uri:
            key_bytes = self._fetch_key_from_kms(first_key.uri)
            if not key_bytes:
                return None
            if first_key.iv:
                iv_val = first_key.iv
                if isinstance(iv_val, str):
                    if iv_val.lower().startswith("0x"):
                        iv_val = iv_val[2:]
                    try:
                        key_iv = bytes.fromhex(iv_val)
                    except ValueError:
                        key_iv = None
                else:
                    key_iv = bytes(iv_val)

        init_data = b""
        if media.segment_map:
            init_uri = media.segment_map[0].uri
            if init_uri:
                try:
                    ir = self.session.get(urljoin(media_url, init_uri))
                    ir.raise_for_status()
                    init_data = ir.content
                except Exception as e:
                    self.log.warning(f" - Failed to fetch init segment: {e}")
                    return None

        total = len(media.segments)
        results: list[bytes | None] = [None] * total

        def fetch_one(idx: int, seg) -> tuple[int, bytes | None]:
            try:
                sr = self.session.get(urljoin(media_url, seg.uri))
                sr.raise_for_status()
                data = sr.content
            except Exception as e:
                self.log.warning(f" - Segment {idx + 1}/{total} fetch failed: {e}")
                return idx, None

            if key_bytes:
                seg_iv = key_iv
                seg_key = getattr(seg, "key", None) or first_key
                if seg_key is not None and getattr(seg_key, "iv", None):
                    iv_val = seg_key.iv
                    if isinstance(iv_val, str):
                        if iv_val.lower().startswith("0x"):
                            iv_val = iv_val[2:]
                        try:
                            seg_iv = bytes.fromhex(iv_val)
                        except ValueError:
                            pass
                    else:
                        seg_iv = bytes(iv_val)
                if seg_iv is None:
                    seq = (media.media_sequence or 0) + idx
                    seg_iv = seq.to_bytes(16, "big")
                try:
                    data = _aes_cbc_decrypt_segment(data, key_bytes, seg_iv)
                except Exception as e:
                    self.log.warning(f" - Segment {idx + 1}/{total} decrypt failed: {e}")
                    return idx, None

            return idx, data

        self.log.info(f" + Downloading {total} segments (parallel={self.PARALLEL_SEGMENTS})")
        done = 0
        failed = 0
        with ThreadPoolExecutor(max_workers=self.PARALLEL_SEGMENTS) as ex:
            futures = [ex.submit(fetch_one, i, seg) for i, seg in enumerate(media.segments)]
            for fut in as_completed(futures):
                idx, data = fut.result()
                results[idx] = data
                done += 1
                if data is None:
                    failed += 1
                if done % 100 == 0 or done == total:
                    self.log.info(f"   {done}/{total} segments ({failed} failed)")

        if failed:
            self.log.warning(f" - {failed}/{total} segments failed")

        ext = ".mp4" if media.segment_map else ".ts"
        output_path = os.path.join(out_dir_str, save_name + ext)
        with open(output_path, "wb") as f:
            if init_data:
                f.write(init_data)
            for data in results:
                if data:
                    f.write(data)

        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        self.log.info(f" + Wrote {output_path} ({size_mb:.1f} MB)")
        return output_path

    # ------------------------------------------------------------------ #
    #  Tracks
    # ------------------------------------------------------------------ #

    def get_tracks(self, title):
        manifest_url = title.service_data.get("manifest")
        if not manifest_url:
            raise self.log.exit(" - Missing manifest URL")

        manifest_url = manifest_url.rstrip("\\/\"', \t\r\n")
        self.log.info(f" + Loading manifest: {manifest_url}")

        try:
            r = self.session.get(manifest_url)
            r.raise_for_status()
            master = m3u8.loads(r.text, uri=manifest_url)
        except Exception as e:
            raise self.log.exit(f" - Failed to load manifest: {e}")

        for p in master.playlists:
            if not p.stream_info.codecs:
                p.stream_info.codecs = "avc1"

        LANG_FIX = {
            "guj": "gu", "gujr": "gu",
            "bangla": "bn", "bengali": "bn",
            "eng": "en", "english": "en",
            "hin": "hi", "hindi": "hi",
            "tam": "ta", "tamil": "ta",
            "tel": "te", "telugu": "te",
        }
        for media in master.media:
            if media.type == "AUDIO" and media.language:
                fixed = LANG_FIX.get(media.language.lower())
                if fixed:
                    media.language = fixed

        tracks = Tracks.from_m3u8(master=master, source=self.ALIASES[0])

        for track in tracks.videos + tracks.audios + tracks.subtitles:
            track.original_url = manifest_url
            track.encrypted = False

            def download_hls(out, name=None, headers=None, proxy=None, _track=track):
                if os.path.isfile(out):
                    raise ValueError("Path must be to a directory and not a file")
                os.makedirs(out, exist_ok=True)
                re_name = getattr(_track, "re_name", None) or (name or "{type}_{id}_{enc}").format(
                    type=_track.__class__.__name__,
                    id=_track.id,
                    enc="dec",
                )
                result = self._download_hls(_track.url, out, re_name)
                if not result:
                    raise IOError(f"Failed to download {re_name}")
                _track._location = result
                _track.encrypted = False
                return result

            track.download = download_hls
            if not getattr(track, "kid", None):
                track.kid = "0" * 32
            if not getattr(track, "key", None):
                track.key = "0" * 32

        default_lang = self._safe_language()
        for track in tracks:
            if not track.language:
                track.language = default_lang
            if not track.codec:
                track.codec = "avc1" if track.type == "video" else ("aac" if track.type == "audio" else "")

        unique_audios = []
        seen_audio_keys = set()
        for a in tracks.audios:
            key = (str(a.language) if a.language else self.DEFAULT_AUDIO_LANG, a.url, a.codec)
            if key in seen_audio_keys:
                continue
            seen_audio_keys.add(key)
            unique_audios.append(a)
        if len(unique_audios) < len(tracks.audios):
            tracks.audios = unique_audios

        return tracks

    # ------------------------------------------------------------------ #
    #  Hooks
    # ------------------------------------------------------------------ #

    def get_chapters(self, title):
        return []

    def certificate(self, **_):
        return None

    def license(self, *_args, **_kwargs):
        return None

    def configure(self):
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) "
                "Gecko/20100101 Firefox/156.0"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://utshob.live/",
            "Origin": "https://utshob.live",
        })


class BcineetTracks(Tracks):
    """Sets the audio language tag on the muxed file."""

    def __init__(self, *args, audio_lang="bn", **kwargs):
        super().__init__(*args, **kwargs)
        self.audio_lang = audio_lang

    @classmethod
    def from_tracks(cls, tracks: Tracks, audio_lang: str = "bn") -> "BcineetTracks":
        instance = cls(audio_lang=audio_lang)
        instance.videos = tracks.videos
        instance.audios = tracks.audios
        instance.subtitles = tracks.subtitles
        return instance

    def _resolve_lang(self):
        """Return (alpha3, ietf, display_name) for the target language."""
        try:
            lang_obj = Language.get(self.audio_lang)
            return (
                lang_obj.to_alpha3(),
                str(lang_obj),
                lang_obj.display_name(),
            )
        except Exception:
            code = str(self.audio_lang)
            return (code, code, code)

    def _tag_with_mkvpropedit(self, path: str) -> bool:
        """Force the audio language tag via mkvpropedit. Returns True on success."""
        mkvpropedit = shutil.which("mkvpropedit")
        if not mkvpropedit:
            return False
        alpha3, ietf, name = self._resolve_lang()
        result = subprocess.run(
            [
                mkvpropedit, path,
                "--edit", "track:a1",
                "--set", f"language={alpha3}",
                "--set", f"language-ietf={ietf}",
                "--set", f"name={name}",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.returncode != 0:
            print(f"[BcineetTracks] mkvpropedit failed: {result.stdout}")
        return result.returncode == 0

    def _retag_via_mkvmerge(self, path: str) -> bool:
        """
        Fallback: remux through mkvmerge with an explicit language tag.
        This is slower but works without mkvpropedit.
        """
        mkvmerge = shutil.which("mkvmerge")
        if not mkvmerge:
            return False
        alpha3, ietf, name = self._resolve_lang()
        tmp_path = path + ".retag.mkv"
        result = subprocess.run(
            [
                mkvmerge,
                "-o", tmp_path,
                "--language", f"0:{alpha3}",
                "--track-name", f"0:{name}",
                path,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.returncode not in (0, 1):
            print(f"[BcineetTracks] mkvmerge retag failed: {result.stdout}")
            if os.path.isfile(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return False
        try:
            os.replace(tmp_path, path)
        except Exception as e:
            print(f"[BcineetTracks] Could not replace {path}: {e}")
            return False
        return True

    def mux(self, prefix):
        muxed_location, returncode = super().mux(prefix)

        if returncode not in (0, 1):
            return muxed_location, returncode
        if not self.audio_lang or not os.path.isfile(muxed_location):
            return muxed_location, returncode

        # Try mkvpropedit first (fast, in-place), then mkvmerge fallback.
        if self._tag_with_mkvpropedit(muxed_location):
            print(f"[BcineetTracks] Audio language tagged as {self.audio_lang}")
            return muxed_location, returncode

        print("[BcineetTracks] mkvpropedit unavailable, trying mkvmerge retag")
        if self._retag_via_mkvmerge(muxed_location):
            print(f"[BcineetTracks] Audio language tagged as {self.audio_lang}")
            return muxed_location, returncode

        print(
            "[BcineetTracks] WARNING: could not tag audio language — "
            "install mkvpropedit (from MKVToolNix) and ensure it's on PATH"
        )
        return muxed_location, returncode
