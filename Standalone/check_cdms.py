#!/usr/bin/env python3
"""
Widevine CDM Revocation & Status Checker
Checks Widevine .wvd device files to determine if they are WORKING or REVOKED/DEAD,
either against test servers or a specific OTT license server.
"""

import sys
import argparse
import requests
from pathlib import Path
from pywidevine import Cdm, Device
from pywidevine.pssh import PSSH

# Default directory containing .wvd devices
DEFAULT_DEVICES_DIR = Path(r"D:\temp\vinetrimmer\devices")

# Standard Widevine Test PSSH & Servers
TEST_PSSH = PSSH("AAAAW3Bzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAADsIARIQ62dYfePoSI6f7rAawzdF6hoNd2lkZXZpbmVfdGVzdCIIMjAyMzA4MTFSEDI0NzgwYWJjZGVmMDEyMzQ=")

TEST_SERVERS = [
    "https://widevine-dash.ezdrm.com/proxy?pContinuity=true",
    "https://proxy.uat.widevine.com/proxy"
]

def test_cdm(wvd_path: Path, license_url: str = None, pssh_str: str = None, headers: dict = None) -> dict:
    """Loads a .wvd CDM file and tests whether it is working or revoked."""
    res = {
        "filename": wvd_path.name,
        "security_level": "Unknown",
        "system_id": "N/A",
        "status": "UNKNOWN",
        "details": ""
    }

    if not wvd_path.exists():
        res["status"] = "ERROR"
        res["details"] = "File not found"
        return res

    try:
        device = Device.load(wvd_path)
        
        # Read security level and system ID
        sec_lvl = getattr(device, "security_level", None)
        if sec_lvl is not None:
            res["security_level"] = f"L{sec_lvl}"
        else:
            res["security_level"] = "L1" if "_l1" in wvd_path.name.lower() else "L3"
            
        res["system_id"] = str(getattr(device, "system_id", "N/A"))

        cdm = Cdm.from_device(device)
        session_id = cdm.open()

        try:
            pssh_obj = PSSH(pssh_str) if pssh_str else TEST_PSSH
            challenge = cdm.get_license_challenge(session_id, pssh_obj)

            req_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            if headers:
                req_headers.update(headers)

            target_url = license_url if license_url else TEST_SERVERS[0]
            resp = requests.post(target_url, data=challenge, headers=req_headers, timeout=12)

            if resp.status_code == 200:
                try:
                    cdm.parse_license(session_id, resp.content)
                    keys = cdm.get_keys(session_id)
                    op_keys = [k for k in keys if k.type in ("STREAMING", "OPERATIONAL")]
                    res["status"] = "WORKING"
                    res["details"] = f"License valid ({len(op_keys)} keys extracted)"
                except Exception as parse_err:
                    res["status"] = "WORKING"
                    res["details"] = f"Accepted by server (HTTP 200)"
            elif resp.status_code in (400, 401, 403):
                # Check response text for specific revocation keywords
                err_text = resp.text.lower()
                if any(kw in err_text for kw in ["revoked", "blacklist", "invalid device", "certificate"]):
                    res["status"] = "REVOKED"
                    res["details"] = f"Explicitly Revoked by Server (HTTP {resp.status_code})"
                else:
                    res["status"] = "REVOKED / REJECTED"
                    res["details"] = f"Rejected by License Server (HTTP {resp.status_code})"
            else:
                res["status"] = "FAILED"
                res["details"] = f"Server returned HTTP {resp.status_code}"

        finally:
            cdm.close(session_id)

    except Exception as err:
        res["status"] = "ERROR"
        res["details"] = str(err)

    return res

def main():
    parser = argparse.ArgumentParser(description="Widevine CDM Status & Revocation Checker")
    parser.add_argument("--dir", default=str(DEFAULT_DEVICES_DIR), help="Directory containing .wvd files")
    parser.add_argument("--file", help="Path to a single .wvd device file")
    parser.add_argument("--license", help="Target OTT License Server URL (optional)")
    parser.add_argument("--pssh", help="Target OTT PSSH (optional)")
    parser.add_argument("-H", "--header", action="append", help="HTTP headers 'Name: Value'")

    args = parser.parse_args()

    headers_dict = {}
    if args.header:
        for h in args.header:
            if ":" in h:
                k, v = h.split(":", 1)
                headers_dict[k.strip()] = v.strip()

    devices = []
    if args.file:
        f = Path(args.file)
        if f.exists():
            devices.append(f)
        else:
            print(f"[!] File not found: {f}")
            sys.exit(1)
    else:
        d = Path(args.dir)
        if not d.exists():
            print(f"[!] Directory not found: {d}")
            sys.exit(1)
        devices = sorted([
            f for f in d.rglob("*.wvd")
            if "google_android_sdk_built_for_x86_4464_l3" not in str(f)
        ])

    if not devices:
        print("[!] No .wvd CDM files found.")
        sys.exit(1)

    print("=" * 90)
    print(f" Widevine CDM Revocation & Working Status Checker ({len(devices)} CDMs)")
    if args.license:
        print(f" Target OTT Server: {args.license}")
    else:
        print(f" Target Mode: Global Widevine License Revocation Check")
    print("=" * 90)
    print(f"{'CDM Filename':<48} | {'Level':<5} | {'Status':<18} | {'Details'}")
    print("-" * 90)

    working = 0
    revoked = 0

    for wvd in devices:
        res = test_cdm(wvd, license_url=args.license, pssh_str=args.pssh, headers=headers_dict)
        st = res["status"]
        if "WORKING" in st:
            working += 1
        else:
            revoked += 1
            
        fname = res["filename"]
        if len(fname) > 48:
            fname = fname[:45] + "..."
        print(f"{fname:<48} | {res['security_level']:<5} | {st:<18} | {res['details']}", flush=True)

    print("=" * 90)
    print(f" SUMMARY: Total: {len(devices)} | Working: {working} | Revoked/Rejected: {revoked}")
    print("=" * 90)

if __name__ == "__main__":
    main()
