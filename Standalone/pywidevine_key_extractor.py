#!/usr/bin/env python3
"""
PyWidevine Key Extractor Utility

Extracts Widevine KID:KEY decryption pairs from any OTT service using pywidevine and a .wvd device.

Usage:
    python pywidevine_key_extractor.py --pssh <PSSH> --license <LICENSE_URL> [--device <WVD_DEVICE>] [--header "Header: Value"]

Example:
    python pywidevine_key_extractor.py --pssh "AAAAW3Bzc2g..." --license "https://license-server.com" --device xiaomi_mi_a1_15.0.0_60ceee88_8159_l3.wvd
"""

import os
import sys
import argparse
import requests
from pathlib import Path
from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH

DEVICES_DIR = Path(r"D:\temp\vinetrimmer\devices")


def list_available_devices():
    """Returns list of .wvd files available in devices directory."""
    if not DEVICES_DIR.exists():
        return []
    return [f.name for f in DEVICES_DIR.glob("*.wvd")]


def extract_keys(pssh_b64: str, license_url: str, device_path: Path, headers: dict = None) -> list:
    """Extracts operational decryption keys using pywidevine."""
    if not device_path.exists():
        raise FileNotFoundError(f"Device file not found: {device_path}")

    print(f"[*] Loading device: {device_path.name}")
    device = Device.load(device_path)
    cdm = Cdm.from_device(device)

    print(f"[*] Opening CDM session...")
    session_id = cdm.open()

    try:
        pssh_obj = PSSH(pssh_b64)
        print(f"[*] Building license challenge...")
        challenge = cdm.get_license_challenge(session_id, pssh_obj)

        req_headers = {"User-Agent": "Mozilla/5.0"}
        if headers:
            req_headers.update(headers)

        print(f"[*] Sending challenge to DRM license server: {license_url}")
        resp = requests.post(license_url, data=challenge, headers=req_headers, timeout=15)
        resp.raise_for_status()

        print(f"[*] Parsing license response...")
        cdm.parse_license(session_id, resp.content)

        keys = []
        for key in cdm.get_keys(session_id):
            if key.type == "OPERATIONAL":
                key_str = f"{key.kid.hex()}:{key.key.hex()}"
                keys.append(key_str)
        return keys

    finally:
        cdm.close(session_id)


def main():
    parser = argparse.ArgumentParser(description="PyWidevine Key Extractor")
    parser.add_argument("--pssh", required=True, help="Base64 PSSH string")
    parser.add_argument("--license", required=True, help="DRM License Server URL")
    parser.add_argument("--device", help="Name or path of .wvd device file")
    parser.add_argument("-H", "--header", action="append", help="HTTP header in 'Name: Value' format")

    args = parser.parse_args()

    # Determine device file
    available_devices = list_available_devices()
    if not available_devices:
        print("[!] No .wvd devices found in D:\\temp\\vinetrimmer\\devices")
        sys.exit(1)

    device_file = args.device
    if not device_file:
        device_file = available_devices[0]
        print(f"[*] No --device specified, using default: {device_file}")

    device_path = Path(device_file)
    if not device_path.is_absolute():
        device_path = DEVICES_DIR / device_file

    headers_dict = {}
    if args.header:
        for h in args.header:
            if ":" in h:
                k, v = h.split(":", 1)
                headers_dict[k.strip()] = v.strip()

    try:
        keys = extract_keys(
            pssh_b64=args.pssh,
            license_url=args.license,
            device_path=device_path,
            headers=headers_dict
        )

        print("\n" + "=" * 50)
        print(" Extracted Decryption Keys:")
        print("=" * 50)
        for k in keys:
            print(f"  --key {k}")
        print("=" * 50)

    except Exception as err:
        print(f"\n[!] Error extracting keys: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
