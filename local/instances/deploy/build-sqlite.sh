#!/bin/sh
# Build the SQLite library the runtime images link instead of Debian's, into /sqlite for the final stage to copy.
# Debian's SQLite (3.46) plays back a journal file planted beside a database and deletes whatever file the journal's
# super-journal pointer names. Sidecar tool code can write beside agents' conversation databases, so the primary would
# delete files only it should reach; SQLite 3.53.3 and later delete only real super-journals.
# The options reproduce the features and limits of Debian's library, so queries behave as they did with it.
set -eu

version=3540100
sha256=926ef8d3eab93fbf6e06011901273eec076b25594a4e1cdadd50c987f4f6d64e

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gcc libc6-dev make
cd /tmp
curl -fsSL "https://sqlite.org/2026/sqlite-autoconf-${version}.tar.gz" -o sqlite.tar.gz
echo "${sha256}  sqlite.tar.gz" | sha256sum -c -
tar -xzf sqlite.tar.gz
cd "sqlite-autoconf-${version}"

# The dynamic linker searches /usr/local/lib/<multiarch> before Debian's library directory on every architecture.
libdir="/usr/local/lib/$(python3 -c 'import sysconfig; print(sysconfig.get_config_var("MULTIARCH"))')"
CFLAGS="-O2 \
  -DSQLITE_ALLOW_ROWID_IN_VIEW \
  -DSQLITE_ENABLE_DBPAGE_VTAB \
  -DSQLITE_ENABLE_FTS3_PARENTHESIS \
  -DSQLITE_ENABLE_FTS3_TOKENIZER \
  -DSQLITE_ENABLE_STMTVTAB \
  -DSQLITE_ENABLE_UNLOCK_NOTIFY \
  -DSQLITE_LIKE_DOESNT_MATCH_BLOBS \
  -DSQLITE_MAX_DEFAULT_PAGE_SIZE=32768 \
  -DSQLITE_MAX_SCHEMA_RETRY=25 \
  -DSQLITE_MAX_VARIABLE_NUMBER=250000 \
  -DSQLITE_SECURE_DELETE \
  -DSQLITE_SOUNDEX \
  -DSQLITE_USE_URI=1" \
  ./configure --prefix=/usr/local --libdir="${libdir}" --soname=legacy --disable-static \
  --fts3 --fts4 --fts5 --rtree --session --dbstat --column-metadata --update-limit
make -j"$(nproc)" install DESTDIR=/tmp/install
mkdir -p "/sqlite${libdir}"
cp -P "/tmp/install${libdir}"/libsqlite3.so* "/sqlite${libdir}/"
