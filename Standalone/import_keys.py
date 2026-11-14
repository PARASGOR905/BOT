import sys
import json
from pathlib import Path
from vinetrimmer.config import config
from vinetrimmer.objects.vaults import Vaults, Vault

def import_manifest_keys(json_file_path):
    p = Path(json_file_path)
    if not p.is_file():
        print(f"Error: File '{json_file_path}' not found.")
        return

    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)

    service = data.get("service") or "AMZN"
    titles = data.get("titles") or {}
    
    vault_objs = [Vault(type_=v["type"], **{k: val for k, val in v.items() if k != "type"}) for v in config.key_vaults]
    vaults = Vaults(vault_objs, service)

    inserted_count = 0
    for title_id, title_data in titles.items():
        tracks = title_data.get("tracks") or {}
        for track_id, track_info in tracks.items():
            keys_dict = track_info.get("keys") or {}
            for kid, key in keys_dict.items():
                for vault in vaults.vaults:
                    res = vaults.insert_key(vault, service.lower(), kid.lower(), key.lower(), title_id, commit=True)
                    print(f"[{vault.name}] {kid}:{key} -> {res.name}")
                    inserted_count += 1

    print(f"Done! Imported keys for {len(titles)} title(s).")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        import_manifest_keys(sys.argv[1])
    else:
        print("Usage: python import_keys.py <path_to_manifest.json>")
