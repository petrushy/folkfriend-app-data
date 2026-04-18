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

# Ensure file storing previous hashes exists
mkdir -p data/

OLD_HASH=data/old_hash.txt
NEW_HASH=data/new_hash.txt

touch $OLD_HASH

# Python virtualenv
. env.sh

python src/download_thesession_data.py $SCRIPTPATH
sha1sum data/tunes.json data/aliases.json &> $NEW_HASH

should_rebuild=0

if cmp --silent -- "$OLD_HASH" "$NEW_HASH"
then
    echo ""
    echo "thesession.org data has not changed."
    echo ""
else
    cat $NEW_HASH > $OLD_HASH
    should_rebuild=1
fi

python src/download_folkwiki_data.py $SCRIPTPATH --offline

if ! python src/validate_output.py $SCRIPTPATH --manifest-path data/folkwiki/manifest.json --pageid-path data/folkwiki/hexhash_to_pageid.json --allow-missing-output
then
    echo "Existing output validation indicates Folkwiki maintenance is needed; rebuilding."
    should_rebuild=1
fi

if [ "$should_rebuild" -eq 0 ]
then
    echo "No thesession or Folkwiki-triggering changes detected. Exiting."
    deactivate
    exit 0
fi

python src/build_folkwiki_data.py $SCRIPTPATH
python src/build_non_user_data.py $SCRIPTPATH
python src/validate_output.py $SCRIPTPATH --manifest-path data/folkwiki/manifest.json --pageid-path data/folkwiki/hexhash_to_pageid.json
mv data/folkfriend-non-user-data.json ../public/
mv data/nud-meta.json ../public/
cd ..
git add public/folkfriend-non-user-data.json
git add public/nud-meta.json
git commit -m "`cat public/nud-meta.json`"
git push
/usr/local/bin/firebase deploy
deactivate
