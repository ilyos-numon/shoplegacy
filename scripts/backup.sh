#!/usr/bin/env bash
# Dumps the database in pg_dump custom format to backups/ and keeps the newest 14.
# Usage: SHOPLEGACY_BACKUP_DSN=postgresql://... scripts/backup.sh
# pg_dump must have the same major version as the server. To use another client,
# set SHOPLEGACY_PG_DUMP to a command prefix, e.g. "docker run --rm -i postgres:17 pg_dump"
# (SHOPLEGACY_PSQL defaults to the same prefix with psql). See README "백업과 복원".
# Restore: pg_restore --clean --if-exists --no-owner -d "$TARGET_DSN" backups/<file>.dump
set -euo pipefail

: "${SHOPLEGACY_BACKUP_DSN:?SHOPLEGACY_BACKUP_DSN is required}"
KEEP=14
PG_DUMP="${SHOPLEGACY_PG_DUMP:-pg_dump}"
PSQL="${SHOPLEGACY_PSQL:-${PG_DUMP%pg_dump}psql}"

cd "$(dirname "$0")/.."

# Refuse to dump with a client whose major version differs from the server.
client_major=$($PG_DUMP --version | grep -oE '[0-9]+' | head -1)
server_major=$($PSQL "$SHOPLEGACY_BACKUP_DSN" -Atc "SHOW server_version_num" | cut -c1-2)
if [ "$client_major" != "$server_major" ]; then
  echo "pg_dump major $client_major != server $server_major; set SHOPLEGACY_PG_DUMP (see README)" >&2
  exit 3
fi

mkdir -p backups
out="backups/shoplegacy-$(date +%Y%m%d-%H%M).dump"
tmp="$out.partial"
trap 'rm -f "$tmp"' EXIT
$PG_DUMP -Fc "$SHOPLEGACY_BACKUP_DSN" > "$tmp"
mv "$tmp" "$out"
echo "$out"

# Remove all but the newest $KEEP dumps (names sort by timestamp).
ls -1 backups/shoplegacy-*.dump | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f "$old"
done
