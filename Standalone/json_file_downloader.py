#!/usr/bin/env python3
"""Download ordinary files whose URLs are listed in a JSON manifest.

This supports direct HTTP(S) file links only. It cannot download content that
requires bypassing a login, paywall, DRM, or another access control.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    return name or "download"


def name_from_url(url: str, fallback: str) -> str:
    path_name = Path(unquote(urlparse(url).path)).name
    return safe_name(path_name or fallback)


def parse_manifest(path: Path) -> list[dict[str, str]]:
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        # Exports from streaming tools often contain HLS/DASH track records,
        # rather than downloadable files. Do not treat their playlist URLs as
        # ordinary file downloads, particularly when a DRM block is present.
        titles = data.get("titles")
        if isinstance(titles, dict):
            tracks = [track for title in titles.values() if isinstance(title, dict)
                      for track in (title.get("tracks") or {}).values() if isinstance(track, dict)]
            if any(track.get("drm") for track in tracks):
                raise ValueError(
                    "This is a DRM-protected streaming export, not a direct-file download list. "
                    "Use the service's official offline-download feature instead."
                )
            if any(track.get("descriptor") in {"HLS", "DASH"} for track in tracks):
                raise ValueError(
                    "This is a streaming-playlist export, not a direct-file download list. "
                    "Provide ordinary HTTP(S) file URLs instead."
                )
        data = data.get("downloads", data.get("files", data.get("items")))
    if not isinstance(data, list):
        raise ValueError("JSON must be a list, or an object with downloads/files/items.")

    downloads: list[dict[str, str]] = []
    for index, item in enumerate(data, 1):
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise ValueError(f"Entry {index} needs a string 'url'.")
        url = item["url"]
        if urlparse(url).scheme not in {"http", "https"}:
            raise ValueError(f"Entry {index} is not an HTTP(S) URL: {url}")
        filename = item.get("filename") or item.get("name") or name_from_url(url, f"file-{index}")
        downloads.append({"url": url, "filename": safe_name(str(filename))})
    return downloads


def unique_path(folder: Path, filename: str) -> Path:
    candidate = folder / filename
    stem, suffix = candidate.stem, candidate.suffix
    count = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({count}){suffix}"
        count += 1
    return candidate


def download(url: str, target: Path, retries: int) -> None:
    part = target.with_name(target.name + ".part")
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers={"User-Agent": "JSON-File-Downloader/1.0"})
            with urlopen(request, timeout=60) as response, part.open("wb") as output:
                size = response.headers.get("Content-Length")
                total = int(size) if size and size.isdigit() else None
                received = 0
                while chunk := response.read(1024 * 256):
                    output.write(chunk)
                    received += len(chunk)
                    if total:
                        print(f"\r    {received / total:6.1%} ({received / 1024 / 1024:.1f} MB)", end="", flush=True)
            part.replace(target)
            if total:
                print()
            return
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            if part.exists():
                part.unlink()
            if attempt == retries:
                raise RuntimeError(str(error)) from error
            print(f"\n    Retry {attempt + 1}/{retries}: {error}")
            time.sleep(2 * (attempt + 1))


def main() -> int:
    parser = argparse.ArgumentParser(description="Download direct file URLs from a JSON manifest.")
    parser.add_argument("manifest", type=Path, help="JSON manifest path")
    parser.add_argument("--output", "-o", type=Path, default=Path("downloads"), help="destination folder")
    parser.add_argument("--retries", type=int, default=2, help="retry count per file (default: 2)")
    args = parser.parse_args()

    try:
        entries = parse_manifest(args.manifest)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Manifest error: {error}", file=sys.stderr)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)
    failures = 0
    for number, entry in enumerate(entries, 1):
        target = unique_path(args.output, entry["filename"])
        print(f"[{number}/{len(entries)}] {target.name}")
        try:
            download(entry["url"], target, max(args.retries, 0))
        except RuntimeError as error:
            failures += 1
            print(f"    FAILED: {error}", file=sys.stderr)

    print(f"Done: {len(entries) - failures} downloaded, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
