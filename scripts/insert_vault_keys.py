import os
import re
import sys
from typing import List, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vinetrimmer.config import config, Config
from vinetrimmer.objects.vaults import Vaults, InsertResult

SERVICE_ALIASES = {
    "nf": "netflix",
    "netflix": "netflix",
    "amzn": "amazon",
    "amazon": "amazon",
    "prime": "amazon",
    "dsnp": "disneyplus",
    "disney": "disneyplus",
    "disneyplus": "disneyplus",
    "hotstar": "jiohotstar",
    "jhs": "jiohotstar",
    "jiohotstar": "jiohotstar",
    "max": "max",
    "hbomax": "max",
    "hmax": "max",
    "atvp": "appletv",
    "appletv": "appletv",
    "appletvplus": "appletv",
    "zee5": "zee5",
    "z5": "zee5",
    "cr": "crunchyroll",
    "crunchyroll": "crunchyroll",
    "snxt": "sunnxt",
    "sunnxt": "sunnxt",
    "stg": "stagein",
    "stage": "stagein",
    "stagein": "stagein",
    "cp": "chaupal",
    "chaupal": "chaupal",
    "pm": "planetmarathi",
    "planetmarathi": "planetmarathi",
    "shm": "shemaroome",
    "shemaroome": "shemaroome",
    "ko": "kableone",
    "kableone": "kableone",
    "wav": "waves",
    "waves": "waves",
    "mx": "mxplayer",
    "mxplayer": "mxplayer",
    "sonyliv": "sonyliv",
    "sl": "sonyliv",
    "hulu": "hulu",
    "peacock": "peacock",
    "stan": "stan",
    "all4": "all4",
    "itv": "itv",
    "yt": "youtube",
    "youtube": "youtube",
}

def parse_input_text(text: str) -> Tuple[str, str, List[Tuple[str, str]]]:
    service = ""
    title = ""
    
    service_match = re.search(r"\[([A-Za-z0-9_]+)\]", text)
    if service_match:
        tag = service_match.group(1).lower()
        if tag in SERVICE_ALIASES:
            service = SERVICE_ALIASES[tag]
            
    vid_match = re.search(r'video_id=([0-9a-fA-F_-]+)', text)
    if not vid_match:
        vid_match = re.search(r'video_id=([0-9a-zA-Z_-]+)', text)
    if vid_match:
        title = vid_match.group(1)
    else:
        title_match = re.search(r"video_id=[0-9a-zA-Z_-]+\s*\(([^)]+)\)", text)
        if title_match:
            title = title_match.group(1)

    keys = re.findall(r"([0-9a-fA-F]{32})[:=]([0-9a-fA-F]{32})", text)
    
    unique_keys = []
    seen = set()
    for kid, key in keys:
        kid_clean = kid.lower()
        key_clean = key.lower()
        if (kid_clean, key_clean) not in seen:
            seen.add((kid_clean, key_clean))
            unique_keys.append((kid_clean, key_clean))
            
    return service, title, unique_keys

def insert_keys(service: str, title: str, keys: List[Tuple[str, str]]):
    service = SERVICE_ALIASES.get(service.lower().strip(), service.lower().strip())
    if not service:
        print("---> [ERROR] Service name cannot be empty.")
        return

    if not keys:
        print("---> [WARNING]No valid 32-character hex KID:KEY pairs found.")
        return

    print(f"\n[INFO] Target Service : {service}")
    print(f"[INFO] Title / ID     : {title or '(None)'}")
    print(f"[INFO] Keys Found     : {len(keys)}")
    print("-" * 65)

    loaded_vaults = []
    for v in config.key_vaults:
        try:
            loaded_vaults.append(Config.load_vault(v))
        except Exception as e:
            print(f"[ERROR] Failed to load vault {v.get('name')}: {e}")

    try:
        vaults_obj = Vaults(loaded_vaults, service=service)
    except Exception as e:
        print(f"[ERROR] Failed to initialize Vaults: {e}")
        return

    for kid, key in keys:
        print(f"\nKey: {kid}:{key}")
        for v in vaults_obj:
            try:
                res = vaults_obj.insert_key(v, service, kid, key, title, commit=True)
                status_str = "SUCCESS" if res == InsertResult.SUCCESS else ("ALREADY_EXISTS" if res == InsertResult.ALREADY_EXISTS else "FAILED")
                print(f"  -> Vault [{v.name}] ({v.type.name}): {status_str}")
            except Exception as e:
                print(f"  -> Vault [{v.name}] Error: {e}")

    print("\n" + "=" * 65)
    print("Done caching keys to vault.")

def interactive_mode():
    print("=" * 65)
    print("   Vinetrimmer Direct Vault Key Inserter")
    print("=" * 65)
    print("Paste your keys or log output below.")
    print("Supports: raw KID:KEY, --key KID:KEY, or full Vinetrimmer logs.")
    print("(Press Enter twice when done pasting)\n")

    lines = []
    while True:
        try:
            line = input()
            if not line.strip() and lines:
                break
            if not line.strip() and not lines:
                continue
            lines.append(line)
        except EOFError:
            break

    raw_text = "\n".join(lines)
    if not raw_text.strip():
        print("No input provided. Exiting.")
        return

    detected_service, detected_title, keys = parse_input_text(raw_text)

    service_prompt = f"Enter Service [{detected_service}]: " if detected_service else "Enter Service (e.g. netflix, amazon, jiohotstar): "
    user_service = input(service_prompt).strip()
    service = user_service if user_service else detected_service

    title_prompt = f"Enter Title/ID [{detected_title}]: " if detected_title else "Enter Title/ID (optional, press Enter to skip): "
    user_title = input(title_prompt).strip()
    title = user_title if user_title else detected_title

    insert_keys(service, title, keys)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        args_text = " ".join(sys.argv[1:])
        det_svc, det_ttl, parsed_keys = parse_input_text(args_text)
        
        first_arg = sys.argv[1].lower()
        if first_arg in SERVICE_ALIASES:
            svc = first_arg
            remaining = " ".join(sys.argv[2:])
            _, ttl, parsed_keys = parse_input_text(remaining)
        elif det_svc:
            svc = det_svc
            ttl = det_ttl
        else:
            svc = "netflix"
            ttl = det_ttl
            
        insert_keys(svc, ttl, parsed_keys)
    else:
        interactive_mode()