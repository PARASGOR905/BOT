from __future__ import annotations

import json
import random
import string

import click
import m3u8
from langcodes import Language

from vinetrimmer.objects import Title, Track, Tracks, TextTrack
from vinetrimmer.services.BaseService import BaseService

_DIALECT_CODE_TO_NAME = {
    "mar": "marathi",
    "hin": "hindi",
    "bho": "bhojpuri",
    "pun": "punjabi",
    "har": "haryanvi",
    "raj": "rajasthani",
    "ben": "bengali",
    "tam": "tamil",
    "tel": "telugu",
    "guj": "gujarati",
    "kan": "kannada",
    "mal": "malayalam",
    "ori": "odia",
    "en": "english",
}
_DIALECT_NAME_TO_CODE = {v: k for k, v in _DIALECT_CODE_TO_NAME.items()}


class Stagein(BaseService):    
    """
    Service code for Stage.in (https://www.stage.in).

    Authorization: Cookies (tokenv1 JWT required for premium content)
    Security: No DRM — HLS delivered via CloudFront signed URLs

    Input formats accepted:
      Full URL:   https://www.stage.in/en/marathi/movie/jholachhap-19151
                  https://www.stage.in/en/marathi/show/plus-minus-16510
      Short form: movie:19151   show:16510
    """
    ALIASES = ["STAGE", "stagein", "SI"]
    GEOFENCE = []
    TITLE_RE = [
        r"^https?://(?:www\.)?stage\.in/[^/]+/[^/]+/(?P<content_type>movie|show)/(?:watch/)?[^/]+-(?P<id>\d+)(?:/\d+)?(?:\?.*)?$",
        r"^(?P<content_type>movie|show):(?P<id>\d+)$",
        r"^(?P<id>\d+)$",
    ]

    @staticmethod
    @click.command(name="Stagein", short_help="https://www.stage.in")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return Stagein(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)
        assert ctx.parent is not None

        parsed = self.parse_title(ctx, title) or {}
        self.content_id = int(self.title)
        self.content_type = parsed.get("content_type") or "movie"
        self.wanted = ctx.parent.params.get("wanted")
        self.vcodec = ctx.parent.params.get("vcodec") or "H264"

        self._api_root = self.config["endpoints"]["api"]
        self._site_root = self.config["endpoints"]["site"]

        self._setup_auth()

    def _setup_auth(self):
        token = self.session.cookies.get("tokenv1")
        cfg = self.config["headers"]
        headers = {
            "app_id": cfg["app_id"],
            "platform": cfg["platform"],
            "os": cfg["os"],
            "lang": self.session.cookies.get("lang") or "en",
            "dialect": self._dialect_code(),
            "api-version": cfg["api_version"],
            "Origin": self._site_root,
            "Referer": f"{self._site_root}/",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.session.headers.update(headers)

    def _dialect_code(self):
        name = (self.session.cookies.get("dialectv1") or "").lower()
        return _DIALECT_NAME_TO_CODE.get(name) or name[:3] or "mar"

    def _dialect_name(self, lang_code):
        code = (lang_code or "").lower()
        if code in _DIALECT_CODE_TO_NAME:
            return _DIALECT_CODE_TO_NAME[code]
        return (self.session.cookies.get("dialectv1") or "marathi").lower()

    def _api_get(self, path, params=None):
        resp = self.session.get(f"{self._api_root}{path}", params=params or {}, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _fetch_rsc(self, path):
        rsc_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
        resp = self.session.get(
            f"{self._site_root}{path}?_rsc={rsc_id}",
            headers={
                "rsc": "1",
                "Accept": "*/*",
                "next-url": path.rsplit("/watch/", 1)[0] if "/watch/" in path else "/",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.text

    @staticmethod
    def _url_slug(api_slug, content_id):
        base = api_slug.rsplit("-", 1)[0] if "-" in api_slug else api_slug
        return f"{base}-{content_id}"

    def _parse_signed_url_details(self, rsc_text):
        key = '"signedURLDetails":'
        idx = rsc_text.find(key)
        if idx == -1:
            self.log.exit(" - signedURLDetails not found in page response - check cookies/auth")
        start = rsc_text.index("{", idx + len(key))
        obj, _ = json.JSONDecoder().raw_decode(rsc_text, start)
        return obj

    def _best_hls_url(self, details):
        h265 = [u for u in [
            (details.get("signedObjectH265") or {}).get("signedUrl") or "",
            details.get("signedUrlH265") or "",
        ] if u and "Policy=" in u]
        h264 = [u for u in [
            (details.get("signedObject") or {}).get("signedUrl") or "",
            details.get("signedUrl") or "",
        ] if u and "Policy=" in u]

        ordered = (h265 + h264) if self.vcodec == "H265" else (h264 + h265)
        if ordered:
            return ordered[0]

        self.log.exit(" - No usable signed HLS URL found in page response")

    def _expand_variant(self, playlist_url, cf_auth):
        resp = self.session.get(playlist_url, timeout=10)
        resp.raise_for_status()
        pl = m3u8.loads(resp.text, uri=playlist_url)

        def resolve(uri, base):
            if not uri.startswith(("http://", "https://")):
                uri = base + uri
            if cf_auth and "Policy=" not in uri:
                uri += ("&" if "?" in uri else "?") + cf_auth
            return uri

        segments = []
        has_init = False
        for seg in pl.segments:
            if seg.init_section and not has_init:
                segments.append(resolve(seg.init_section.uri, seg.init_section.base_uri))
                has_init = True
            if seg.uri:
                segments.append(resolve(seg.uri, seg.base_uri))
        return segments

    def get_titles(self):
        if self.content_type == "movie":
            return self._get_movie_title()
        return self._get_show_titles()

    def _get_movie_title(self):
        data = self._api_get(self.config["endpoints"]["movie_details"], {"movieId": self.content_id})
        movie = data.get("movie") or {}
        if not movie:
            self.log.exit(f" - Stage.in returned empty movie details for {self.content_id}")
        lang = _parse_lang(movie.get("language"))
        slug = movie.get("slug") or ""
        return Title(
            id_=str(movie.get("_id") or self.content_id),
            type_=Title.Types.MOVIE,
            name=movie.get("title") or f"Stagein-{self.content_id}",
            year=int(movie.get("yearOfRelease") or 0),
            original_lang=lang,
            source=self.ALIASES[0],
            service_data={
                **movie,
                "_url_slug": self._url_slug(slug, self.content_id),
                "_lang_code": movie.get("language") or "",
                "_dialect_mapping": movie.get("dialectMapping") or movie.get("dialactMapping") or {},
            },
        )

    def _get_show_titles(self):
        data = self._api_get(
            self.config["endpoints"]["show_details"],
            {"showId": self.content_id, "seasonId": ""},
        )
        show = data.get("data") or {}
        if not show:
            self.log.exit(f" - Stage.in returned empty show details for {self.content_id}")

        lang = _parse_lang(show.get("language"))
        show_name = show.get("title") or f"Stagein-{self.content_id}"
        year = int(show.get("yearOfRelease") or 0)
        show_url_slug = self._url_slug(show.get("slug") or "", self.content_id)

        titles = []
        for ep in show.get("episodes") or []:
            ep_order = int(ep.get("episodeOrder") or ep.get("order") or 0)
            if self.wanted and f"1x{ep_order}" not in self.wanted:
                continue
            titles.append(Title(
                id_=str(ep.get("id") or ep.get("_id") or ""),
                type_=Title.Types.TV,
                name=show_name,
                year=year,
                season=1,
                episode=ep_order,
                episode_name=ep.get("title"),
                original_lang=lang,
                source=self.ALIASES[0],
                service_data={
                    **ep,
                    "_show_url_slug": show_url_slug,
                    "_episode_order": ep_order,
                    "_lang_code": show.get("language") or "",
                    "_dialect_mapping": show.get("dialectMapping") or show.get("dialactMapping") or {},
                },
            ))
        if not titles:
            self.log.exit(" - No Stage.in episodes matched.")
        return titles

    def get_tracks(self, title):
        meta = title.service_data
        lang_code = meta.get("_lang_code") or ""
        dialect_name = self._dialect_name(lang_code)

        if title.type == Title.Types.MOVIE:
            watch_path = f"/en/{dialect_name}/movie/watch/{meta['_url_slug']}"
        else:
            watch_path = f"/en/{dialect_name}/show/watch/{meta['_show_url_slug']}/{meta['_episode_order']}"

        rsc_text = self._fetch_rsc(watch_path)
        details = self._parse_signed_url_details(rsc_text)
        m3u8_url = self._best_hls_url(details)

        self.log.debug(" + HLS URL: %s", m3u8_url[:120])
        

        resp = self.session.get(m3u8_url, timeout=10)
        resp.raise_for_status()
        master = m3u8.loads(resp.text, uri=resp.url)

        content_lang = _parse_lang(lang_code) or _parse_lang(self._dialect_code()) or "mr"
        for media in master.media:
            if media.type == "AUDIO":
                media.language = content_lang

        tracks = Tracks.from_m3u8(master, source=self.ALIASES[0], url=m3u8_url)

        audio_codec = _audio_codec_from_master(master)
        if audio_codec:
            for audio in tracks.audios:
                audio.codec = audio_codec

        # Override default/missing audio bitrate dynamically
        for track in tracks.audios:
            if not track.bitrate or track.bitrate == 0:
                try:
                    auth_url = track.url
                    if "Policy=" in m3u8_url:
                        cf_auth = "&".join(
                            p for p in m3u8_url.split("?", 1)[-1].split("&")
                            if p.startswith(("Policy=", "Key-Pair-Id=", "Signature="))
                        )
                        if cf_auth and "Policy=" not in auth_url:
                            auth_url += ("&" if "?" in auth_url else "?") + cf_auth
                    playlist_resp = self.session.get(auth_url, timeout=2)
                    playlist_resp.raise_for_status()
                    import m3u8 as m3u8_lib
                    audio_playlist = m3u8_lib.loads(playlist_resp.text, uri=auth_url)
                    if audio_playlist.segments:
                        first_seg = audio_playlist.segments[0]
                        size = 0
                        if getattr(first_seg, "byterange", None):
                            try:
                                size = int(first_seg.byterange.split("@")[0])
                            except (ValueError, IndexError):
                                pass
                        if not size:
                            seg_url = first_seg.absolute_uri
                            seg_head = self.session.head(seg_url, timeout=2)
                            size = int(seg_head.headers.get("Content-Length", 0))
                            if not size:
                                seg_get = self.session.get(seg_url, headers={"Range": "bytes=0-0"}, timeout=2)
                                cr = seg_get.headers.get("Content-Range")
                                if cr and "/" in cr:
                                    size = int(cr.split("/")[-1])
                        if size and first_seg.duration:
                            calculated_bitrate = int((size * 8) / first_seg.duration)
                            if calculated_bitrate > 5_000_000:
                                total_duration = sum(seg.duration for seg in audio_playlist.segments)
                                if total_duration > 0:
                                    calculated_bitrate = int((size * 8) / total_duration)
                            track.bitrate = (calculated_bitrate // 1000) * 1000
                except Exception as e:
                    self.log.debug(f"Failed to dynamically calculate audio bitrate: {e}")

                if not track.bitrate or track.bitrate == 0:
                    track.bitrate = 128000

        if "Policy=" in m3u8_url:
            cf_auth = "&".join(
                p for p in m3u8_url.split("?", 1)[-1].split("&")
                if p.startswith(("Policy=", "Key-Pair-Id=", "Signature="))
            )
            for track in tracks.videos + tracks.audios + tracks.subtitles:
                if cf_auth and "Policy=" not in track.url:
                    track.url += ("&" if "?" in track.url else "?") + cf_auth
                segments = self._expand_variant(track.url, cf_auth)
                if segments:
                    track.url = segments
                    track.descriptor = Track.Descriptor.URL

        # Keep only 1 primary audio track to avoid duplicates
        if len(tracks.audios) > 1:
            tracks.audios = [tracks.audios[0]]

        # Add sidecar subtitles from API response
        subtitles_dict = meta.get("subtitle") or {}
        if isinstance(subtitles_dict, dict):
            from hashlib import md5
            import os
            from vinetrimmer.config import directories
            os.makedirs(directories.temp, exist_ok=True)
            for lang_code_raw, srt_filename in subtitles_dict.items():
                if srt_filename and srt_filename.strip():
                    playlist_base = m3u8_url.split("?", 1)[0].rsplit("/", 1)[0]
                    cf_auth = ""
                    if "Policy=" in m3u8_url:
                        cf_auth = "&".join(
                            p for p in m3u8_url.split("?", 1)[-1].split("&")
                            if p.startswith(("Policy=", "Key-Pair-Id=", "Signature="))
                        )
                    
                    sub_lang = _parse_lang(lang_code_raw) or lang_code_raw
                    track_id = md5(srt_filename.encode()).hexdigest()[0:6]
                    local_srt_path = os.path.join(directories.temp, f"TextTrack_{track_id}_dec.srt")
                    
                    candidate_urls = [
                        f"{playlist_base}/{srt_filename}",
                        f"https://dg8bsfqjprq0f.cloudfront.net/subtitles/{srt_filename}",
                        f"https://dg8bsfqjprq0f.cloudfront.net/{srt_filename}",
                        f"https://media.stage.in/subtitles/{srt_filename}",
                        f"https://media.stage.in/{srt_filename}",
                    ]
                    if cf_auth:
                        candidate_urls = [u + ("&" if "?" in u else "?") + cf_auth for u in candidate_urls]

                    # Pre-fetch subtitle file immediately while CloudFront signature is fresh
                    fetched_path = None
                    for sub_u in candidate_urls:
                        try:
                            s_resp = self.session.get(sub_u, timeout=5)
                            if s_resp.status_code == 200 and s_resp.text.strip():
                                with open(local_srt_path, "w", encoding="utf-8") as f:
                                    f.write(s_resp.text)
                                fetched_path = local_srt_path
                                break
                        except Exception as e_sub:
                            self.log.debug(f"Subtitle fetch attempt failed for {lang_code_raw}: {e_sub}")

                    srt_url = fetched_path if fetched_path else candidate_urls[0]

                    tt = TextTrack(
                        id_=track_id,
                        source=self.ALIASES[0],
                        url=srt_url,
                        codec="srt",
                        language=Language.get(sub_lang),
                        descriptor=Track.Descriptor.URL,
                    )

                    def _make_sub_downloader(tr, initial_candidates, sub_file_name, w_path):
                        def _sub_download(out, name=None, headers=None, proxy=None):
                            import os, shutil
                            os.makedirs(out, exist_ok=True)
                            re_name = (name or "{type}_{id}_{enc}").format(
                                type=tr.__class__.__name__, id=tr.id, enc="enc" if tr.encrypted else "dec"
                            )
                            save_path = os.path.join(out, f"{re_name}.srt")

                            # 1. If pre-fetched locally or already exists
                            if os.path.exists(tr.url) and os.path.isfile(tr.url) and os.path.getsize(tr.url) > 0:
                                if os.path.abspath(tr.url) != os.path.abspath(save_path):
                                    shutil.copy(tr.url, save_path)
                                tr._location = save_path
                                return save_path

                            # 2. Try initial candidate URLs
                            for sub_u in initial_candidates:
                                try:
                                    res = self.session.get(sub_u, timeout=8)
                                    if res.status_code == 200 and res.text.strip():
                                        with open(save_path, "w", encoding="utf-8") as f:
                                            f.write(res.text)
                                        tr._location = save_path
                                        return save_path
                                except Exception:
                                    pass

                            # 3. Refresh CloudFront signature and try again
                            try:
                                fresh_rsc = self._fetch_rsc(w_path)
                                fresh_details = self._parse_signed_url_details(fresh_rsc)
                                fresh_m3u8 = self._best_hls_url(fresh_details)
                                fresh_cf = "&".join(p for p in fresh_m3u8.split("?", 1)[-1].split("&") if p.startswith(("Policy=", "Key-Pair-Id=", "Signature=")))
                                base_p = fresh_m3u8.split("?", 1)[0].rsplit("/", 1)[0]
                                fresh_urls = [
                                    f"{base_p}/{sub_file_name}?{fresh_cf}",
                                    f"https://dg8bsfqjprq0f.cloudfront.net/subtitles/{sub_file_name}?{fresh_cf}",
                                    f"https://dg8bsfqjprq0f.cloudfront.net/{sub_file_name}?{fresh_cf}",
                                ]
                                for f_u in fresh_urls:
                                    res2 = self.session.get(f_u, timeout=8)
                                    if res2.status_code == 200 and res2.text.strip():
                                        with open(save_path, "w", encoding="utf-8") as f:
                                            f.write(res2.text)
                                        tr._location = save_path
                                        return save_path
                            except Exception as e_fr:
                                self.log.warning(f"Failed to refresh subtitle signature: {e_fr}")

                            # 4. Safety Fallback: Ensure file exists on disk so mkvmerge doesn't crash
                            self.log.warning(f"Subtitle file missing/unreachable ({sub_file_name}). Writing empty SRT fallback.")
                            with open(save_path, "w", encoding="utf-8") as f:
                                f.write("1\n00:00:00,000 --> 00:00:01,000\n \n")
                            tr._location = save_path
                            return save_path

                        return _sub_download

                    tt.download = _make_sub_downloader(tt, candidate_urls, srt_filename, watch_path)
                    tracks.subtitles.append(tt)

        return tracks

    def get_chapters(self, title):
        return []


def _parse_lang(label):
    if not label:
        return None
    label_lower = label.lower()
    code = None
    if label_lower in _DIALECT_CODE_TO_NAME:
        code = label_lower
    elif label_lower in _DIALECT_NAME_TO_CODE:
        code = _DIALECT_NAME_TO_CODE[label_lower]
        
    if code == "har":
        return "har"
    if code:
        return code
        
    try:
        lang = Language.get(label)
        if lang.language:
            return lang.to_alpha3()
    except Exception:
        pass
    try:
        lang = Language.find(label)
        return lang.to_alpha3()
    except Exception:
        return None


def _audio_codec_from_master(master):
    audio_bases = {"mp4a", "ac-3", "ec-3", "ac-4", "opus", "vorbis", "flac", "alac"}
    for pl in master.playlists:
        for part in (pl.stream_info.codecs or "").split(","):
            part = part.strip()
            if part.split(".")[0].lower() in audio_bases:
                return part
    return None