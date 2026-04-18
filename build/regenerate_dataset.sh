#!/bin/bash
set -euo pipefail

SCRIPT="$(realpath "$0")"
SCRIPTPATH="$(dirname "$SCRIPT")"
cd "$SCRIPTPATH"

DEPLOY=0
SKIP_PAGEIDS=0
SKIP_FILL_MISSING=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --deploy)
            DEPLOY=1
            shift
            ;;
        --skip-pageids)
            SKIP_PAGEIDS=1
            shift
            ;;
        --skip-fill-missing)
            SKIP_FILL_MISSING=1
            shift
            ;;
        -h|--help)
            cat <<'EOF'
Usage: bash build/regenerate_dataset.sh [options]

Refreshes the full dataset from upstream sources:
  1. Downloads TheSession data
  2. Downloads live Folkwiki ABC files
  3. Rebuilds Folkwiki page-id mappings
  4. Fills missing Folkwiki entries missed by CDX
  5. Rebuilds merged output JSON files
  6. Validates the generated dataset

Options:
  --deploy             Copy outputs to public/ and run firebase deploy --only hosting
  --skip-pageids       Skip discover_folkwiki_pageids.py
  --skip-fill-missing  Skip fill_missing_folkwiki.py
  -h, --help           Show this help text
EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

if [[ ! -f ./abc2midi ]]; then
    echo "Missing ./abc2midi. See build/CLAUDE.md for setup." >&2
    exit 1
fi

. env.sh

echo "==> Downloading TheSession data"
python src/download_thesession_data.py .

echo "==> Downloading live Folkwiki data"
python src/download_folkwiki_data.py .

if [[ "$SKIP_PAGEIDS" -eq 0 ]]; then
    echo "==> Refreshing Folkwiki page-id mapping"
    python src/discover_folkwiki_pageids.py .
else
    echo "==> Skipping Folkwiki page-id mapping refresh"
fi

if [[ "$SKIP_FILL_MISSING" -eq 0 ]]; then
    echo "==> Filling missing Folkwiki entries"
    python src/fill_missing_folkwiki.py .
else
    echo "==> Skipping fill_missing_folkwiki.py"
fi

echo "==> Building Folkwiki processed data"
python src/build_folkwiki_data.py .

echo "==> Building merged non-user data"
python src/build_non_user_data.py .

echo "==> Validating output"
python src/validate_output.py . \
    --manifest-path data/folkwiki/manifest.json \
    --pageid-path data/folkwiki/hexhash_to_pageid.json

if [[ "$DEPLOY" -eq 1 ]]; then
    echo "==> Copying outputs to public/"
    cp data/folkfriend-non-user-data.json ../public/
    cp data/nud-meta.json ../public/

    echo "==> Deploying to Firebase Hosting"
    (
        cd ..
        firebase deploy --only hosting
    )
fi

echo "==> Done"
echo "Generated files:"
echo "  $SCRIPTPATH/data/folkfriend-non-user-data.json"
echo "  $SCRIPTPATH/data/nud-meta.json"
