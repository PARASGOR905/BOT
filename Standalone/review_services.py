import os
import sys
import importlib
import traceback
from pathlib import Path

# Add d:\temp to sys.path
sys.path.insert(0, r"D:\temp")

services_dir = Path(r"D:\temp\vinetrimmer\services")
service_files = sorted([f for f in services_dir.glob("*.py") if f.name not in ["__init__.py", "BaseService.py"]])

print(f"[*] Found {len(service_files)} service modules to review.\n")

success_count = 0
failed_services = []
widevine_services = []

for s_file in service_files:
    mod_name = f"vinetrimmer.services.{s_file.stem}"
    try:
        mod = importlib.import_module(mod_name)
        # Check for service class
        svc_classes = [obj for name, obj in mod.__dict__.items() if isinstance(obj, type) and getattr(obj, '__module__', '') == mod_name]
        
        # Check if service imports or uses widevine / cdm
        has_wv = False
        with open(s_file, 'r', encoding='utf-8', errors='ignore') as f:
            code = f.read()
            if 'widevine' in code.lower() or 'cdm' in code.lower() or 'pssh' in code.lower():
                has_wv = True
                widevine_services.append(s_file.stem)
                
        print(f"  [+] {s_file.stem:<20} | Loaded successfully | Service classes: {[c.__name__ for c in svc_classes]} | Uses Widevine: {has_wv}")
        success_count += 1
    except Exception as e:
        print(f"  [!] {s_file.stem:<20} | FAILED TO LOAD: {e}")
        traceback.print_exc()
        failed_services.append((s_file.stem, str(e)))

print("\n" + "="*60)
print(f" Review Summary: {success_count}/{len(service_files)} services loaded without errors.")
print(f" Widevine-enabled services: {len(widevine_services)}")
if failed_services:
    print(f" Failed services ({len(failed_services)}):")
    for name, err in failed_services:
        print(f"   - {name}: {err}")
print("="*60)
