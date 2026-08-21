"""
Smoke tests for folkfriend-app-data deployment.

Checks:
  - Live endpoints: datasets.json, every per-dataset file, and the legacy
    bundle reachable with CORS headers
  - Local data integrity: per-dataset counts, cross-dataset ID disjointness,
    schema fields, and that norbeck is absent from the legacy bundle
  - App reachable at folkfriend-petrush-fork.web.app

Run from any directory:
  python3 test/smoke_test.py
"""

import json
import sys
import urllib.request
import urllib.error
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
    r = urllib.request.urlopen("https://folkfriend-data.web.app/datasets.json", timeout=15)
    manifest = json.loads(r.read())
    cors = r.getheader("Access-Control-Allow-Origin", "")
    check("datasets.json reachable", True)
    check("datasets.json CORS header present", cors == "*", f"got {cors!r}")
    check("datasets.json has manifestVersion", "manifestVersion" in manifest, str(manifest)[:120])
    entries = manifest.get("datasets", [])
    check("datasets.json lists at least 2 datasets", len(entries) >= 2, f"got {len(entries)}")
except Exception as e:
    check("datasets.json reachable", False, str(e))
    entries = []

# Every file the manifest names must actually be served, at the size it claims.
# The app uses `size` for its download progress bar in preference to
# Content-Length (which is the gzipped length), so a stale size is visible.
for entry in entries:
    name = entry.get("filename", "?")
    try:
        req = urllib.request.Request(
            f"https://folkfriend-data.web.app/{name}", method="HEAD")
        r = urllib.request.urlopen(req, timeout=15)
        cors = r.getheader("Access-Control-Allow-Origin", "")
        ct = r.getheader("Content-Type", "")
        check(f"{name} HEAD OK", True)
        check(f"{name} CORS header present", cors == "*", f"got {cors!r}")
        check(f"{name} Content-Type is JSON", "json" in ct, f"got {ct!r}")
    except Exception as e:
        check(f"{name} HEAD OK", False, str(e))

# The legacy bundle must stay published: installed PWAs that predate dataset
# selection fetch it by name and have no other source of tunes.
try:
    req = urllib.request.Request(
        "https://folkfriend-data.web.app/folkfriend-non-user-data.json",
        method="HEAD"
    )
    r = urllib.request.urlopen(req, timeout=15)
    size = int(r.getheader("Content-Length", "0"))
    check("legacy bundle still served", True)
    check("legacy bundle size > 30 MB", size > 30_000_000, f"got {size:,}")
except Exception as e:
    check("legacy bundle still served", False, str(e))

try:
    r = urllib.request.urlopen("https://folkfriend-petrush-fork.web.app/", timeout=15)
    html = r.read(1000).decode(errors="replace")
    check("app reachable at folkfriend-petrush-fork.web.app", "FolkFriend" in html)
except Exception as e:
    check("app reachable", False, str(e))

# ---------------------------------------------------------------------------
section("2. Local data integrity checks")
# ---------------------------------------------------------------------------

# Datasets are validated as their own files. Source identity used to be
# inferred from numeric ID range ("tune_id >= 1_000_000 means folkwiki"), which
# cannot survive a third source: folkwiki's range runs to ~1.68e9, so any new
# base either collides or turns the test into a chain of magic numbers.
script_dir = os.path.dirname(os.path.abspath(__file__))
public_dir = os.path.join(script_dir, "..", "public")

EXPECTED = {
    "thesession": {"min_settings": 50_000, "min_aliases": 20_000,
                   "needs_source_url": False, "origin_empty": True},
    "folkwiki":   {"min_settings": 5_000,  "min_aliases": 5_000,
                   "needs_source_url": True,  "origin_empty": False},
}

# Built, but never served — Norbeck's terms forbid making the ABC files
# available for download on a web page. It is imported by hand in the app.
UNPUBLISHED = ("norbeck",)

REQUIRED_FIELDS = {"tune_id", "meter", "mode", "abc", "dance", "contour", "origin"}
MODE_SUFFIXES = ("major", "minor", "dorian", "mixolydian", "lydian",
                 "phrygian", "locrian")

loaded = {}
for ds_id, spec in EXPECTED.items():
    path = os.path.join(public_dir, f"{ds_id}.json")
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        check(f"{ds_id}.json parseable", True)
    except Exception as e:
        check(f"{ds_id}.json parseable", False, str(e))
        continue

    loaded[ds_id] = payload
    settings = payload.get("settings", {})
    aliases = payload.get("aliases", {})

    check(f"{ds_id}: settings >= {spec['min_settings']:,}",
          len(settings) >= spec["min_settings"], f"got {len(settings):,}")
    check(f"{ds_id}: aliases >= {spec['min_aliases']:,}",
          len(aliases) >= spec["min_aliases"], f"got {len(aliases):,}")
    print(f"       {ds_id}: {len(settings):,} settings, {len(aliases):,} tunes")

    sample = [settings[k] for k in list(settings)[:300]]
    missing = {f for s in sample for f in REQUIRED_FIELDS if f not in s}
    check(f"{ds_id}: settings have all required fields", not missing,
          f"missing: {missing}")

    with_contour = sum(1 for s in sample if s.get("contour"))
    check(f"{ds_id}: >95% of settings have a contour",
          with_contour >= len(sample) * 0.95,
          f"{with_contour}/{len(sample)}")

    # The app derives thesession.org URLs from the tune ID, but cannot derive a
    # folkwiki or norbeck one — a missing source_url there is a dead link.
    if spec["needs_source_url"]:
        with_url = sum(1 for s in sample if s.get("source_url"))
        check(f"{ds_id}: every setting has a source_url",
              with_url == len(sample), f"{with_url}/{len(sample)}")

    if spec["origin_empty"]:
        nonempty = sum(1 for s in sample if s.get("origin", "") != "")
        check(f"{ds_id}: origin is empty", nonempty == 0,
              f"{nonempty} have a non-empty origin")

    modes = {s.get("mode", "") for s in sample}
    raw = {m for m in modes if not any(m.endswith(x) for x in MODE_SUFFIXES)}
    check(f"{ds_id}: modes normalised (no raw K: values)", not raw,
          f"un-normalised: {raw}")

    # ABC escapes must be decoded, or accented titles are stored as backslash
    # noise and cannot be found by searching for the name a user would type.
    escaped = [t for ts in list(aliases.values())[:2000] for t in ts
               if "\\" in t or "{" in t]
    check(f"{ds_id}: aliases have no undecoded ABC escapes", not escaped,
          f"e.g. {escaped[:3]}")

if len(loaded) > 1:
    setting_owner, tune_owner, collisions = {}, {}, []
    for ds_id, payload in loaded.items():
        for sid, setting in payload["settings"].items():
            if setting_owner.setdefault(sid, ds_id) != ds_id:
                collisions.append(f"setting {sid}")
            tid = setting.get("tune_id")
            if tune_owner.setdefault(tid, ds_id) != ds_id:
                collisions.append(f"tune {tid}")
        for tid in payload["aliases"]:
            if tune_owner.setdefault(tid, ds_id) != ds_id:
                collisions.append(f"tune {tid}")
    check("No ID collisions across datasets", not collisions,
          f"{len(collisions)} collisions, e.g. {collisions[:3]}")
    print(f"       {len(setting_owner):,} setting IDs, {len(tune_owner):,} tune IDs")

# Swedish dance names must survive as Swedish, not be translated or mangled.
if "folkwiki" in loaded:
    dances = {s["dance"] for s in list(loaded["folkwiki"]["settings"].values())[:500]}
    check("folkwiki has Swedish dance types (polska/vals/schottis/…)",
          any(d in dances for d in ("polska", "vals", "schottis", "hambo", "gånglåt")),
          f"found: {sorted(dances)[:10]}")

# An unpublished dataset must not be sitting in the deploy directory, and must
# not be reachable on the live site. This is the check that actually enforces
# the licensing position; everything else is bookkeeping.
for ds_id in UNPUBLISHED:
    local = os.path.join(public_dir, f"{ds_id}.json")
    check(f"{ds_id}.json is NOT in public/", not os.path.exists(local),
          f"{local} exists and would be deployed")
    try:
        req = urllib.request.Request(
            f"https://folkfriend-data.web.app/{ds_id}.json", method="HEAD")
        urllib.request.urlopen(req, timeout=15)
        check(f"{ds_id}.json is NOT served", False, "it returned 200")
    except urllib.error.HTTPError as e:
        check(f"{ds_id}.json is NOT served", e.code == 404, f"HTTP {e.code}")
    except Exception as e:
        check(f"{ds_id}.json is NOT served", False, str(e))
    check(f"{ds_id} is NOT in datasets.json",
          all(e.get("id") != ds_id for e in entries))


# The legacy bundle is still fetched by installed apps that predate dataset
# selection. It must exist, and must NOT contain norbeck — those clients cannot
# opt a dataset out, and the collection carries redistribution terms.
legacy_path = os.path.join(public_dir, "folkfriend-non-user-data.json")
try:
    with open(legacy_path) as f:
        legacy = json.load(f)
    check("legacy bundle present and parseable", True)
    check("legacy bundle has >= 55,000 settings",
          len(legacy.get("settings", {})) >= 55_000,
          f"got {len(legacy.get('settings', {})):,}")
    if "norbeck" in loaded:
        leaked = set(loaded["norbeck"]["settings"]) & set(legacy.get("settings", {}))
        check("norbeck is absent from the legacy bundle", not leaked,
              f"{len(leaked)} settings leaked")
except Exception as e:
    check("legacy bundle present and parseable", False, str(e))

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
