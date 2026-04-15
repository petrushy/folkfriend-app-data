"""
Smoke tests for folkfriend-app-data deployment.

Checks:
  - Live endpoints: nud-meta.json and data file HEAD reachable with CORS headers
  - Local data integrity: settings/aliases counts, ID ranges, schema fields
  - App reachable at folkfriend-petrush-fork.web.app

Run from any directory:
  python3 test/smoke_test.py
"""

import json
import sys
import urllib.request
import os

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

errors = []

def check(label, condition, detail=""):
    if condition:
        print(f"  {PASS}  {label}")
    else:
        msg = f"{label}" + (f": {detail}" if detail else "")
        print(f"  {FAIL}  {msg}")
        errors.append(msg)

def section(title):
    print(f"\n=== {title} ===")

# ---------------------------------------------------------------------------
section("1. Live endpoint checks")
# ---------------------------------------------------------------------------

try:
    r = urllib.request.urlopen("https://folkfriend-data.web.app/nud-meta.json", timeout=15)
    meta = json.loads(r.read())
    cors = r.getheader("Access-Control-Allow-Origin", "")
    check("nud-meta.json reachable", True)
    check("nud-meta CORS header present", cors == "*", f"got {cors!r}")
    check("nud-meta has 'v' and 'size'", "v" in meta and "size" in meta, str(meta))
    print(f"       v={meta.get('v')}, size={meta.get('size', 0):,} bytes")
except Exception as e:
    check("nud-meta.json reachable", False, str(e))
    meta = {}

try:
    req = urllib.request.Request(
        "https://folkfriend-data.web.app/folkfriend-non-user-data.json",
        method="HEAD"
    )
    r = urllib.request.urlopen(req, timeout=15)
    cors = r.getheader("Access-Control-Allow-Origin", "")
    ct = r.getheader("Content-Type", "")
    size = int(r.getheader("Content-Length", "0"))
    check("data file HEAD OK", True)
    check("data file CORS header present", cors == "*", f"got {cors!r}")
    check("data file Content-Type is JSON", "json" in ct, f"got {ct!r}")
    check("data file size > 30 MB", size > 30_000_000, f"got {size:,}")
except Exception as e:
    check("data file HEAD OK", False, str(e))

try:
    r = urllib.request.urlopen("https://folkfriend-petrush-fork.web.app/", timeout=15)
    html = r.read(1000).decode(errors="replace")
    check("app reachable at folkfriend-petrush-fork.web.app", "FolkFriend" in html)
except Exception as e:
    check("app reachable", False, str(e))

# ---------------------------------------------------------------------------
section("2. Local data integrity checks")
# ---------------------------------------------------------------------------

# Locate the public data file relative to this script
script_dir = os.path.dirname(os.path.abspath(__file__))
data_path = os.path.join(script_dir, "..", "public", "folkfriend-non-user-data.json")

try:
    with open(data_path) as f:
        data = json.load(f)
    check("data file parseable as JSON", True)
except Exception as e:
    check("data file parseable as JSON", False, str(e))
    print("Cannot continue integrity checks without local data file.")
    sys.exit(1 if errors else 0)

settings = data.get("settings", {})
aliases = data.get("aliases", {})

check("'settings' key present", "settings" in data)
check("'aliases' key present", "aliases" in data)

ts_ids = [k for k in settings if int(k) < 2_000_000]
fw_ids = [k for k in settings if int(k) >= 2_000_000]
ts_alias_ids = [k for k in aliases if int(k) < 1_000_000]
fw_alias_ids = [k for k in aliases if int(k) >= 1_000_000]

check(f"Total settings >= 60,000", len(settings) >= 60_000, f"got {len(settings):,}")
check(f"thesession settings > 50,000", len(ts_ids) > 50_000, f"got {len(ts_ids):,}")
check(f"folkwiki settings > 5,000", len(fw_ids) > 5_000, f"got {len(fw_ids):,}")
check(f"No setting ID collisions", len(set(ts_ids) & set(fw_ids)) == 0)
print(f"       thesession: {len(ts_ids):,}  folkwiki: {len(fw_ids):,}")

check(f"Total alias entries > 25,000", len(aliases) > 25_000, f"got {len(aliases):,}")
check(f"folkwiki aliases > 5,000", len(fw_alias_ids) > 5_000, f"got {len(fw_alias_ids):,}")

# Schema checks on samples
ts_sample = [settings[k] for k in list(ts_ids)[:200]]
fw_sample = [settings[k] for k in list(fw_ids)[:200]]

required_fields = {"tune_id", "meter", "mode", "abc", "dance", "contour", "origin"}
ts_missing = [f for s in ts_sample for f in required_fields if f not in s]
fw_missing = [f for s in fw_sample for f in required_fields if f not in s]
check("thesession settings have all required fields", len(ts_missing) == 0,
      f"missing: {set(ts_missing)}")
check("folkwiki settings have all required fields", len(fw_missing) == 0,
      f"missing: {set(fw_missing)}")

ts_origin_nonempty = sum(1 for s in ts_sample if s.get("origin", "") != "")
check("thesession settings have empty origin", ts_origin_nonempty == 0,
      f"{ts_origin_nonempty} have non-empty origin")

fw_with_origin = sum(1 for s in fw_sample if s.get("origin", ""))
fw_with_contour = sum(1 for s in fw_sample if s.get("contour", ""))
check("folkwiki settings have origin (>50% non-empty)", fw_with_origin > 100,
      f"{fw_with_origin}/200 have non-empty origin")
check("folkwiki settings have contour (>95%)", fw_with_contour >= 190,
      f"{fw_with_contour}/200 have contour")

# Dance type check — folkwiki should have Swedish names
fw_dances = {settings[k]["dance"] for k in list(fw_ids)[:500]}
has_svenska = any(d in fw_dances for d in ("polska", "vals", "schottis", "hambo", "gånglåt"))
check("folkwiki has Swedish dance types (polska/vals/hambo/…)", has_svenska,
      f"found: {sorted(fw_dances)[:10]}")

# Mode normalisation check
fw_modes = {settings[k]["mode"] for k in list(fw_ids)[:500]}
raw_abc_modes = {m for m in fw_modes if not any(
    m.endswith(s) for s in ("major", "minor", "dorian", "mixolydian", "lydian", "phrygian", "locrian")
)}
check("folkwiki modes are normalised (no raw K: values)", len(raw_abc_modes) == 0,
      f"un-normalised: {raw_abc_modes}")

# ---------------------------------------------------------------------------
print()
if errors:
    print(f"\033[31m{len(errors)} check(s) FAILED:\033[0m")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    total = 0  # count from lines above — just report success
    print("\033[32mAll checks passed.\033[0m")
    sys.exit(0)
