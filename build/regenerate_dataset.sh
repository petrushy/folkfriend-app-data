#!/bin/bash
set -euo pipefail

SCRIPT="$(realpath "$0")"
SCRIPTPATH="$(dirname "$SCRIPT")"
cd "$SCRIPTPATH"

DEPLOY=0
SKIP_NORBECK_REFS=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --deploy)
            DEPLOY=1
            shift
            ;;
        --skip-norbeck-refs)
            SKIP_NORBECK_REFS=1
            shift
            ;;
        -h|--help)
            cat <<'EOF'
Usage: bash build/regenerate_dataset.sh [options]

Refreshes every dataset from upstream sources, ignoring change detection:
  1. Downloads TheSession data
  2. Takes a verified snapshot of folkwiki.se and packs it into
     archive/folkwiki/<date>.tar.gz (commit that file)
  3. Downloads Norbeck's ABC collection
  4. Refreshes Norbeck's display.asp (rhythm, ref) index
  5. Rebuilds all three dataset files
  6. Assembles datasets.json and the legacy merged bundle
  7. Validates everything

Options:
  --deploy             Copy outputs to public/ and run firebase deploy --only hosting
  --skip-norbeck-refs  Skip discover_norbeck_refs.py
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

echo "==> Taking a folkwiki snapshot (about two hours; resumable)"
# Fails, and packs nothing, unless every page was verified. Re-running on the
# same day resumes and retries only what is still unverified.
python src/archive_folkwiki.py .

echo "==> Downloading Norbeck collection"
python src/download_norbeck_data.py .

if [[ "$SKIP_NORBECK_REFS" -eq 0 ]]; then
    # Norbeck's per-tune pages are display.asp?rhythm=X&ref=N, and a wrong ref
    # returns HTTP 500 rather than 404. This snapshot is what lets the builder
    # emit a deep link only when the pair really exists.
    echo "==> Refreshing Norbeck display.asp ref index"
    python src/discover_norbeck_refs.py .
else
    echo "==> Skipping discover_norbeck_refs.py"
fi

echo "==> Building TheSession dataset"
python src/build_thesession_data.py .

echo "==> Building Folkwiki dataset"
python src/build_folkwiki_data.py .

echo "==> Building Norbeck dataset"
python src/build_norbeck_data.py .

echo "==> Assembling published files"
python src/assemble_datasets.py .

echo "==> Validating output"
python src/validate_output.py . \
    --folkwiki-snapshot "$(python -c "import sys; sys.path.insert(0, 'src');
from build_folkwiki_data import latest_snapshot; print(latest_snapshot('.'))")"

if [[ "$DEPLOY" -eq 1 ]]; then
    echo "==> Copying outputs to public/"
    # ONLY what assemble_datasets declared publishable — same rule as build.sh.
    # This used to name the files literally and so happily published Norbeck,
    # which must never be served (see assemble_datasets.py).
    if [[ ! -f data/PUBLISHED_FILES.txt ]]; then
        echo "assemble_datasets.py did not write data/PUBLISHED_FILES.txt" >&2
        exit 1
    fi
    while read -r f; do
        [[ -z "$f" ]] && continue
        [[ -f "data/$f" ]] && cp "data/$f" ../public/
    done < data/PUBLISHED_FILES.txt

    for f in norbeck.json; do
        if [[ -f "../public/$f" ]]; then
            echo "FATAL: ../public/$f exists but is not publishable" >&2
            exit 1
        fi
    done

    echo "==> Deploying to Firebase Hosting"
    (
        cd ..
        firebase deploy --only hosting
    )
fi

echo "==> Done"
echo "Generated files:"
for f in datasets.json thesession.json folkwiki.json norbeck.json \
         folkfriend-non-user-data.json nud-meta.json; do
    [[ -f "data/$f" ]] && echo "  $SCRIPTPATH/data/$f"
done
echo "Publishable (see data/PUBLISHED_FILES.txt):"
[[ -f data/PUBLISHED_FILES.txt ]] && sed 's/^/  /' data/PUBLISHED_FILES.txt
