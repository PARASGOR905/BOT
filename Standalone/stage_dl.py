#!/usr/bin/env python3
import os
import sys
import re
import shutil
import requests
import subprocess
import argparse
import json
import logging
import coloredlogs
from http.cookiejar import MozillaCookieJar
from tqdm import tqdm

# Add binaries folder to PATH for pymediainfo / ffprobe
binaries_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "binaries"))
os.environ['PATH'] = binaries_dir + os.path.pathsep + os.environ.get('PATH', '')

# Import vinetrimmer components
try:
    from vinetrimmer.config import directories, config
    from vinetrimmer.objects import Title, VideoTrack, AudioTrack, TextTrack
    from langcodes import Language
except ImportError:
    print("Error: Could not import vinetrimmer modules. Please ensure you are running inside the correct virtual environment.", file=sys.stderr)
    sys.exit(1)

log = logging.getLogger("STAGE")

class MockTrack:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

class MockMediaInfo:
    def __init__(self, video_tracks, audio_tracks):
        self.video_tracks = video_tracks
        self.audio_tracks = audio_tracks

def create_mock_media_info(title_obj):
    mock_video_tracks = []
    if title_obj.tracks.videos:
        vt = title_obj.tracks.videos[0]
        codec = vt.codec or "H264"
        if codec.lower() in ("avc1", "h264"):
            fmt = "AVC"
        elif codec.lower() in ("hev1", "hvc1", "hevc"):
            fmt = "HEVC"
        else:
            fmt = codec
            
        mock_video_tracks.append(MockTrack(
            width=vt.width or 1920,
            height=vt.height or 1080,
            other_display_aspect_ratio=["16:9"],
            format=fmt,
            hdr_format=None,
            hdr_format_compatibility=None,
            hdr_format_commercial=None,
            transfer_characteristics=None,
            transfer_characteristics_original=None,
            frame_rate=str(vt.fps or 25.0),
        ))
        
    mock_audio_tracks = []
    if title_obj.tracks.audios:
        at = title_obj.tracks.audios[0]
        codec = at.codec or "AAC"
        fmt = "AAC" if codec.lower() in ("aac", "mp4a") else codec.upper()
        
        channels = at.channels or "2ch"
        if "6" in channels or "5.1" in channels:
            layout = "L C R Ls Rs LFE"
        else:
            layout = "L R"
            
        mock_audio_tracks.append(MockTrack(
            format=fmt,
            channel_layout=layout,
            format_additionalfeatures=None,
        ))
        
    return MockMediaInfo(mock_video_tracks, mock_audio_tracks)

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

# Dialect patterns from VT stagein service
TITLE_RE = [
    r"^https?://(?:www\.)?stage\.in/(?:[^/]+/)?(?P<dialect>[^/]+)/(?P<content_type>movie|show)/(?:watch/)?[^/]+-(?P<id>\d+)(?:/(?P<episode>\d+))?(?:\?.*)?$",
    r"^https?://(?:www\.)?stage\.in/(?P<content_type>movie|show)/(?:watch/)?[^/]+-(?P<id>\d+)(?:/(?P<episode>\d+))?(?:\?.*)?$",
    r"^(?P<content_type>movie|show):(?P<id>\d+)$",
    r"^(?P<id>\d+)$",
]

def load_cookies():
    cookie_file = os.path.join(directories.cookies, "stagein", "default.txt")
    cookies = {}
    if os.path.isfile(cookie_file):
        try:
            cookie_jar = MozillaCookieJar(cookie_file)
            cookie_jar.load(ignore_discard=True, ignore_expires=True)
            for c in cookie_jar:
                cookies[c.name] = c.value
        except Exception as e:
            log.warning(f"Failed to load cookies: {e}")
    return cookies

def parse_input(input_str):
    input_str = (input_str or "").strip()
    if input_str.lower().startswith("show:http") or input_str.lower().startswith("movie:http"):
        input_str = input_str.split(":", 1)[1].strip()

    for pattern in TITLE_RE:
        match = re.search(pattern, input_str, re.IGNORECASE)
        if match:
            gd = match.groupdict()
            dialect = gd.get("dialect")
            content_type = gd.get("content_type") or "movie"
            content_id = gd.get("id")
            episode = gd.get("episode")
            return dialect, content_type, content_id, episode
            
    # Fallback: find any digits
    cleaned = "".join(filter(str.isdigit, input_str))
    if cleaned:
        content_type = "show" if "show" in input_str.lower() else "movie"
        return None, content_type, cleaned, None
    return None, None, None, None

def get_dialect_code(dialect_name, cookies):
    if dialect_name:
        name = dialect_name.lower()
        if name in _DIALECT_NAME_TO_CODE:
            return _DIALECT_NAME_TO_CODE[name]
        return name[:3]
    cookie_dialect = (cookies.get("dialectv1") or "").lower()
    if cookie_dialect:
        if cookie_dialect in _DIALECT_NAME_TO_CODE:
            return _DIALECT_NAME_TO_CODE[cookie_dialect]
        return cookie_dialect[:3]
    return "mar" # Default fallback

def probe_and_setup_tracks(title_obj, video_url, headers, dialect_code, subtitles_dict):
    
    binaries_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "binaries"))
    ffprobe_path = shutil.which("ffprobe") or os.path.join(binaries_dir, "ffprobe.exe")
    
    headers_str = "\r\n".join(f"{k}: {v}" for k, v in headers.items()) + "\r\n"
    cmd = [
        ffprobe_path,
        "-v", "quiet",
        "-headers", headers_str,
        "-probesize", "500000",
        "-analyzeduration", "500000",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        video_url
    ]
    
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            log.warning(f"Failed to read remote metadata via ffprobe: {res.stderr}")
            return
            
        info = json.loads(res.stdout)
        fmt = info.get("format", {})
        
        # Fetch the exact content length using HEAD request to avoid 32-bit overflow limitations in ffprobe
        size = None
        try:
            head_res = requests.head(video_url, headers=headers, timeout=10)
            if head_res.status_code == 200:
                size = int(head_res.headers.get("content-length") or 0)
        except Exception as e:
            log.warning(f"Failed to fetch content-length via HEAD request: {e}")

        duration = None
        try:
            duration = float(fmt.get("duration") or 0.0)
        except Exception:
            pass

        for stream in info.get("streams", []):
            codec_type = stream.get("codec_type")
            codec_name = (stream.get("codec_name") or "").lower()
            
            if codec_type == "video":
                width = int(stream.get("width") or 0)
                height = int(stream.get("height") or 0)
                
                # Parse FPS
                fps = None
                fps_raw = stream.get("r_frame_rate")
                if fps_raw and "/" in fps_raw:
                    try:
                        num, den = fps_raw.split("/")
                        if int(den) != 0:
                            fps = int(num) / int(den)
                    except Exception:
                        pass
                elif fps_raw:
                    try:
                        fps = float(fps_raw)
                    except Exception:
                        pass

                # Parse Bitrate
                bitrate = None
                try:
                    bitrate = int(stream.get("bit_rate") or 0)
                except Exception:
                    pass
                if not bitrate:
                    try:
                        bitrate = int(fmt.get("bit_rate") or 0)
                    except Exception:
                        pass
                
                video_track = VideoTrack(
                    id_="video",
                    source="STAGE",
                    url=video_url,
                    codec="avc1" if codec_name == "h264" else ("hev1" if codec_name == "hevc" else codec_name),
                    bitrate=bitrate,
                    width=width,
                    height=height,
                    fps=fps,
                    size=size,
                    duration=duration,
                    encrypted=False
                )
                title_obj.tracks.videos.append(video_track)
                
            elif codec_type == "audio":
                # Parse Bitrate
                bitrate = None
                try:
                    bitrate = int(stream.get("bit_rate") or 0)
                except Exception:
                    pass
                
                # Channels
                channels = stream.get("channels")
                channels_str = "2.0" if channels == 2 else (f"{channels}.0" if channels else None)
                
                audio_lang = "bgc" if dialect_code == "har" else (dialect_code or "mar")
                
                audio_track = AudioTrack(
                    id_="audio",
                    source="STAGE",
                    url=video_url,
                    codec=codec_name,
                    bitrate=bitrate,
                    channels=channels_str,
                    language=Language.get(audio_lang),
                    encrypted=False,
                    duration=duration
                )
                audio_track.is_original_lang = True
                title_obj.tracks.audios.append(audio_track)
                
        # Subtitles
        if isinstance(subtitles_dict, dict):
            for lang, srt_filename in subtitles_dict.items():
                if srt_filename and srt_filename.strip():
                    base_url_no_ext = video_url.rsplit(".", 1)[0]
                    srt_url = f"{base_url_no_ext}/{srt_filename.strip()}"
                    
                    sub_track = TextTrack(
                        id_=f"sub-{lang}",
                        source="STAGE",
                        url=srt_url,
                        codec="srt",
                        language=Language.get(lang),
                    )
                    sub_track.is_original_lang = False
                    title_obj.tracks.subtitles.append(sub_track)
                    
        log.info("> All Tracks:")
        title_obj.tracks.print()
        log.info("> Selected Tracks:")
        title_obj.tracks.print()
        
    except Exception as e:
        log.warning(f"Failed to probe tracks: {e}")

def get_url_slug(api_slug, content_id):
    if not api_slug:
        return str(content_id)
    base = api_slug.rsplit("-", 1)[0] if "-" in api_slug else api_slug
    return f"{base}-{content_id}"

def get_cloudfront_auth(watch_path, cookies, api_headers, dialect_name):
    import random
    import string
    site_root = "https://www.stage.in"
    rsc_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    
    headers = {
        "rsc": "1",
        "Accept": "*/*",
        "next-url": watch_path.rsplit("/watch/", 1)[0] if "/watch/" in watch_path else "/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    if "tokenv1" in cookies:
        headers["Authorization"] = f"Bearer {cookies['tokenv1']}"
    
    session = requests.Session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".stage.in")
    
    # Overwrite dialect cookies to match content's dialect name
    session.cookies.set("dialectv1", dialect_name, domain=".stage.in")
    session.cookies.set("userCulture", dialect_name, domain=".stage.in")
    
    url = f"{site_root}{watch_path}?_rsc={rsc_id}"
    try:
        resp = session.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            text = resp.text
            key = '"signedURLDetails":'
            idx = text.find(key)
            if idx != -1:
                start = text.index("{", idx + len(key))
                obj, _ = json.JSONDecoder().raw_decode(text, start)
                signed_url = (obj.get("signedObject") or {}).get("signedUrl") or obj.get("signedUrl") or ""
                if "Policy=" in signed_url:
                    cf_auth = "&".join(
                        p for p in signed_url.split("?", 1)[-1].split("&")
                        if p.startswith(("Policy=", "Key-Pair-Id=", "Signature="))
                    )
                    return cf_auth
    except Exception as e:
        log.warning(f"Failed to fetch CloudFront signature from watch page: {e}")
    return None

def download_file(url, headers, dest_path):
    log.info(f"Downloading from: {url}")

    # Clean headers for CDN downloads (media.stage.in) to avoid WAF block
    clean_headers = {}
    if "media.stage.in" in url:
        clean_headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        if "Referer" in headers:
            clean_headers["Referer"] = headers["Referer"]
    else:
        clean_headers = dict(headers)

    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)

    import time

    MAX_RETRIES = 10
    CHUNK = 1024 * 1024  # 1 MiB

    # Get total size with a HEAD request first
    try:
        head = requests.head(url, headers=clean_headers, timeout=15, allow_redirects=True)
        total_size = int(head.headers.get("content-length", 0))
        accepts_ranges = head.headers.get("accept-ranges", "").lower() == "bytes"
    except Exception:
        total_size = 0
        accepts_ranges = False

    attempt = 0
    with tqdm(
        desc="Progress",
        total=total_size or None,
        unit="iB",
        unit_scale=True,
        unit_divisor=1024,
    ) as bar:
        while attempt < MAX_RETRIES:
            # How many bytes are already on disk?
            downloaded = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
            if total_size and downloaded >= total_size:
                bar.n = total_size
                bar.refresh()
                break

            req_headers = dict(clean_headers)
            if downloaded and accepts_ranges:
                req_headers["Range"] = f"bytes={downloaded}-"
            elif downloaded and not accepts_ranges:
                # Server doesn't support Range — restart from scratch
                downloaded = 0

            bar.n = downloaded
            bar.refresh()

            try:
                response = requests.get(
                    url, headers=req_headers, stream=True, timeout=60
                )
                if response.status_code == 416:
                    # Range Not Satisfiable — file already complete
                    break
                response.raise_for_status()

                # On a ranged response the server confirms content-length of the slice
                if not total_size:
                    cl = response.headers.get("content-length", "0")
                    cr = response.headers.get("content-range", "")
                    if "/" in cr:
                        total_size = int(cr.split("/")[-1])
                    else:
                        total_size = downloaded + int(cl)
                    if total_size:
                        bar.total = total_size
                        bar.refresh()

                mode = "ab" if downloaded else "wb"
                with open(dest_path, mode) as fh:
                    for chunk in response.iter_content(chunk_size=CHUNK):
                        if chunk:
                            fh.write(chunk)
                            bar.update(len(chunk))

                # Verify completion
                final_size = os.path.getsize(dest_path)
                if total_size and final_size < total_size:
                    raise requests.exceptions.ChunkedEncodingError(
                        f"Incomplete: got {final_size}/{total_size} bytes"
                    )
                break  # success

            except (
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout,
            ) as exc:
                attempt += 1
                on_disk = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
                wait = min(5 * attempt, 60)
                if attempt < MAX_RETRIES:
                    log.warning(
                        f" - Connection dropped at {on_disk / 1024**3:.2f} GiB "
                        f"({100*on_disk/total_size:.1f}% of {total_size/1024**3:.2f} GiB). "
                        f"Retrying in {wait}s (attempt {attempt}/{MAX_RETRIES})..."
                    )
                    time.sleep(wait)
                    accepts_ranges = True  # assume it does — will fall back if not
                else:
                    log.error(f" - Download failed after {MAX_RETRIES} retries: {exc}")
                    raise

    log.info("Download completed.")



class ProgressFileReader:
    def __init__(self, file_path):
        self.filename = os.path.basename(file_path)
        self.file_size = os.path.getsize(file_path)
        self.file = open(file_path, 'rb')
        self.bar = tqdm(
            desc=f"Uploading {self.filename}",
            total=self.file_size,
            unit='iB',
            unit_scale=True,
            unit_divisor=1024,
        )

    def read(self, size=-1):
        data = self.file.read(size)
        if data:
            self.bar.update(len(data))
        return data

    def close(self):
        self.file.close()
        self.bar.close()
        
    def __len__(self):
        return self.file_size

def upload_to_gofile(file_path):
    try:
        # 1. Get server details
        server_resp = requests.get("https://api.gofile.io/servers", timeout=10)
        server_resp.raise_for_status()
        server_data = server_resp.json()
        if server_data.get("status") != "ok":
            print(f"Error: Gofile returned status {server_data.get('status')} when requesting servers.", file=sys.stderr)
            return None
            
        servers = server_data.get("data", {}).get("servers", [])
        if not servers:
            print("Error: No servers available from Gofile API.", file=sys.stderr)
            return None
            
        # Select first server
        server_name = servers[0].get("name")
        upload_url = f"https://{server_name}.gofile.io/contents/uploadfile"
        
        # 2. Upload file with progress tracking
        reader = ProgressFileReader(file_path)
        files = {
            'file': (reader.filename, reader)
        }
        
        # Check if we have a token in the configuration
        gofile_token = getattr(config, "gofile", {}).get("token")
        if gofile_token:
            files['token'] = (None, gofile_token)
        
        try:
            upload_resp = requests.post(upload_url, files=files, timeout=1200)
            upload_resp.raise_for_status()
            upload_data = upload_resp.json()
            if upload_data.get("status") != "ok":
                print(f"Error: Gofile upload failed with status {upload_data.get('status')}", file=sys.stderr)
                return None
                
            download_link = upload_data.get("data", {}).get("downloadPage")
            print(f"Upload successful! Link: {download_link}")
            return download_link
        finally:
            reader.close()
    except Exception as e:
        print(f"\nError uploading to Gofile: {e}", file=sys.stderr)
        return None

def main():
    LOG_FORMAT = "{asctime} [{levelname[0]}] {name} : {message}"
    LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
    LOG_STYLE = "{"
    
    coloredlogs.install(
        level=logging.INFO,
        fmt=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        style=LOG_STYLE,
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(description="Stage.in Downloader")
    parser.add_argument("input", nargs="?", help="Stage.in Movie/Show URL or ID")
    parser.add_argument("-g", "--gofile", action="store_true", help="Upload downloaded files to Gofile")
    parser.add_argument("-l", "--list", action="store_true", help="List available tracks and exit")
    parser.add_argument("-e", "-w", "--episode", "--wanted", dest="episode", default=None, help="Episode number (e.g. 1, 2) or 'all'")
    args = parser.parse_args()

    input_str = args.input
    if not input_str:
        try:
            input_str = input("Enter Stage.in Movie/Show URL or ID: ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            sys.exit(0)

    if not input_str:
        log.error("Error: No input provided.")
        sys.exit(1)

    dialect, content_type, content_id, ep_from_url = parse_input(input_str)
    episode = args.episode or ep_from_url
    if not content_id:
        log.error(f"Error: Could not extract a valid numeric ID from input: '{input_str}'")
        sys.exit(1)

    cookies = load_cookies()
    dialect_code = get_dialect_code(dialect, cookies)
    
    headers = {
        "app_id": "in.stage.web",
        "platform": "web",
        "os": "android",
        "api-version": "2",
        "dialect": dialect_code,
        "lang": cookies.get("lang") or "en"
    }
    if "tokenv1" in cookies:
        headers["Authorization"] = f"Bearer {cookies['tokenv1']}"

    api_root = "https://stageapi.stage.in"
    final_paths = []
    binaries_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "binaries"))

    movie_data = None
    show_data = None

    if content_type == "movie":
        url = f"{api_root}/nest/contents/movies/details"
        resp = requests.get(url, params={"movieId": content_id}, headers=headers, timeout=10)
        if resp.status_code == 200:
            movie_data = resp.json().get("movie") or {}
        if not movie_data:
            resp_show = requests.get(f"{api_root}/nest/contents/shows/details", params={"showId": content_id, "seasonId": ""}, headers=headers, timeout=10)
            if resp_show.status_code == 200 and resp_show.json().get("data"):
                content_type = "show"
                show_data = resp_show.json().get("data") or {}
    else:
        url = f"{api_root}/nest/contents/shows/details"
        resp = requests.get(url, params={"showId": content_id, "seasonId": ""}, headers=headers, timeout=10)
        if resp.status_code == 200:
            show_data = resp.json().get("data") or {}
        if not show_data:
            resp_mov = requests.get(f"{api_root}/nest/contents/movies/details", params={"movieId": content_id}, headers=headers, timeout=10)
            if resp_mov.status_code == 200 and resp_mov.json().get("movie"):
                content_type = "movie"
                movie_data = resp_mov.json().get("movie") or {}

    if content_type == "movie":
        if not movie_data:
            log.error(f"Error: Empty movie details returned for ID {content_id}.")
            sys.exit(1)

        meta_lang = movie_data.get("language")
        if meta_lang:
            dialect_code = _DIALECT_NAME_TO_CODE.get(meta_lang.lower()) or meta_lang.lower()
            if dialect_code == "hindi":
                dialect_code = "hin"

        title_name = movie_data.get("title")
        log.info("Retrieving Titles")
        
        # Create Title object
        title_obj = Title(
            id_=str(movie_data.get("_id") or content_id),
            type_=Title.Types.MOVIE,
            name=title_name,
            year=int(movie_data.get("yearOfRelease") or 0),
            source="STAGE",
        )
        
        logging.getLogger("Titles").info(f"Title: {title_obj.name}")
        
        # Get raw source link
        full_source = movie_data.get("sourceLink")
        video_url = None
        if full_source and full_source.strip():
            video_url = f"https://media.stage.in/episode/main-video/{full_source.strip()}"
        else:
            sp = movie_data.get("selectedPeripheral", {})
            peripheral_source = sp.get("sourceLink") or sp.get("visionularHls", {}).get("sourceLink")
            if peripheral_source and peripheral_source.strip() and not peripheral_source.strip().endswith(".m3u8"):
                video_url = f"https://media.stage.in/episode/main-video/{peripheral_source.strip()}"

        if not video_url:
            log.error("Error: No direct/raw MP4 video link found for this movie.")
            sys.exit(1)
        
        log.info(f"Getting tracks for {title_obj.name}{f' ({title_obj.year})' if title_obj.year else ''} [{title_obj.id}]")
        
        # Probe and setup tracks
        probe_and_setup_tracks(title_obj, video_url, headers, dialect_code, movie_data.get("subtitle") or {})
        
        if args.list:
            log.info("Processed all titles!")
            sys.exit(0)
        
        # Setup temporary destination
        temp_file = os.path.join(directories.temp, f"stage_movie_{content_id}.mp4")
        download_file(video_url, headers, temp_file)
        
        # Generate mock media info using parsed track details
        media_info = create_mock_media_info(title_obj)
        
        # Generate filename from VT logic
        filename = title_obj.parse_filename(media_info=media_info)
        filename = filename.replace(".WEB-DL.", ".RAW-WEB-DL.")
        
        # Determine language code to tag the audio track
        audio_lang = "bgc" if dialect_code == "har" else (dialect_code or "hin")
        
        # Mux to MKV container
        temp_mkv = os.path.join(directories.temp, f"stage_movie_{content_id}.mkv")
        ffmpeg_path = shutil.which("ffmpeg") or os.path.join(binaries_dir, "ffmpeg.exe")
        log.info(f"Muxing to MKV container (setting audio language to {audio_lang})...")
        try:
            subprocess.run([
                ffmpeg_path,
                "-y",
                "-i", temp_file,
                "-c", "copy",
                "-metadata:s:a:0", f"language={audio_lang}",
                temp_mkv
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.remove(temp_file)
            src_file = temp_mkv
        except Exception as e:
            log.warning(f"Warning: Failed to mux to MKV container: {e}. Moving raw file.")
            src_file = temp_file

        # Final output destination
        final_dir = directories.downloads
        os.makedirs(final_dir, exist_ok=True)
        ext = ".mkv" if src_file.endswith(".mkv") else ".mp4"
        final_path = os.path.join(final_dir, f"{filename}{ext}")
        
        shutil.move(src_file, final_path)
        log.info(f"File saved successfully to: {final_path}")
        final_paths.append(final_path)

        # Download sidecar subtitles if available
        subtitles_dict = movie_data.get("subtitle") or {}
        if isinstance(subtitles_dict, dict) and any(subtitles_dict.values()):
            movie_slug = movie_data.get("slug") or title_obj.name.lower()
            url_slug = get_url_slug(movie_slug, content_id)
            dialect_name = _DIALECT_CODE_TO_NAME.get(dialect_code, "hindi")
            watch_path = f"/en/{dialect_name}/movie/watch/{url_slug}"
            cf_auth = get_cloudfront_auth(watch_path, cookies, headers, dialect_name)
            if not cf_auth:
                log.warning("Warning: Failed to fetch CloudFront signature (check if your subscription has expired). Subtitle download will likely fail.")
            
            for lang, srt_filename in subtitles_dict.items():
                if srt_filename and srt_filename.strip():
                    base_url_no_ext = video_url.rsplit(".", 1)[0]
                    srt_url = f"{base_url_no_ext}/{srt_filename.strip()}"
                    if cf_auth:
                        srt_url = f"{srt_url}?{cf_auth}"
                    sub_filename = f"{filename}.{lang}.srt"
                    sub_dest = os.path.join(final_dir, sub_filename)
                    log.info(f"Downloading subtitle ({lang}): {srt_url}")
                    try:
                        download_file(srt_url, headers, sub_dest)
                        log.info(f"Subtitle saved to: {sub_dest}")
                        final_paths.append(sub_dest)
                    except Exception as e:
                        log.warning(f"Warning: Failed to download subtitle ({lang}): {e}")

    else: # show
        if not show_data:
            log.error(f"Error: Empty show details returned for ID {content_id}.")
            sys.exit(1)

        meta_lang = show_data.get("language")
        if meta_lang:
            dialect_code = _DIALECT_NAME_TO_CODE.get(meta_lang.lower()) or meta_lang.lower()
            if dialect_code == "hindi":
                dialect_code = "hin"

        show_title = show_data.get("title")
        log.info("Retrieving Titles")
        logging.getLogger("Titles").info(f"Title: {show_title}")
        episodes = show_data.get("episodes", [])
        if not episodes:
            log.error(f"Error: No episodes found for show '{show_title}'.")
            sys.exit(1)

        selected_ep = None
        if episode:
            # Look for matching episode
            for ep in episodes:
                ep_order = ep.get("episodeOrder") or ep.get("order")
                if ep_order and str(ep_order) == str(episode):
                    selected_ep = ep
                    break
            if not selected_ep:
                log.warning(f"Warning: Episode {episode} not found in episode list. Falling back to selection.")
        
        if not selected_ep:
            if args.list or (episode and str(episode).lower() == 'all'):
                eps_to_dl = episodes
            else:
                print(f"\nShow: {show_title} ({len(episodes)} Episodes)")
                for i, ep in enumerate(episodes):
                    ep_num = ep.get("episodeOrder") or ep.get("order") or (i + 1)
                    ep_title = ep.get("title") or f"Episode {ep_num}"
                    print(f"  [{i+1}] Episode {ep_num}: {ep_title}")
                
                try:
                    choice = input(f"\nSelect episode to download (e.g. 3 / 6-8 / 1,3,5 / all): ").strip()
                except (KeyboardInterrupt, EOFError):
                    print()
                    sys.exit(0)

                if choice.lower() == 'all':
                    eps_to_dl = episodes
                else:
                    # Parse single numbers, ranges (6-8), comma lists (1,3,5),
                    # and mixed (1,3-5,7).  All are 1-based menu indices.
                    selected_indices = set()
                    try:
                        for part in choice.split(","):
                            part = part.strip()
                            if "-" in part:
                                lo, hi = part.split("-", 1)
                                lo, hi = int(lo.strip()), int(hi.strip())
                                selected_indices.update(range(lo, hi + 1))
                            else:
                                selected_indices.add(int(part))
                        eps_to_dl = []
                        for menu_idx in sorted(selected_indices):
                            real_idx = menu_idx - 1  # convert 1-based to 0-based
                            if 0 <= real_idx < len(episodes):
                                eps_to_dl.append(episodes[real_idx])
                            else:
                                log.error(f"Error: Episode {menu_idx} is out of range (1-{len(episodes)}).")
                                sys.exit(1)
                        if not eps_to_dl:
                            log.error("Error: No valid episodes selected.")
                            sys.exit(1)
                    except ValueError:
                        log.error("Error: Invalid input. Use a number (3), range (6-8), or comma list (1,3,5).")
                        sys.exit(1)

        else:
            eps_to_dl = [selected_ep]

        for ep in eps_to_dl:
            ep_num = ep.get("episodeOrder") or ep.get("order") or 1
            ep_title = ep.get("title") or f"Episode {ep_num}"
            
            # Create Title object
            title_obj = Title(
                id_=str(ep.get("id") or ep.get("_id") or ""),
                type_=Title.Types.TV,
                name=show_title,
                year=int(show_data.get("yearOfRelease") or 0),
                season=1,
                episode=int(ep_num),
                episode_name=ep_title,
                source="STAGE",
            )
            
            log.info("Getting tracks for {} S{:02}E{:02}{} [{}]".format(
                title_obj.name,
                title_obj.season,
                title_obj.episode,
                f" - {title_obj.episode_name}" if title_obj.episode_name else "",
                title_obj.id
            ))
            
            # Get raw source link
            full_source = ep.get("sourceLink")
            video_url = None
            if full_source and full_source.strip():
                video_url = f"https://media.stage.in/episode/main-video/{full_source.strip()}"
            else:
                sp = ep.get("selectedPeripheral", {})
                peripheral_source = sp.get("sourceLink") or sp.get("visionularHls", {}).get("sourceLink")
                if peripheral_source and peripheral_source.strip() and not peripheral_source.strip().endswith(".m3u8"):
                    video_url = f"https://media.stage.in/show/main-video/{peripheral_source.strip()}"

            if not video_url:
                log.warning(f"Warning: No direct/raw MP4 video link found for Episode {ep_num}. Skipping...")
                continue

            # Probe and setup tracks
            probe_and_setup_tracks(title_obj, video_url, headers, dialect_code, ep.get("subtitle") or {})

            if args.list:
                continue

            # Setup temporary destination
            temp_file = os.path.join(directories.temp, f"stage_show_{content_id}_ep_{ep_num}.mp4")
            download_file(video_url, headers, temp_file)
            
            # Generate mock media info using parsed track details
            media_info = create_mock_media_info(title_obj)
            
            # Generate filename from VT logic
            filename = title_obj.parse_filename(media_info=media_info)
            filename = filename.replace(".WEB-DL.", ".RAW-WEB-DL.")
            folder_name = title_obj.parse_filename(media_info=media_info, folder=True)
            folder_name = folder_name.replace(".WEB-DL.", ".RAW-WEB-DL.")
            
            # Determine language code to tag the audio track
            audio_lang = "bgc" if dialect_code == "har" else (dialect_code or "hin")
            
            # Mux to MKV container
            temp_mkv = os.path.join(directories.temp, f"stage_show_{content_id}_ep_{ep_num}.mkv")
            ffmpeg_path = shutil.which("ffmpeg") or os.path.join(binaries_dir, "ffmpeg.exe")
            log.info(f"Muxing to MKV container (setting audio language to {audio_lang})...")
            try:
                subprocess.run([
                    ffmpeg_path,
                    "-y",
                    "-i", temp_file,
                    "-c", "copy",
                    "-metadata:s:a:0", f"language={audio_lang}",
                    temp_mkv
                ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                os.remove(temp_file)
                src_file = temp_mkv
            except Exception as e:
                log.warning(f"Warning: Failed to mux to MKV container: {e}. Moving raw file.")
                src_file = temp_file

            # Final output destination
            final_dir = os.path.join(directories.downloads, folder_name)
            os.makedirs(final_dir, exist_ok=True)
            ext = ".mkv" if src_file.endswith(".mkv") else ".mp4"
            final_path = os.path.join(final_dir, f"{filename}{ext}")
            
            shutil.move(src_file, final_path)
            log.info(f"File saved successfully to: {final_path}")
            final_paths.append(final_path)

            # Download sidecar subtitles if available
            subtitles_dict = ep.get("subtitle") or {}
            if isinstance(subtitles_dict, dict) and any(subtitles_dict.values()):
                show_slug = show_data.get("slug") or title_obj.name.lower()
                url_slug = get_url_slug(show_slug, content_id)
                dialect_name = _DIALECT_CODE_TO_NAME.get(dialect_code, "haryanvi")
                watch_path = f"/en/{dialect_name}/show/watch/{url_slug}/{ep_num}"
                cf_auth = get_cloudfront_auth(watch_path, cookies, headers, dialect_name)
                if not cf_auth:
                    log.warning("Warning: Failed to fetch CloudFront signature (check if your subscription has expired). Subtitle download will likely fail.")
                
                for lang, srt_filename in subtitles_dict.items():
                    if srt_filename and srt_filename.strip():
                        base_url_no_ext = video_url.rsplit(".", 1)[0]
                        srt_url = f"{base_url_no_ext}/{srt_filename.strip()}"
                        if cf_auth:
                            srt_url = f"{srt_url}?{cf_auth}"
                        sub_filename = f"{filename}.{lang}.srt"
                        sub_dest = os.path.join(final_dir, sub_filename)
                        log.info(f"Downloading subtitle ({lang}): {srt_url}")
                        try:
                            download_file(srt_url, headers, sub_dest)
                            log.info(f"Subtitle saved to: {sub_dest}")
                            final_paths.append(sub_dest)
                        except Exception as e:
                            log.warning(f"Warning: Failed to download subtitle ({lang}): {e}")

    if args.list:
        log.info("Processed all titles!")
        sys.exit(0)

    log.info("Processed all titles!")

    # Gofile Upload Phase
    gofile_upload = args.gofile
    if not gofile_upload and final_paths:
        try:
            choice = input("\nDo you want to upload the downloaded file(s) to Gofile? (y/n): ").strip().lower()
            if choice == 'y' or choice == 'yes':
                gofile_upload = True
        except (KeyboardInterrupt, EOFError):
            print()

    if gofile_upload and final_paths:
        print("\n--- Gofile Uploads ---")
        for path in final_paths:
            if os.path.exists(path):
                upload_to_gofile(path)

if __name__ == "__main__":
    main()
