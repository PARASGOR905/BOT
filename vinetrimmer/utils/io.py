import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import sys
import httpx
import pproxy
import requests
import yaml
import tqdm
import logging
from urllib.parse import urlsplit
from pathlib import Path
from sys import platform

from vinetrimmer import config
from vinetrimmer.utils.collections import as_list

if "win" not in platform:
    import shlex


def load_yaml(path):
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fd:
        return yaml.safe_load(fd)


def _mk_proxies(proxy: str | None):
    """Return requests 'proxies' dict or None."""
    if not proxy:
        return None
    if proxy.startswith("https://"):
        return {"https": proxy}
    return {"http": proxy}


def _safe_is_akamai(uri) -> bool:
    """Return True if 'akamai' appears in the URL (string). If list, check first element."""
    if isinstance(uri, (list, tuple)) and uri:
        uri = uri[0]
    if not isinstance(uri, str):
        return False
    return "akamai" in uri


_ip_info = None


def get_ip_info(session=None, fresh=False):
    """Use ipwho.is to get IP location information."""
    global _ip_info
    if fresh or not _ip_info:
        _ip_info = (session or httpx).get("https://ipwho.is/").json()
    return _ip_info


@contextlib.asynccontextmanager
async def start_pproxy(host, port, username=None, password=None):
    # Re-route HTTPS proxy via local HTTP
    rerouted_proxy = "http://localhost:8081"
    server = pproxy.Server(rerouted_proxy)
    auth = ""
    if username or password:
        auth = f"#{username or ''}:{password or ''}"
    remote = pproxy.Connection(f"http+ssl://{host}:{port}{auth}")
    handler = await server.start_server(dict(rserver=[remote]))
    try:
        yield rerouted_proxy
    finally:
        handler.close()
        await handler.wait_closed()


def download_range(url, count, start=0, proxy=None):
    """Download n bytes without using the Range header due to support issues."""
    executable = shutil.which("curl")
    if not executable:
        raise EnvironmentError("Track needs curl to download a chunk of data but wasn't found...")

    arguments = [
        executable,
        "-s",          # quiet
        "-L",          # follow redirects
        "--proxy-insecure",
        "--output", "-",
        "--url", url
    ]
    if proxy:
        arguments.extend(["--proxy", proxy])

    curl = subprocess.Popen(
        arguments,
        stdout=subprocess.PIPE,
        stderr=open(os.devnull, "wb"),
        shell=False
    )
    buffer = b""
    location = -1
    while len(buffer) < count:
        stdout = curl.stdout
        data = b""
        if stdout:
            data = stdout.read(1)
        if data:
            location += len(data)
            if location >= start:
                buffer += data
        else:
            if curl.poll() is not None:
                break
    curl.kill()
    return buffer


async def aria2c(uri, out, headers=None, proxy=None):
    """
    Downloads file(s) using Aria2(c).
    If uri is a list, segments will be merged into a single file.
    """
    executable = shutil.which("aria2c") or shutil.which("aria2")
    if not executable:
        raise EnvironmentError("Aria2c executable not found...")

    aria2c_cfg = getattr(getattr(config, "config", object()), "aria2c", {}) or {}

    max_conn = str(aria2c_cfg.get("max_connection_per_server", aria2c_cfg.get("x", "4")))
    split_conn = str(aria2c_cfg.get("split", aria2c_cfg.get("s", "4")))
    concurrent = str(aria2c_cfg.get("max_concurrent_downloads", aria2c_cfg.get("j", "4")))
    file_alloc = aria2c_cfg.get("file_allocation", "none" if sys.platform == "win32" else "falloc")

    segmented = isinstance(uri, list)
    raw_uris = list(uri) if segmented else []
    segments_dir = f"{out}_segments"

    # Amazon's CVBR audio MPD encodes SegmentList entries with a mediaRange
    # attribute and no media attribute.  The MPD parser falls back to repeating
    # rep_base_url (the full file URL) for every segment, producing a list of
    # N identical URLs.  Trying to download them all concurrently would attempt
    # to fetch the same ~558 MB file 1880 times and fail.
    # Detect this situation (segmented list where every URL is the same
    # akamaized.net/amazon URL) and collapse it to a single-URL download so the
    # _is_amzn_segbase path below handles it as one continuous connection.
    if segmented and raw_uris:
        _first_base = raw_uris[0].split("?")[0]
        if len(raw_uris) > 1 and all(u.split("?")[0] == _first_base for u in raw_uris):
            uri = next((u for u in raw_uris if "?" in u), raw_uris[0])
            segmented = False
            raw_uris = []

    arguments = [
        executable,
        "--remote-time",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--retry-wait", "2",
        "--max-tries", "10",
        "--max-file-not-found", "10",
        "--summary-interval", "0",
        "--file-allocation", file_alloc,
        "--console-log-level", "warn",
        "--download-result", "hide",
        "--allow-piece-length-change=true",
    ]

    if segmented:
        # For segmented chunks (.m4s/cmaf), download 1 connection per segment to avoid CDN range mismatch
        arguments.extend([
            "-j", str(max(int(concurrent), 16)),
            "-x", "1",
            "-s", "1",
            "--deferred-input=true",
            "--auto-save-interval=0",
            "--remove-control-file=true",
        ])
    else:
        # Detect AMZN single-URL (SegmentBase/BaseURL) downloads.
        # Both Amazon's Akamai CDN and Fastly CDN cap byte-range responses at
        # ~2.88 GB regardless of the real file size. When aria2c splits the
        # download (-s N, N>1) it issues parallel Range requests and the CDN
        # returns a mismatched Content-Length per piece, causing an abort:
        #   "total length mismatch. expected: 7410932879, actual: 2903248408"
        # Fix: force -x 1 -s 1 so the file is fetched in a single continuous
        # connection without any Range splitting.
        _uri_str = uri if isinstance(uri, str) else (uri[0] if uri else "")
        _is_amzn_segbase = (
            ("akamaized.net" in _uri_str and "amazon" in _uri_str)
            or ("fastly-edge.com" in _uri_str and "amazon" in _uri_str)
            or ("vod-dash-pv" in _uri_str and "fastly" in _uri_str)
            or "cloudfront.net" in _uri_str  # Amazon India / other regions use CloudFront
            or "pv-dispatch" in _uri_str     # Amazon PV dispatch CDN
            or "amazon-vod" in _uri_str      # Generic Amazon VOD CDN
            # Also detect by output filename: AMZN unencrypted tracks get _dec suffix
            or (os.path.basename(out).startswith(("VideoTrack_", "AudioTrack_")) and "_dec.mp4" in out)
        )
        if _is_amzn_segbase:
            # Do NOT use -c (resume) for AMZN segbase downloads.
            # After a successful decrypt+repackage, track.swap() replaces
            # enc.mp4 with the decrypted output.  On the next run, aria2c
            # with -c would append encrypted bytes onto the decrypted file,
            # producing a corrupt hybrid that neither packager nor mp4decrypt
            # can parse (Bento4 AP4_ERROR_IO = -5).
            # --allow-overwrite=true (already in base args) handles the overwrite.
            arguments.extend([
                "--remove-control-file=true",
                "-o", os.path.basename(out),
                "-x", "1",
                "-j", "1",
                "-s", "1",
            ])

        else:
            arguments.extend([
                "-c",
                "-o", os.path.basename(out),
                "-x", max_conn,
                "-j", concurrent,
                "-s", split_conn,
                "--min-split-size", "1M",
            ])


    # Headers (skip accept-encoding and connection)
    for header, value in (headers or {}).items():
        if header.lower() in ("accept-encoding", "connection"):
            continue
        arguments.extend(["--header", f"{header}: {value}"])

    skip_aria2_segmented = False
    if segmented:
        os.makedirs(segments_dir, exist_ok=True)
        downloaded_set = set(f for f in os.listdir(segments_dir) if re.match(r"^\d{8}\.mp4$", f)) if os.path.exists(segments_dir) else set()
        missing_indices = [idx for idx in range(len(raw_uris)) if f"{idx:08}.mp4" not in downloaded_set]
        if not missing_indices and raw_uris:
            skip_aria2_segmented = True
            log = logging.getLogger("Aria2c")
            log.info(f" - All {len(raw_uris)} segments already downloaded, proceeding to merge.")
        else:
            fetch_indices = missing_indices if downloaded_set else list(range(len(raw_uris)))
            # aria2c input file format (stdin via -i-)
            uri = "\n".join([
                f"{raw_uris[i]}\n"
                f"\tdir={segments_dir}\n"
                f"\tout={i:08}.mp4"
                for i in fetch_indices
            ])

    # Proxy handling (support https proxy via pproxy)
    if proxy:
        arguments.append("--all-proxy")
        if proxy.lower().startswith("https://"):
            auth, hostname = proxy[8:].split("@")
            async with start_pproxy(*hostname.split(":"), *auth.split(":")) as pproxy_:
                arguments.extend([pproxy_, "-d"])
                if segmented:
                    if not skip_aria2_segmented:
                        arguments.extend([segments_dir, "-i-"])
                        proc = await asyncio.create_subprocess_exec(*arguments, stdin=subprocess.PIPE)
                        await proc.communicate(as_list(uri)[0].encode("utf-8"))
                else:
                    arguments.extend([os.path.dirname(out), uri])
                    proc = await asyncio.create_subprocess_exec(*arguments)
                    await proc.communicate()
        else:
            arguments.append(proxy)

    if not skip_aria2_segmented:
        try:
            if segmented:
                subprocess.run(
                    arguments + ["-d", segments_dir, "-i-"],
                    input=as_list(uri)[0],
                    encoding="utf-8",
                    check=True
                )
            else:
                subprocess.run(
                    arguments + ["-d", os.path.dirname(out), uri],
                    check=True
                )
        except subprocess.CalledProcessError as e:
            if segmented:
                if os.path.exists(segments_dir):
                    downloaded_set = set(f for f in os.listdir(segments_dir) if re.match(r"^\d{8}\.mp4$", f))
                    missing_indices = [idx for idx in range(len(raw_uris)) if f"{idx:08}.mp4" not in downloaded_set]
                    if missing_indices:
                        log = logging.getLogger("Aria2c")
                        log.warning(f" - Warning: {len(missing_indices)}/{len(raw_uris)} segments failed to download (exit code {e.returncode}). Retrying missing segments...")
                        for attempt in range(1, 4):
                            if not missing_indices:
                                break
                            retry_input = "\n".join([
                                f"{raw_uris[idx]}\n\tdir={segments_dir}\n\tout={idx:08}.mp4"
                                for idx in missing_indices
                            ])
                            try:
                                subprocess.run(
                                    arguments + ["-d", segments_dir, "-i-"],
                                    input=retry_input,
                                    encoding="utf-8",
                                    check=True
                                )
                            except subprocess.CalledProcessError:
                                pass
                            downloaded_set = set(f for f in os.listdir(segments_dir) if re.match(r"^\d{8}\.mp4$", f))
                            missing_indices = [idx for idx in range(len(raw_uris)) if f"{idx:08}.mp4" not in downloaded_set]

                        if missing_indices:
                            log.warning(f" - Aria2c failed on {len(missing_indices)} segments. Falling back to Python downloader...")
                            import requests
                            import time
                            req_headers = {
                                k: v for k, v in (headers or {}).items()
                                if k.lower() not in ("accept-encoding", "connection")
                            }
                            req_proxies = {"all": proxy} if proxy else None
                            for idx in list(missing_indices):
                                seg_url = raw_uris[idx]
                                seg_file = os.path.join(segments_dir, f"{idx:08}.mp4")
                                success = False
                                for req_try in range(3):
                                    try:
                                        res = requests.get(seg_url, headers=req_headers, proxies=req_proxies, timeout=20)
                                        if res.status_code == 200:
                                            with open(seg_file, "wb") as sf:
                                                sf.write(res.content)
                                            success = True
                                            break
                                        else:
                                            log.warning(f" - Segment {idx} HTTP {res.status_code}")
                                    except Exception as req_err:
                                        log.warning(f" - Segment {idx} error: {req_err}")
                                    time.sleep(1)
                                if success:
                                    missing_indices.remove(idx)

                        if missing_indices:
                            raise ValueError(f"Failed to download {len(missing_indices)} segments after retries and fallback")
                    else:
                        log = logging.getLogger("Aria2c")
                        log.info(" - All segments present despite non-zero exit code.")
                else:
                    raise ValueError("Aria2c failed to download any segments")

            else:
                aria2_file = f"{out}.aria2"
                if os.path.exists(out) or os.path.exists(aria2_file):
                    log = logging.getLogger("Aria2c")
                    log.warning(f" - Warning: Aria2c failed (exit code {e.returncode}). Cleaning up stale/mismatched output and .aria2 control files and retrying...")
                    if os.path.exists(out):
                        try:
                            os.remove(out)
                        except Exception:
                            pass
                    if os.path.exists(aria2_file):
                        try:
                            os.remove(aria2_file)
                        except Exception:
                            pass
                    try:
                        subprocess.run(
                            arguments + ["-d", os.path.dirname(out), uri],
                            check=True
                        )
                    except subprocess.CalledProcessError as e2:
                        raise ValueError(f"Aria2c failed to download the file (exit code {e2.returncode})")
                else:
                    raise ValueError(f"Aria2c failed to download the file (exit code {e.returncode})")

    if segmented:
        # merge the segments together
        with open(out, "wb") as ofd:
            for file in sorted(os.listdir(segments_dir)):
                if not re.match(r"^\d{8}\.mp4$", file):
                    continue
                file_path = os.path.join(segments_dir, file)
                try:
                    with open(file_path, "rb") as ifd:
                        data = ifd.read()
                except (FileNotFoundError, OSError):
                    continue
                # Apple TV+ needs this done to fix audio decryption
                data = re.sub(b"(tfhd\x00\x02\x00\x1a\x00\x00\x00\x01\x00\x00\x00)\x02", b"\\g<1>\x01", data)
                ofd.write(data)
                try:
                    os.unlink(file_path)
                except Exception:
                    pass
        import time
        for _ in range(5):
            try:
                shutil.rmtree(segments_dir)
                break
            except Exception:
                time.sleep(0.2)
        else:
            shutil.rmtree(segments_dir, ignore_errors=True)

    print()


async def saldl(uri, out, headers=None, proxy=None):
    # Clean headers (don't mutate original)
    if headers:
        headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}

    executable = shutil.which("saldl") or shutil.which("saldl-win64") or shutil.which("saldl-win32")
    if not executable:
        raise EnvironmentError("Saldl executable not found...")

    arguments = [
        executable,
        "--skip-TLS-verification",
        "--resume",
        "--merge-in-order",
        "-c8",
        "--auto-size", "1",
        "-D", os.path.dirname(out),
        "-o", os.path.basename(out),
    ]

    if headers:
        arguments.extend([
            "--custom-headers",
            "\r\n".join([f"{k}: {v}" for k, v in headers.items()])
        ])

    if proxy:
        arguments.extend(["--proxy", proxy])

    if isinstance(uri, list):
        raise ValueError("Saldl code does not yet support multiple uri (e.g. segmented) downloads.")
    arguments.append(as_list(uri)[0])

    try:
        subprocess.run(arguments, check=True)
    except subprocess.CalledProcessError:
        raise ValueError("Saldl failed too many times, aborting")

    print()


async def tqdm_downloader(uri, out, headers=None, proxy=None):
    proxies = _mk_proxies(proxy)
    url = as_list(uri)[0]
    req_headers = dict(headers or {})
    req_headers.pop("Range", None)
    req_headers.pop("range", None)

    with requests.get(url, headers=req_headers, proxies=proxies, stream=True, timeout=60) as r:
        r.raise_for_status()
        size_hdr = r.headers.get("Content-Length")
        file_size = int(size_hdr) if size_hdr else None
        chunk_size = 1024 * 1024

        with open(out, "wb") as fp:
            with tqdm.tqdm(
                total=file_size,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=os.path.basename(out),
                leave=True,
            ) as pbar:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if chunk:
                        fp.write(chunk)
                        pbar.update(len(chunk))
    print()


async def ffmpeg_download(uri, out, headers=None, proxy=None):
    executable = shutil.which("ffmpeg")
    if not executable:
        raise EnvironmentError("ffmpeg executable not found...")

    if isinstance(uri, (list, tuple)) and uri:
        uri = uri[0]

    headers_list = []
    if headers:
        for k, v in headers.items():
            headers_list.append(f"{k}: {v}")
    headers_str = "\r\n".join(headers_list) + "\r\n"

    # Determine suitable suffix based on extension
    # If the output suffix is not .ts, it is safer to write to .ts first then remux, 
    # as ffmpeg copy to mp4 sometimes fails due to MPEG-TS streams containing H.263/H.264 packets 
    # that don't conform to strict mp4 muxer rules.
    tmp_out = out
    if not out.endswith(".ts"):
        tmp_out = os.path.splitext(out)[0] + ".ts"

    arguments = [
        executable,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-headers", headers_str,
        "-i", uri,
        "-c", "copy",
        tmp_out
    ]

    env = os.environ.copy()
    if proxy:
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy

    proc = await asyncio.create_subprocess_exec(*arguments, env=env)
    await proc.communicate()
    if proc.returncode != 0:
        raise ValueError(f"ffmpeg download failed with exit status {proc.returncode}")

    if tmp_out != out:
        # Remux/rename to final path
        if os.path.exists(tmp_out):
            if os.path.exists(out):
                os.remove(out)
            os.rename(tmp_out, out)


async def m3u8re(uri, out, track, headers=None, proxy=None):
    executable = shutil.which("N_m3u8DL-RE") or shutil.which("m3u8DL")
    if not executable:
        raise EnvironmentError("N_m3u8DL-RE executable not found...")

    # If uri is list, use the first one (N_m3u8DL-RE expects one URL)
    if isinstance(uri, (list, tuple)) and uri:
        uri = uri[0]

    save_dir = os.path.dirname(out)
    track_name = os.path.splitext(os.path.basename(out))[0]
    tmp_dir = os.path.join(save_dir, "_re_temp", track_name)
    os.makedirs(tmp_dir, exist_ok=True)

    ffmpeg_binary = shutil.which("ffmpeg")
    arguments = [
        executable,
        track.original_url or uri,
        "--save-dir", save_dir,
        "--tmp-dir", tmp_dir,
        "--save-name", track_name,
        "--write-meta-json", "False",
        "--log-level", "ERROR",
        "--thread-count", "16",
        "--download-retry-count", "5",
        "--http-request-timeout", "15",
        "--ffmpeg-binary-path", ffmpeg_binary,
    ]

    if getattr(track, "source", None) not in ("STAGE", "stagein", "SI"):
        arguments.append("--binary-merge")

    if headers:
        for k, v in headers.items():
            arguments.extend(["--header", f"{k}: {v}"])

    # For CHORKI, HLS streams use AES-128 encryption even though vinetrimmer reports
    # them as "Unencrypted" (no Widevine/Playready). The Chorki KMS requires session
    # cookies that N_m3u8DL-RE cannot forward, so chorki.py's _prefetch_hls_keys()
    # fetches the key via the authenticated session and stores it on the track.
    if getattr(track, "source", None) == "CHORKI":
        hls_key = getattr(track, "hls_key_hex", None)
        if hls_key:
            arguments.extend(["--custom-hls-method", "AES_128",
                              "--custom-hls-key", hls_key])
            hls_iv = getattr(track, "hls_key_iv", None)
            if hls_iv:
                arguments.extend(["--custom-hls-iv", hls_iv])
            logging.getLogger("CHORKI").debug("Using pre-fetched AES-128 key for N_m3u8DL-RE")
        elif getattr(track, "encrypted", False):
            logging.getLogger("CHORKI").warning(
                "No pre-fetched HLS key on track — N_m3u8DL-RE will attempt key fetch "
                "and likely fail with 403. Check that your cookies are valid and have "
                "subscription access to this content."
            )

    if _safe_is_akamai(uri) and getattr(track, "source", None) == "DSPA":
        arguments.extend(["--max-speed", "50M"])

    if track.source == "VDO":
        arguments.extend(["-M", "format=mp4",])

    if proxy:
        arguments.extend(["--custom-proxy", proxy])

    # Track-specific selections
    tname = track.__class__.__name__
    if tname == "VideoTrack":
        if track.source in ("NOW", "nowtvuk", "nowtvit"):
            if getattr(track, "height", None):
                arguments.extend(["-sv", f"res='{track.height}*':for=best"])
            else:
                arguments.extend(["-sv", "best"])
        elif getattr(track, "height", None) and getattr(track, "codec", None):
            if track.source == "VIU":
                arguments.extend([
                    "-M", "format=mp4",
                ])
            elif track.source in ("CHORKI", "KABLEONE", "TARANGPLUS", "tarangplus", "tarang"):
                # Single media playlists, select all
                pass
            else:
                arguments.extend([
                    "-sv", f"res='{track.height}*':codec='{track.codec}':for=best",
                ])
        else:
            if track.source not in ("CHORKI", "KABLEONE", "TARANGPLUS", "tarangplus", "tarang"):
                arguments.extend(["-sv", "best"])

        if track.source not in ("CHORKI", "KABLEONE", "TARANGPLUS", "tarangplus", "tarang"):
            arguments.extend([
                "-da", "all",
                "-ds", "all",
            ])

    elif tname == "AudioTrack":
        if track.source in ("NOW", "nowtvuk", "nowtvit"):
            arguments.extend(["-sa", "all"])
        else:
            lang = getattr(track, "language", None)
            if getattr(track, "extra", None) and hasattr(track.extra, "language") and track.extra.language:
                lang = track.extra.language
            if lang and str(lang) != "und" and track.source not in ("CHORKI", "KABLEONE", "UJHK", "ULTRAJHAKAAS", "UPLAY", "ULTRAPLAY", "ujhk", "ultrajhakaas", "uplay", "ultraplay"):
                arguments.extend(["-sa", f"lang={lang}:for=best"])
            else:
                if track.source not in ("CHORKI", "KABLEONE"):
                    arguments.extend(["-sa", "best"])

        if track.source not in ("CHORKI", "KABLEONE"):
            arguments.extend([
                "-dv", "all",
                "-ds", "all",
            ])
        else:
            arguments.extend([
                "-ds", "all",
            ])
    else:
        raise ValueError(f"{tname} not supported yet!")

    try:
        subprocess.run(arguments, check=True)
    except subprocess.CalledProcessError as e:
        if getattr(track, "source", None) == "CHORKI":
            raise ValueError(
                "Decryption failed or stream is corrupted. "
                "Please verify you have loaded valid premium cookies to chorki.txt for subscription-only content."
            )
        raise e

    # Verify downloaded file is a valid media file (for non-DRM services like Chorki)
    if getattr(track, "source", None) == "CHORKI" and tname in ("VideoTrack", "AudioTrack") and os.path.exists(out):
        ffprobe_bin = shutil.which("ffprobe")
        if ffprobe_bin:
            r = subprocess.run([
                ffprobe_bin, "-v", "error",
                "-show_entries", "format=format_name",
                "-of", "csv=p=0",
                out
            ], capture_output=True)
            if r.returncode != 0:
                raise ValueError(
                    "Decryption failed or stream is corrupted. "
                    "Please verify you have loaded valid premium cookies to chorki.txt for subscription-only content."
                )
