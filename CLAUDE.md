# folkfriend-app-data — Claude Context

## What this repo is

This is the **data pipeline and hosting repo** for the FolkFriend app's tune index. It downloads raw tune data from [thesession.org's GitHub dataset](https://github.com/adactio/TheSession-data), processes it into a compact queryable format, and deploys the result to Firebase Hosting so the FolkFriend web app can fetch it.

The two output files served from `public/` are:

- `folkfriend-non-user-data.json` — the full tune index (~35 MB): a JSON object with two top-level keys:
  - `settings` — map of `setting_id → {tune_id, abc, meter, mode, dance, contour, origin}`
  - `aliases` — map of `tune_id → [list of name strings]` (primary name first)
  - Contains ~54k thesession settings + ~5.9k folkwiki settings = ~60k total
  - `origin` is empty string for thesession entries; populated (e.g. `"Dalarna"`) for folkwiki entries
- `nud-meta.json` — tiny metadata file: `{"v": <days since 2020-01-01>, "size": <bytes>}`

The `v` field is used by the FolkFriend app to detect stale cached data.

## Key concept: MIDI contour

Each tune setting's ABC notation is converted to a **contour string** — a sequence of ASCII letters representing relative MIDI pitch, quantised to quavers (eighth notes). This is the representation the Rust/WASM query engine actually searches against.

- Pitch range: MIDI 48 (C2) to MIDI 95 (B6), mapped to `abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ` (48 characters, `string.ascii_letters[:48]` from `ff_config.py`)
- Notes outside this range are octave-shifted into it before mapping
- All note durations are quantised to quavers; dotted/longer notes become repeated characters

## Repo structure

```text
build/
  build.sh                  # Main build script (bash)
  env.sh                    # Activates Python virtualenv and sets PYTHONPATH
  requirements.txt          # Python dependencies (pip)
  abc2midi                  # Required binary (NOT in git — must be placed here manually)
  data/                     # Generated at build time (NOT in git)
    tunes.json              # Downloaded from thesession.org GitHub
    aliases.json            # Downloaded from thesession.org GitHub
    midis/                  # Intermediate per-setting .midi files (cached between runs)
    folkwiki/               # Folkwiki data (cached between runs)
      manifest.json         # hexhash → {name, url} for all discovered tunes
      {hexhash}.abc         # Downloaded ABC files (~6100 files)
      midis/                # Intermediate MIDI files for folkwiki settings
    folkwiki-processed.json # Parsed folkwiki settings + aliases (intermediate)
    folkfriend-non-user-data.json  # Final merged output (thesession + folkwiki)
    nud-meta.json
    old_hash.txt / new_hash.txt  # Change detection
  src/
    download_thesession_data.py  # Fetches tunes.json and aliases.json
    build_non_user_data.py       # Main processing: ABC → MIDI → contour, alias cleanup; merges folkwiki
    midi.py                      # ABC→MIDI via abc2midi subprocess; MIDI→contour logic
    ff_config.py                 # MIDI pitch range and mapping constants
    download_folkwiki_data.py    # Discovers .abc URLs via Wayback CDX, downloads to data/folkwiki/
    build_folkwiki_data.py       # Parses folkwiki ABC files → contours → data/folkwiki-processed.json
public/
  folkfriend-non-user-data.json  # Deployed output (committed to git)
  nud-meta.json                  # Deployed output (committed to git)
  404.html                       # Firebase Hosting fallback
firebase.json                    # Firebase Hosting config (serves public/, adds CORS headers)
```

## How to run a build

**Prerequisites:**

1. Python virtualenv at `build/venv/` — create with `python3 -m venv build/venv` and install deps:

   ```sh
   cd build && . env.sh && pip install -r requirements.txt
   ```

2. `abc2midi` binary version **4.84 (January 20 2023)** placed at `build/abc2midi`. Get it from [abcmidi](https://github.com/sshlien/abcmidi). The build script checks the version and exits if it doesn't match. During development a symlink to the system abc2midi (5.02 via Homebrew) works fine for building and testing, but `build.sh` will reject it on the version check — run the Python scripts directly to bypass this.

3. Firebase CLI installed and authenticated: `npm install -g firebase-tools && firebase login`

**Running:**

```sh
cd build && bash build.sh
```

The script:

1. Verifies abc2midi version
2. Downloads `tunes.json` and `aliases.json` from thesession.org GitHub into `build/data/`
3. Compares SHA1 hash of data files against the previous run — exits early if unchanged
4. Runs `build_non_user_data.py` (multiprocessing, ~1 ABC→MIDI per setting, cached in `build/data/midis/`)
5. Moves output JSON files to `public/`
6. `git add`, `git commit` (with `nud-meta.json` content as the message), `git push`
7. `firebase deploy`

## Firebase Hosting

- Serves `public/` directory
- All `.json` files are served with `Access-Control-Allow-Origin: *` so the FolkFriend app can fetch them cross-origin from any domain
- The deployed URL is used by the FolkFriend app's service worker as a runtime-cached resource (StaleWhileRevalidate strategy)

## Notes

- The `build/data/midis/` directory caches intermediate MIDI files per `setting_id`. If a setting's MIDI already exists it is not regenerated — this makes incremental rebuilds fast.
- `build_non_user_data.py` uses `process_map` (tqdm + multiprocessing) for the ABC→contour step.
- The alias deduplication logic removes stop words, strips plurals, and removes subset aliases (e.g. "the cup" is dropped if "the cup of tea" is present).
- The `name` field from each tune is merged into `aliases` as the first entry, so `aliases[tune_id][0]` is always the canonical session name.

---

## Folkwiki integration — COMPLETE

Swedish folk music from folkwiki.se is merged into the same `folkfriend-non-user-data.json` output alongside thesession.org data. The merged index has **60,190 settings** (54,293 thesession + 5,897 folkwiki).

### What was done

1. **`build/src/download_folkwiki_data.py`** — NEW, complete and working.
   - `http://www.folkwiki.se/pub/cache/` directory listing is intercepted by WordPress and returns a "Coming Soon" page, but individual `.abc` file URLs still work (served by Apache).
   - Uses the **Wayback Machine CDX API** to discover all 6,797 known `.abc` URLs, then fetches each live from folkwiki.se.
   - Downloads to `build/data/folkwiki/{hexhash}.abc`, skipping already-cached files.
   - Saves a manifest at `build/data/folkwiki/manifest.json`.
   - Supports `--offline` flag to skip discovery/download and use cached files only.
   - **6,103 files successfully downloaded** (33 CDX entries returned 404 from live server).
   - **Encoding fix applied**: server returns `text/plain` without charset; requests defaults to ISO-8859-1 and mangles UTF-8 Swedish characters. Fix: use `r.content.decode('utf-8', errors='replace')` instead of `r.text`. All 6,103 already-downloaded files have been repaired in-place with the correct UTF-8 content.

2. **`build/src/build_folkwiki_data.py`** — NEW, complete and working.
   - Parses ABC files: handles multi-tune files (multiple `X:` blocks), extracts `T:` (aliases), `M:`, `L:`, `K:`, `R:` (dance type), `O:` (origin/region).
   - Normalises ABC `K:` values to thesession-style mode strings (`K:Am` → `Aminor`, `K:Ddor` → `Ddorian`, etc.). Special cases: `K:none`, `K:HP`, `K:free` all map to `Cmajor`.
   - Preserves the file's `L:` (note length) value and passes it to abc2midi — critical because folkwiki often uses `L:1/16` while thesession always uses `L:1/8`.
   - Dance types kept as original lowercase Swedish/Nordic names (`polska`, `vals`, `schottis`, `hambo`, `gånglåt`, etc.) — not translated to English.
   - IDs: folkwiki `tune_id` starts at **1,000,000**, `setting_id` starts at **2,000,000** — no collision with thesession's ~23k/~54k range.
   - Handles abc2midi failures gracefully: if MIDI file isn't produced, returns empty contour instead of crashing.
   - **Output: `build/data/folkwiki-processed.json`** with 5,897 settings (of 6,103 files; some files unparseable or have no note body). Only 9 empty contours.
   - MIDI cache at `build/data/folkwiki/midis/` (same caching pattern as thesession).

3. **`build/src/build_non_user_data.py`** — MODIFIED.
   - After building thesession data, loads `folkwiki-processed.json` and merges both `settings` and `aliases` dicts.
   - Adds `"origin": ""` to all thesession settings for schema uniformity.

4. **`build/build.sh`** — MODIFIED.
   - Calls `download_folkwiki_data.py` and `build_folkwiki_data.py` before `build_non_user_data.py`.

5. **`folkfriend/rust/src/index/schema.rs`** — MODIFIED.
   - Added `#[serde(default)] pub origin: String` to the `Setting` struct.

6. **`build/venv/`** — Python virtualenv created, all requirements installed.

7. **`build/abc2midi`** — Currently a symlink to `/opt/homebrew/bin/abc2midi` (version 5.02). Production builds require version 4.84 per `build.sh`'s version check.

### Running the full pipeline (folkwiki already built)

```sh
cd build
. env.sh   # activates venv at build/venv/

# folkwiki ABC files already cached at build/data/folkwiki/ (6103 files)
# Re-download only if you suspect new tunes have been added:
# python src/download_folkwiki_data.py .

# folkwiki contours already built at build/data/folkwiki-processed.json
# Re-run only if ABC files changed:
# python src/build_folkwiki_data.py .

# Download fresh thesession data and run the merge:
python src/download_thesession_data.py .
python src/build_non_user_data.py .
```

This produces `build/data/folkfriend-non-user-data.json` (~60k settings) ready to copy to `public/`.

### Schema changes summary

`folkfriend-non-user-data.json` — `settings` entries now have an additional field:

- `"origin": ""` — geographic region string (populated for folkwiki tunes e.g. `"Dalarna"`, empty string for thesession tunes)

The Rust `Setting` struct in `folkfriend/rust/src/index/schema.rs` has `#[serde(default)]` on `origin` so old cached JSON without this field still deserialises correctly.

### App-side changes (folkfriend repo) — DONE

- **Source link** — `app/src/views/Tune.vue` updated. Tunes with `tuneID < 1,000,000` link to thesession.org (existing behaviour). Tunes with `tuneID >= 1,000,000` (folkwiki) link to `http://www.folkwiki.se/Musik/{TuneName}`, constructed from the display name with spaces → underscores. Both the tune-level chip and the per-setting chip are handled.
- **No other app changes required** — folkwiki tunes appear in melody and name search automatically once the merged JSON is deployed.

### Remaining / optional

1. **Display `origin` field** — the region (e.g. "Dalarna") is in the index but not shown anywhere in the UI yet. Could appear in the Tune view header or as a chip alongside the dance type.
2. **Deploy** — once abc2midi 4.84 is in `build/abc2midi`, run `bash build.sh` to build, deploy to Firebase, and the live app picks it up on next refresh.
