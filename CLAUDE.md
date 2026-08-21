
# folkfriend-app-data — Claude Context

## What this repo is

This is the **data pipeline and hosting repo** for the FolkFriend app's tune
index. It ingests tunes from three sources, converts each setting's ABC
notation into a searchable melodic contour, and deploys the result to Firebase
Hosting (`folkfriend-data.web.app`) for the app to fetch.

### Published files (August 2026 — multi-dataset layout)

The index is served as **one file per source plus a manifest**, so the app can
let the user choose which datasets to download and search:

| file | contents |
|---|---|
| `datasets.json` | the manifest: each dataset's `id`, `filename`, `v`, `date`, `size`, `settings`, `tunes` |
| `thesession.json` | 54,293 settings / 23,017 tunes — 34.8 MB |
| `folkwiki.json` | 8,484 / 8,484 — 7.3 MB |
| `norbeck.json` | 3,472 / 3,472 — 3.1 MB |
| `folkfriend-non-user-data.json` | **legacy** merged bundle, thesession + folkwiki only |
| `nud-meta.json` | **legacy** metadata for the above |

Each dataset file is `{settings, aliases}`:

- `settings` — `setting_id → {tune_id, abc, meter, mode, dance, contour, origin, composer[, source_url][, zid]}`
- `aliases` — `tune_id → [name strings]`, primary name first, all lowercased

`norbeck.json` additionally carries top-level `copyright` and `release` fields.

**Why the legacy files still exist, and why norbeck is not in them.** Installed
PWAs that have never updated fetch `folkfriend-non-user-data.json` by name and
have no other source of tunes, so it must keep being published. But those
clients also cannot turn a dataset *off*, and Norbeck's collection carries
redistribution terms — so it is deliberately excluded. `validate_output.py` and
`smoke_test.py` both assert that it is absent, and `assemble_datasets.py` fails
the build if it leaks in.

`public/folkfriend-non-user-data.json` is **gitignored**: it is derived from
`thesession.json` + `folkwiki.json`, and committing it too would add ~42 MB of
redundancy to every data revision. It is still deployed — `firebase.json` has
its own ignore list and does not consult `.gitignore`.

### Per-dataset versions

`v` is whole days since 2020-01-01 at build time, as it always was, but each
dataset now carries **its own**. That is the point of the split: refreshing
folkwiki must not bump thesession's version and push a 35 MB download to every
user. `build.sh` preserves the published `v` for any dataset it did not rebuild.

Two builds of the same dataset on the same day produce the same `v` and clients
will not update. That has bitten this repo before — use
`assemble_datasets.py --set-version <id>=<v>` to force one out.

`nud-meta.json` is **generated from `datasets.json`** rather than independently,
because two manifests describing the same pipeline will drift and the failure
("updates stopped happening") is silent.

### ID ranges — read this before adding a source

Source identity used to be inferred from numeric ID range. That is now only a
legacy fallback, and the ranges are not what they look like:

| source | tune_id | setting_id |
|---|---|---|
| thesession | < 30,000 | < 60,000 |
| folkwiki | 1,000,000 – ~1,678,715,901 | 2,000,000 – ~1,679,715,901 |
| norbeck | 3,000,000,000 – ~7.3e9 | 8,000,000,000 – ~12.3e9 |

**Folkwiki is not a small block.** `stable_folkwiki_id` is
`base + int(hexhash,16)*100 + block` with a six-hex-digit hash, so it reaches
1.68e9 in *both* namespaces. A fourth source starting at, say, 3,000,000 would
collide immediately. All IDs stay below `2**53` so JS `parseInt` is exact, and
`u64` covers them on the Rust side.

IDs are hash-derived rather than enumerated so they stay stable when tunes are
inserted — user favourites reference them. `assemble_datasets.py`,
`validate_output.py`, `smoke_test.py` and the Rust test
`datasets_have_disjoint_ids` all check disjointness, because a collision raises
nothing: one setting silently shadows the other and the tune stops being
findable.

## Key concept: MIDI contour

Each tune setting's ABC notation is converted to a **contour string** — a sequence of ASCII letters representing relative MIDI pitch, quantised to quavers (eighth notes). This is the representation the Rust/WASM query engine actually searches against.

- Pitch range: MIDI 48 (C2) to MIDI 95 (B6), mapped to `abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ` (48 characters, `string.ascii_letters[:48]` from `ff_config.py`)
- Notes outside this range are octave-shifted into it before mapping
- All note durations are quantised to quavers; dotted/longer notes become repeated characters

## Repo structure

```text
build/
  build.sh                  # Incremental build: per-dataset change detection, then deploy
  regenerate_dataset.sh     # Full refresh from upstream, ignoring change detection
  env.sh                    # Activates the Python virtualenv
  requirements.txt
  abc2midi                  # Required binary (NOT in git — symlink it yourself)
  data/                     # Generated at build time (NOT in git)
    tunes.json / aliases.json         # thesession, from GitHub
    midis/                            # cached per-setting .midi, keyed on setting_id
    folkwiki/                         # manifest.json, {hexhash}.abc, midis/ (fw_ prefix)
    norbeck/                          # manifest.json, hnYYYYMM.zip, abc/, site_refs.json, midis/ (hn_ prefix)
    hashes/                           # per-dataset input hashes for change detection
    thesession.json / folkwiki.json / norbeck.json
    datasets.json / folkfriend-non-user-data.json / nud-meta.json
  src/
    abc_common.py                # SHARED: ABC parsing, escape decoding, alias cleaning, contour generation
    midi.py                      # abc2midi subprocess; MIDI→contour
    ff_config.py                 # MIDI pitch range and mapping constants
    download_thesession_data.py  # Fetches tunes.json / aliases.json
    build_thesession_data.py     # → data/thesession.json   (was build_non_user_data.py)
    download_folkwiki_data.py    # Wayback CDX discovery + live download
    discover_folkwiki_pageids.py # hexhash → wiki page id
    fill_missing_folkwiki.py     # Gap-fill for Latin-1 percent-encoded URLs
    build_folkwiki_data.py       # → data/folkwiki.json
    download_norbeck_data.py     # Resolves + downloads hnYYYYMM.zip, extracts it
    discover_norbeck_refs.py     # Scrapes valid display.asp (rhythm, ref) pairs
    build_norbeck_data.py        # → data/norbeck.json
    assemble_datasets.py         # → datasets.json + legacy bundle + nud-meta.json
    validate_output.py           # Per-dataset checks + cross-dataset ID disjointness
public/                          # Deployed verbatim; the merged bundle is gitignored
test/
  build_script_test.py       # build.sh ordering invariants
  folkwiki_pipeline_test.py  # folkwiki IDs + shared parser behaviour
  norbeck_pipeline_test.py   # abc_common (escapes, L:, ornaments) + norbeck IDs/URLs
  smoke_test.py              # Post-deploy live endpoints + local data integrity
firebase.json                # Hosting config (serves public/, CORS on **/*.json)
```

### `abc_common.py` — the shared module

Alias cleaning, ABC header parsing, mode normalisation, multi-tune splitting,
voice extraction, ornament stripping and contour generation were duplicated
between the thesession and folkwiki builders. A third source would have made
three copies, and the passing-note and grace-note bugs of April 2026 both had
to be fixed twice for exactly that reason. They now live in one module.

**The regression gate on that extraction was a full rebuild diffed against the
previous output: 0 contour changes across 62,777 settings** (three settings went
from empty to non-empty, which is abc2midi timing out on the earlier run). If
you touch `abc_common.py`, re-run that diff.

Two behaviours in it are subtle and load-bearing:

- **`decode_abc_escapes`.** ABC files are historically 7-bit ASCII and spell
  accented letters as TeX escapes. Norbeck's collection is pure ASCII and uses
  them 3,500+ times — both the accent form (`sl\"angpolska`) and the brace
  ligature form (`G{\aa}rdebyl{\aa}ten`, 796 occurrences). Without decoding,
  every Swedish and Irish accented title is stored as backslash noise and
  cannot be found by searching for the name a user would type. Handling `\aa`
  but not the braces is *worse* than doing nothing, because `gärdebyl{å}ten`
  looks decoded. Applied to header-derived text only — the note body goes to
  abc2midi and ABCJS, which understand escapes themselves.
- **`note_len=None` omits the `L:` line** from the synthetic header rather than
  defaulting to `1/8`, so abc2midi applies the ABC standard rule (1/16 below a
  meter of 0.75, else 1/8) exactly as it would for the original file. 45% of
  Norbeck tunes have no `L:`, and 290 of them are in 2/4 or 3/8 where the
  default is *not* 1/8. Guessing halves every duration and silently destroys
  matching.

## How to run a build

**Prerequisites:**

1. Python virtualenv at `build/venv/` — create with `python3 -m venv build/venv` and install deps:

   ```sh
   cd build && . env.sh && pip install -r requirements.txt
   ```

2. `abc2midi` binary version **5.02 (February 16 2025)** placed at `build/abc2midi`. Get it from [abcmidi](https://github.com/sshlien/abcmidi). The build script checks the version and exits if it doesn't match. A symlink to the Homebrew binary works: `ln -sf /opt/homebrew/bin/abc2midi build/abc2midi`.

3. Firebase CLI installed and authenticated: `npm install -g firebase-tools && firebase login`

**Running:**

```sh
cd build && bash build.sh
```

The script (`set -e` — exits immediately on any failure):

1. Verifies the abc2midi version string
2. Fetches all three sources (folkwiki `--offline`, i.e. cached ABC only)
3. **Per-dataset change detection** — hashes each dataset's own inputs against
   `data/hashes/<id>.sha1`, and exits early only if *nothing* changed. This
   used to be a single hash over thesession's two files, which meant a folkwiki
   or norbeck update could never trigger a build.
4. Rebuilds **only the datasets whose inputs changed**, so an unchanged 35 MB
   file does not get a new version and force every client to re-download it
5. `assemble_datasets.py` — writes `datasets.json`, the legacy merged bundle
   and `nud-meta.json`, preserving the published `v` of any dataset it did not
   rebuild
6. `validate_output.py` — per-dataset thresholds, cross-dataset ID
   disjointness, manifest/file agreement, and that norbeck is absent from the
   legacy bundle
7. Moves the output files to `public/`
8. `git add`, `git commit` (with `nud-meta.json` content as the message), `git push`
9. `firebase deploy`
10. **Only then records the input hashes.** They used to be written at detection
    time, so any later failure — abc2midi dying, a failed validation, a Firebase
    blip — left the inputs marked as processed. The next run would see
    "unchanged", exit early, and that dataset would never be retried until its
    upstream source happened to change again. `set -e` made it silent.

`regenerate_dataset.sh` is the full refresh: it ignores change detection,
downloads folkwiki live and refreshes both discovery artifacts. Use
`--skip-pageids`, `--skip-fill-missing` and `--skip-norbeck-refs` to skip the
slow crawls.

**Running the tests:**

```sh
python3 -m unittest discover -s test -p '*_pipeline_test.py'
python3 -m unittest discover -s test -p 'build_script_test.py'
python3 test/smoke_test.py          # needs a deploy for its live checks
```

## Firebase Hosting

- Serves `public/` directory
- All `.json` files are served with `Access-Control-Allow-Origin: *` so the FolkFriend app can fetch them cross-origin from any domain
- The deployed URL is used by the FolkFriend app's service worker as a runtime-cached resource (StaleWhileRevalidate strategy)

## Notes

- MIDI files are cached per setting and reused, which is what makes rebuilds
  fast. Each source uses its own directory or filename prefix (`midis/`,
  `folkwiki/midis/fw_`, `norbeck/midis/hn_`) — sharing one would alias the
  caches across sources.
- All three builders use `process_map` (tqdm + multiprocessing) for the
  ABC→contour step.
- Alias deduplication removes stop words, strips plurals, and removes subset
  aliases ("the cup" is dropped if "the cup of tea" is present).
- `aliases[tune_id][0]` is always the canonical name.
- abc2midi failures are non-fatal and yield an empty contour; the validator
  fails the build if more than 5% of a dataset is empty. Real rates are 0.04%
  (thesession), 0.45% (folkwiki), 0.35% (norbeck).

---

## Folkwiki integration — COMPLETE

Swedish folk music from folkwiki.se, now published as its own `folkwiki.json`
(and merged into the legacy bundle alongside thesession).

> Counts below are from the original April 2026 integration and are **stale** —
> the gap-fill described further down raised folkwiki to **8,484 settings**.
> Current figures are in "Published files" at the top.

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
   - IDs: folkwiki `tune_id` is based at **1,000,000** and `setting_id` at
     **2,000,000**. Note these are *bases*, not blocks — the hash term carries
     them up to ~1.68e9. See "ID ranges" at the top.
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

7. **`build/abc2midi`** — Symlink to `/opt/homebrew/bin/abc2midi` (version 5.02). `build.sh` verifies the version string matches before proceeding.

### Schema changes summary

`folkfriend-non-user-data.json` — `settings` entries now have an additional field:

- `"origin": ""` — geographic region string (populated for folkwiki tunes e.g. `"Dalarna"`, empty string for thesession tunes)

The Rust `Setting` struct in `folkfriend/rust/src/index/schema.rs` has `#[serde(default)]` on `origin` so old cached JSON without this field still deserialises correctly.

### App-side changes (folkfriend repo) — DONE

- **Source link** — `app/src/views/Tune.vue` updated. Tunes with `tuneID < 1,000,000` link to thesession.org (existing behaviour). Tunes with `tuneID >= 1,000,000` (folkwiki) link to `http://www.folkwiki.se/Musik/{TuneName}`, constructed from the display name with spaces → underscores. Both the tune-level chip and the per-setting chip are handled.
- **No other app changes required** — folkwiki tunes appear in melody and name search automatically once the merged JSON is deployed.

### Gap-fill for Latin-1 encoded URLs — `fill_missing_folkwiki.py`

The Wayback Machine CDX API misses ABC files whose URLs use Latin-1 percent-encoding (e.g. `%F6` for ö, `%E4` for ä). As a result, tunes with Swedish characters in their names are absent from `manifest.json` even though their wiki pages exist.

**`build/src/fill_missing_folkwiki.py`** bridges this gap:

- For each entry in `hexhash_to_pageid.json` that is not already in `manifest.json`, fetches `http://www.folkwiki.se/Musik/{pageID}`, extracts the `.abc` href from the page HTML (which contains the correctly Latin-1-encoded URL), downloads the file, and updates `manifest.json`.
- Uses 5 parallel workers, 3 retries per page, incremental manifest saves every 100 pages.
- Result (April 2026): 7,885 ABC files total, adding 1,782 previously missing tunes (e.g. "Baggbölebäckens klagan").

**When to re-run:** When new wiki pages are added to folkwiki.se that contain Swedish characters in their tune names. Check by comparing `len(manifest)` against `len(hexhash_to_pageid)` — a growing gap indicates new Latin-1-named tunes. This script is **not** part of `build.sh` (it is slow and rarely needed); run it manually:

```sh
cd build && . env.sh && python src/fill_missing_folkwiki.py .
```

## Deployment — DONE (April 2026)

### Firebase Hosting — multi-site setup

Two separate sites within the `folkfriend-petrush-fork` Firebase project:

- **`folkfriend-data.web.app`** — hosts the tune index JSON (~40 MB, 60k settings).
  - `folkfriend-app-data/.firebaserc` → project `folkfriend-petrush-fork`
  - `folkfriend-app-data/firebase.json` → `"site": "folkfriend-data"`
  - Deploy: `cd folkfriend-app-data && firebase deploy --only hosting`

- **`folkfriend-petrush-fork.web.app`** — hosts the Vue app.
  - `folkfriend/app/.firebaserc` → project `folkfriend-petrush-fork` (default)
  - Deploy: `cd folkfriend/app && npm run build && firebase deploy --only hosting`

### Tune index versioning

Superseded by per-dataset versions — see "Per-dataset versions" at the top.
The app still re-fetches only when `remoteVersion > localVersion`; it just
compares per dataset now.

### Smoke tests

`test/smoke_test.py` — run after any data deploy to verify endpoints and data integrity:
```sh
python3 test/smoke_test.py
```

---

## Folkwiki source URL — DONE (April 2026)

### How it works

Folkwiki wiki pages use numeric IDs in URLs: `http://www.folkwiki.se/Musik/4237`. The hex hash in the ABC filename (`165c37`) is unrelated to the page ID.

**`build/src/discover_folkwiki_pageids.py`** crawled `/Musik/1`-`/Musik/6500` with 20 parallel workers and built `build/data/folkwiki/hexhash_to_pageid.json` - a `{hexhash: pageID}` reverse mapping. 7,154 mappings, covering 72% of the 6,103 manifest entries. The remaining 28% have no corresponding wiki page and fall back to the `pub/cache` ABC file URL.

**`build/src/build_folkwiki_data.py`** loads `hexhash_to_pageid.json` at build time and sets `source_url` per setting:

- If hexhash is in mapping: `http://www.folkwiki.se/Musik/{pageID}`
- Otherwise: `http://www.folkwiki.se/pub/cache/{name}_{hexhash}.abc`

**`app/src/js/source.mjs`** (new helper module) handles URL construction for both thesession and folkwiki tunes. `Tune.vue` and `Favourites.vue` use it. `worker.js` extracts `source_url` as a sideband field (same pattern as `abc`) and re-attaches it in `settingsFromTuneID`.

---

## Norbeck integration — DONE (August 2026)

[Henrik Norbeck's ABC collection](https://www.norbeck.nu/abc/) — 3,472 settings
of mostly Irish and Swedish traditional music — published as `norbeck.json`.

### ⚠️ NOT PUBLISHED — imported by hand

Norbeck's terms forbid making the ABC files available for download on a web
page, so **this dataset is built but never served**. It is absent from
`datasets.json` and from `public/`; `assemble_datasets.py` marks it
`published: False` and writes `PUBLISHED_FILES.txt`, which `build.sh` obeys —
plus an explicit guard that fails the build if `public/norbeck.json` ever
appears. `validate_output.py` checks both.

The built `build/data/norbeck.json` is stamped self-describing (`id`, `label`,
`v`, `date`) and is loaded into the app by hand: **Settings → "Add a database"**,
from a file or from a URL you supply. FolkFriend hosts nothing and is not in the
distribution chain.

**Both deploy paths are guarded, not just `build.sh`.** `regenerate_dataset.sh`
also copies into `public/`, and it was missed the first time — it published
Norbeck for a commit. Both now read `PUBLISHED_FILES.txt` and both fail if
`public/norbeck.json` exists, and `build_script_test.py` asserts that of every
script in `SCRIPTS`, so a third entry point cannot quietly skip it.

If you host it somewhere yourself to sync your own devices, note that is still
you making it available for download — obscurity, not permission — and that the
app's URL import needs the host to send `Access-Control-Allow-Origin`.

### ⚠️ Copyright: the underlying terms

His stated terms ([AboutTunes.asp](https://www.norbeck.nu/abc/AboutTunes.asp)):

- *"May not be used for commercial purposes (such as printing a tune book to sell)"*
- *"The ABC files (or parts of them) may not be made available on a web page for download without permission from me"*
- *"This copyright notice must be kept"*, and individual tunes must keep the `Z:id:hn-` line
- Contact: `henrik@norbeck.nu`

**Publishing `norbeck.json` is a redistribution, and permission has not been
sought.** This was a deliberate decision, not an oversight. What we do carry:

- FolkFriend is free and non-commercial
- The `Z:id` is preserved on every setting as a `zid` field (the stored `abc` is
  body-only by schema, so it cannot live in the ABC itself)
- `COPYRIGHT_NOTICE` travels in `norbeck.json` and in `datasets.json`, and the
  app shows it
- Every tune links back to norbeck.nu
- The dataset is **excluded from the legacy merged bundle**, because clients
  reading that file cannot opt out

**Takedown is one file.** Because datasets are modular, honouring an objection
is deleting `public/norbeck.json` and its `datasets.json` entry. Existing
installs then report it unavailable and keep working. Emailing him is still the
cheap move — he reportedly grants permission readily for non-commercial sites.

### Pipeline

1. `download_norbeck_data.py` — resolves the current `hnYYYYMM.zip` name **from
   the download page** (it encodes the release date, so a pinned name silently
   404s the day he updates), fetches ~640 KB, extracts 72 `.abc` files into
   `data/norbeck/abc/`, records a manifest with the zip's sha1.
2. `discover_norbeck_refs.py` — scrapes the 49 listing pages for the
   `(rhythm, ref)` pairs that actually exist. Cached; not part of every build.
3. `build_norbeck_data.py` — parses, assigns IDs, generates contours.

### Things that were not obvious

- **`Z:id` is the primary key.** `hn-reel-1` is unique, stable across releases,
  and he requires it be kept. IDs are `sha1(key)[:8]` plus a base.

  **Every fallback is derived from CONTENT, never from position** — this is the
  property that stops a user's favourites silently repointing when Norbeck
  publishes a new zip:

  - 28 blocks carry an unsubstituted template (`hn-%R-%X`) or no `Z:` at all,
    and key off `sha1(title|meter|mode|body)`.
  - 8 `Z:id`s are duplicated. **Both** halves of each pair get a content
    suffix, which is why the build runs a parse pass before an ID pass: if only
    the second were suffixed, the first would keep the bare key purely for
    being encountered first, and the two would swap IDs the moment they swapped
    places in the file.
  - Only two byte-identical blocks fall back to encounter order, and there it
    genuinely does not matter which wins.

  The earlier scheme used `<relpath>#<block index>` and a `#2` suffix. Inserting
  one tune near the top of `hnr0.abc` would have shifted every later fallback ID
  onto a different tune. `norbeck_pipeline_test.py` asserts the derived keys are
  identical when the blocks are reversed and when a tune is inserted before them.
- **Per-tune URLs are `display.asp?rhythm=<rhythm>&ref=<n>`, and `<n>` is the
  Z:id number — but only ~90% of the time, and a wrong ref returns HTTP 500,
  not 404.** Hence `site_refs.json`: emit a deep link only when the pair is
  known to exist, else fall back to the rhythm's listing page. Result: 88.7%
  deep links, 100% working URLs. `source_url` is mandatory here because the app
  cannot derive a Norbeck URL from a tune ID.
- **The Z:id rhythm token and the site's `rhythm` parameter disagree** for
  several families, non-derivably: `sp`→`slängpolska`, `slipjig`→`slip jig`,
  `jp`→`polska J`, `hf`→`highland`, `setdance`→`set dance`. `ZID_TO_SITE_RHYTHM`
  was read off the site's own category navigation.
- **A `P:` line in the body starts another complete setting of the tune**, and
  each becomes its own setting sharing the tune's id — the same shape
  thesession has. 1,062 sections across 924 tunes, giving **4,521 settings over
  3,472 tunes**.

  This was first read as "variations are fragments to discard": the contour came
  from the head of the body and the rest was kept only for display. Measuring
  settled it — against the head of the same tune the median section is
  **1.01×** its length, and only 2 of 836 `variations` sections are under a
  quarter. They are complete alternative renderings, not snippets.

  The old behaviour left 925 tunes with material the app could show but never
  match, and left the **12 tunes whose body *starts* with a `P:`** (songs, where
  every verse is a part) with an empty head, an empty contour, and no way to be
  found at all. Splitting fixed all 12; the dataset now has **zero** empty
  contours.

  Setting ids pack the section index (`base + hash*100 + n`) so a tune's
  settings sort together and the head comes first — the Rust side orders
  settings by numeric id, so otherwise a variation could be listed above the
  tune it varies. The section's `P:` label is kept in the stored `abc`, where
  ABCJS renders it above the score, and stripped from what abc2midi sees, where
  it would trigger part expansion.

  **Norbeck setting ids therefore changed** (tune ids did not), so the
  uniqueness check is split: setting ids must be unique outright, while tune ids
  are shared by a tune's settings and are only checked for two different source
  keys landing on the same id.
- **45% of tunes have no `L:` field**, and 290 of those are in 2/4 or 3/8 where
  the ABC default is 1/16, not 1/8. See `abc_common.py` above.
- **Every accented character is a TeX escape.** See `decode_abc_escapes`.
- Each file opens with a copyright preamble before the first `X:`, which
  becomes its own block and is rejected by `parse_abc_tune` for having no
  `T:`/`M:`/`K:`. That accounts for 72 of the 73 "unparseable blocks"; the 73rd
  genuinely has no `M:`.

### Verification

- **25/25 sampled Norbeck tunes self-match into the top 3** against the full
  66,249-setting index (Rust test `norbeck_self_match_ranks_first`).
- Contour length distribution matches thesession's (median 195 vs 200).
- 1,671 Norbeck tunes share a primary name with a thesession tune, and their
  contours agree closely — *The Bigamist* is **byte-identical at 195
  characters** across two independently transcribed sources, which is about as
  good a correctness signal as this pipeline can produce.
- 99.7% non-empty contours.
