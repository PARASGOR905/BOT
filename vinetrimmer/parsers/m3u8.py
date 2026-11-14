import base64
import re
from hashlib import md5
import m3u8
import requests
from vinetrimmer.objects import AudioTrack, TextTrack, Track, Tracks, VideoTrack
from vinetrimmer.utils import Cdm
from vinetrimmer.vendor.pymp4.parser import Box


def max_duration(m3u_playlist) -> float | None:
    if not getattr(m3u_playlist, "segments", None):
        return None 
    buckets = []
    acc = 0.0
    for seg in m3u_playlist.segments:
        if getattr(seg, "discontinuity", False):
            buckets.append(acc)
            acc = 0.0
        acc += float(getattr(seg, "duration", 0.0) or 0.0)
    buckets.append(acc)
    return max(buckets) if buckets else None


def get_duration(url: str, session: requests.Session | None = None) -> float | None:
    try:
        sess = session or requests.Session()
        text = sess.get(url, timeout=(5, 15)).text
        pl = m3u8.loads(text, uri=url)
        return max_duration(pl)
    except Exception:
        return None

def resolve_uri(uri: str | None, base_uri: str | None = None, fallback_url: str | None = None) -> str:
    if not uri:
        return ""
    if re.match(r"^https?://", uri):
        return uri
    if base_uri:
        return base_uri + uri
    if fallback_url:
        base = fallback_url.split("?", 1)[0].rsplit("/", 1)[0] + "/" if "/" in fallback_url else fallback_url
        return base + uri
    return uri


def parse(master, source=None, url=None):
    if not master.is_variant:
        raise ValueError("Tracks.from_m3u8: Expected a Variant Playlist M3U8 document...")
    widevine_urn = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
    widevine_keys = [x.uri for x in master.session_keys if x.keyformat.lower() == widevine_urn]
    pssh = widevine_keys[0].split(",")[-1] if widevine_keys else None
    pr_keys = [x.uri for x in master.session_keys if x.keyformat.lower() == "com.microsoft.playready"]
    pr_pssh = pr_keys[0].split(",")[-1] if pr_keys else None
    if pssh:
        pssh = base64.b64decode(pssh)
        try:
            pssh = Box.parse(pssh)
        except Exception:
            pssh = Box.parse(Box.build(dict(
                type=b"pssh",
                version=0, 
                flags=0,
                system_ID=Cdm.uuid,
                init_data=pssh
            )))

    session = requests.Session()
    top_video_url = None
    top_video_bitrate = -1

    for pl in master.playlists:
        br = pl.stream_info.average_bandwidth or pl.stream_info.bandwidth or 0
        if br > top_video_bitrate:
            top_video_bitrate = br
            top_video_url = resolve_uri(pl.uri, pl.base_uri or master.base_uri, url)

    if url and "?" in url:
        query_params = url.split("?", 1)[-1]
        if query_params:
            if top_video_url:
                top_video_url += ("&" if "?" in top_video_url else "?") + query_params

    video_duration = get_duration(top_video_url, session=session) if top_video_url else None

    # VIDEO
    video_tracks = []
    for x in master.playlists:
        vt = VideoTrack(
            id_=md5(str(x).encode()).hexdigest()[0:7],  # 7 chars only for filename length
            source=source,
            url=resolve_uri(x.uri, x.base_uri or master.base_uri, url),
            # metadata
            codec=(x.stream_info.codecs.split(",")[0].split(".")[0]) if x.stream_info.codecs else "h264",  # first codec may not be for the video
            language=None,  # playlists don't state the language, fallback must be used
            bitrate=x.stream_info.average_bandwidth or x.stream_info.bandwidth,
            width=x.stream_info.resolution[0] if x.stream_info.resolution else 0,
            height=x.stream_info.resolution[1] if x.stream_info.resolution else 0,
            fps=x.stream_info.frame_rate,
            hdr10=(((x.stream_info.codecs.split(".")[0] not in ("dvhe", "dvh1")) if x.stream_info.codecs else True)
                   and (x.stream_info.video_range or "SDR").strip('"') != "SDR"),
            hlg=False,  # TODO: Can we get this from the manifest?
            dv=(x.stream_info.codecs.split(".")[0] in ("dvhe", "dvh1")) if x.stream_info.codecs else False,
            # switches/options
            descriptor=Track.Descriptor.M3U,
            # decryption
            encrypted=bool(master.keys or master.session_keys),
            pssh=pssh,
            pr_pssh=pr_pssh,
            # extra
            extra=x
        )
        if video_duration:
            vt.duration = video_duration
        video_tracks.append(vt)

    # AUDIO
    audio_tracks = []
    for x in master.media:
        if x.type != "AUDIO" or not x.uri:
            continue

        # Parse codec from group_id, with a fallback if it's generic/uninformative
        codec = x.group_id.replace("audio-", "").split("-")[0].split(".")[0]
        if codec.lower() in ("program_audio", "audio", "program", "default", "track") or not codec:
            uri_lower = (x.uri or "").lower()
            if "ec3" in uri_lower or "eac3" in uri_lower or "surround" in uri_lower or "6ch" in uri_lower:
                codec = "ec-3"
            elif "ac3" in uri_lower:
                codec = "ac-3"
            elif "aac" in uri_lower or "stereo" in uri_lower or "2ch" in uri_lower:
                codec = "aac"
            else:
                codec = "aac"

        # Estimate bitrate based on channels if not specified
        bitrate = 0
        if x.channels:
            try:
                ch = float(x.channels)
                if ch > 2.0:
                    bitrate = 384000  # 384 kbps for 5.1
                else:
                    bitrate = 128000  # 128 kbps for stereo
            except ValueError:
                if "surround" in (x.channels or "").lower() or "5.1" in (x.channels or "").lower():
                    bitrate = 384000
                else:
                    bitrate = 128000
        else:
            bitrate = 128000

        at = AudioTrack(
            id_=md5(str(x).encode()).hexdigest()[0:6],
            source=source,
            url=resolve_uri(x.uri, x.base_uri or master.base_uri, url),
            # metadata
            codec=codec,
            language=x.language,
            bitrate=bitrate,
            channels=x.channels,
            atmos=(x.channels or "").endswith("/JOC"),
            descriptive="public.accessibility.describes-video" in (x.characteristics or ""),
            # switches/options
            descriptor=Track.Descriptor.M3U,
            # decryption
            encrypted=False,  # don't know for sure if encrypted
            pssh=pssh,
            pr_pssh=pr_pssh,
            # extra
            extra=x
        )
        if video_duration:
            at.duration = video_duration
        audio_tracks.append(at)

    # SUBTITLES
    subtitle_tracks = []
    for x in master.media:
        if x.type != "SUBTITLES" or not x.uri:
            continue
        tt = TextTrack(
            id_=md5(str(x).encode()).hexdigest()[0:6],
            source=source,
            url=resolve_uri(x.uri, x.base_uri or master.base_uri, url),
            # metadata
            codec="vtt",  # assuming VTT, codec info isn't shown
            language=x.language,
            forced=x.forced == "YES",
            sdh="public.accessibility.describes-music-and-sound" in (x.characteristics or ""),
            # switches/options
            descriptor=Track.Descriptor.M3U,
            # extra
            extra=x
        )
        if video_duration:
            tt.duration = video_duration
        subtitle_tracks.append(tt)

    return Tracks(video_tracks, audio_tracks, subtitle_tracks)
