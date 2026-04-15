#!/bin/bash

SCRIPT=`realpath $0`
SCRIPTPATH=`dirname $SCRIPT`
cd $SCRIPTPATH

ABC_VER=`./abc2midi -ver`
SUPPORTED_ABC_VER="5.02 February 16 2025 abc2midi"

if [[ "$ABC_VER" == "$SUPPORTED_ABC_VER" ]]
then
    echo "Found abc2midi version $ABC_VER"
else
	echo "Please ensure abc2midi version $SUPPORTED_ABC_VER is installed."
	echo "Current version: $ABC_VER"
	echo "See https://github.com/sshlien/abcmidi"
	echo "Provide this executable as 'abc2midi' in the build directory."
    exit
fi

# Ensure file storing previous hashes existss
mkdir -p data/

OLD_HASH=data/old_hash.txt
NEW_HASH=data/new_hash.txt

touch $OLD_HASH

# Python virtualenv
. env.sh

python src/download_thesession_data.py $SCRIPTPATH
sha1sum data/tunes.json data/aliases.json &> $NEW_HASH

if cmp --silent -- "$OLD_HASH" "$NEW_HASH"
then
    echo ""
    echo "thesession.org data has not changed. Exiting."
    echo ""
    exit 1
else
    cat $NEW_HASH > $OLD_HASH
    python src/download_folkwiki_data.py $SCRIPTPATH
    python src/build_folkwiki_data.py $SCRIPTPATH
    python src/build_non_user_data.py $SCRIPTPATH
    mv data/folkfriend-non-user-data.json ../public/
    mv data/nud-meta.json ../public/
    cd ..
    git add public/folkfriend-non-user-data.json
    git add public/nud-meta.json
    git commit -m "`cat public/nud-meta.json`"
    git push
    /usr/local/bin/firebase deploy
    deactivate
fi
