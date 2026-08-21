#!/bin/bash
set -e
set -o pipefail

SCRIPT=`realpath $0`
SCRIPTPATH=`dirname $SCRIPT`
cd $SCRIPTPATH

ABC_VER=`./abc2midi -ver`
# abc2midi 5.02 (February 2025) — symlink build/abc2midi to the system binary
SUPPORTED_ABC_VER="5.02 February 16 2025 abc2midi"

if [[ "$ABC_VER" == "$SUPPORTED_ABC_VER" ]]
then
    echo "Found abc2midi version $ABC_VER"
else
    echo "Please ensure abc2midi version $SUPPORTED_ABC_VER is installed."
    echo "Current version: $ABC_VER"
    echo "See https://github.com/sshlien/abcmidi"
    echo "Provide this executable as 'abc2midi' in the build directory."
    exit 1
fi

mkdir -p data/hashes

# Python virtualenv
. env.sh

# Per-dataset change detection.
#
# This used to be one SHA1 over thesession's two files, which meant a folkwiki
# or norbeck change could never trigger a build. It is now per dataset, and
# each dataset is rebuilt only when ITS OWN inputs changed — which is the whole
# point of splitting the published index: a folkwiki refresh must not bump
# thesession's version and force every user into a 35 MB download.
#
# HASHES ARE ONLY PERSISTED ONCE THE DEPLOY HAS SUCCEEDED. Recording them here,
# at detection time, means a failure anywhere later — abc2midi dying, a failed
# validation, a Firebase blip — leaves the input marked as processed, so the
# next run sees "unchanged", exits early, and that dataset is never retried
# until its upstream source happens to change again. `set -e` makes that silent:
# the script simply stops, and the stale hash is already on disk.
#
# `check_changed <name> <files...>` sets CHANGED[<name>] and stashes the new
# hash in PENDING_HASH[<name>]; commit_hashes writes them at the very end.
declare -A CHANGED
declare -A PENDING_HASH

check_changed () {
    local name="$1"; shift
    local hash_file="data/hashes/${name}.sha1"
    local new_hash
    touch "$hash_file"
    new_hash=$(sha1sum "$@" 2>/dev/null | sha1sum || echo missing)
    PENDING_HASH[$name]="$new_hash"
    if [[ "$(cat "$hash_file")" == "$new_hash" ]]; then
        CHANGED[$name]=0
        echo "  $name: inputs unchanged"
    else
        CHANGED[$name]=1
        echo "  $name: inputs changed, will rebuild"
    fi
}

commit_hashes () {
    local name
    for name in "${!PENDING_HASH[@]}"; do
        echo "${PENDING_HASH[$name]}" > "data/hashes/${name}.sha1"
    done
    echo "Recorded input hashes for: ${!PENDING_HASH[*]}"
}

echo "==> Fetching sources"
python src/download_thesession_data.py $SCRIPTPATH
python src/download_folkwiki_data.py $SCRIPTPATH --offline
python src/download_norbeck_data.py $SCRIPTPATH

echo "==> Checking for changes"
check_changed thesession data/tunes.json data/aliases.json
check_changed folkwiki data/folkwiki/manifest.json
check_changed norbeck data/norbeck/manifest.json

# The folkwiki validator doubles as a maintenance signal: if source_url or
# page-id coverage has decayed, rebuild folkwiki even when its manifest is
# unchanged.
if ! python src/validate_output.py $SCRIPTPATH \
        --dataset folkwiki \
        --manifest-path data/folkwiki/manifest.json \
        --pageid-path data/folkwiki/hexhash_to_pageid.json \
        --allow-missing-output
then
    echo "Existing folkwiki output needs maintenance; rebuilding it."
    CHANGED[folkwiki]=1
fi

# A missing build product must always be rebuilt, whatever the hashes say —
# otherwise a deleted data/ leaves assemble_datasets silently omitting a
# dataset from the manifest, and the app stops offering it.
for ds in thesession folkwiki norbeck; do
    if [[ ! -f "data/${ds}.json" ]]; then
        echo "  $ds: no data/${ds}.json, forcing rebuild"
        CHANGED[$ds]=1
    fi
done

if [[ "${CHANGED[thesession]}" -eq 0 && "${CHANGED[folkwiki]}" -eq 0 \
      && "${CHANGED[norbeck]}" -eq 0 ]]
then
    echo ""
    echo "No dataset inputs have changed. Exiting."
    echo ""
    # Nothing was rebuilt, so there is nothing new to record — and the stored
    # hashes already match by definition.
    deactivate
    exit 0
fi

[[ "${CHANGED[thesession]}" -eq 1 ]] && python src/build_thesession_data.py $SCRIPTPATH
[[ "${CHANGED[folkwiki]}" -eq 1 ]] && python src/build_folkwiki_data.py $SCRIPTPATH
[[ "${CHANGED[norbeck]}" -eq 1 ]] && python src/build_norbeck_data.py $SCRIPTPATH

# Only the rebuilt datasets get today's version; the rest keep the version
# already published, so an unchanged 35 MB file is not re-downloaded by every
# client just because a sibling dataset moved.
VERSION_ARGS=()
for ds in thesession folkwiki norbeck; do
    if [[ "${CHANGED[$ds]}" -eq 0 ]]; then
        prev=$(python -c "import json,sys;
d=json.load(open('../public/datasets.json'))
print(next((e['v'] for e in d['datasets'] if e['id']=='$ds'), ''))" 2>/dev/null || true)
        if [[ -n "$prev" ]]; then
            VERSION_ARGS+=(--set-version "$ds=$prev")
        fi
    fi
done

echo "==> Assembling published files"
python src/assemble_datasets.py $SCRIPTPATH "${VERSION_ARGS[@]}"

echo "==> Validating output"
python src/validate_output.py $SCRIPTPATH \
    --manifest-path data/folkwiki/manifest.json \
    --pageid-path data/folkwiki/hexhash_to_pageid.json

for f in datasets.json thesession.json folkwiki.json norbeck.json \
         folkfriend-non-user-data.json nud-meta.json; do
    [[ -f "data/$f" ]] && mv "data/$f" ../public/
done

cd ..
git add public/
git commit -m "`cat public/nud-meta.json`"
git push
firebase deploy

# Everything succeeded — only now is it safe to say these inputs are processed.
cd "$SCRIPTPATH"
commit_hashes
deactivate
