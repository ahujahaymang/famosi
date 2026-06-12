#!/bin/bash
set -euo pipefail
echo "=== pg fix v3 ==="

# Find pg_hba.conf and postgresql.conf directly from filesystem
PG_DATA=$(find /var/lib/pgsql /var/lib/postgresql -name pg_hba.conf 2>/dev/null | head -1 | xargs dirname)
echo "PG_DATA: $PG_DATA"

PG_HBA="$PG_DATA/pg_hba.conf"
PG_CONF="$PG_DATA/postgresql.conf"

# Enable TCP listening
sed -i "s/^#listen_addresses.*/listen_addresses = 'localhost'/" "$PG_CONF" || true
sed -i "s/^listen_addresses.*/listen_addresses = 'localhost'/" "$PG_CONF" || true

# Add md5 TCP rule if not present
grep -q '127.0.0.1.*md5' "$PG_HBA" || echo "host all all 127.0.0.1/32 md5" >> "$PG_HBA"

# Replace peer with md5 for local socket connections
sed -i 's/^local[[:space:]]\+all[[:space:]]\+all[[:space:]]\+peer$/local   all             all             md5/' "$PG_HBA" || true

echo "=== pg_hba.conf after edit ==="
grep -v '^#' "$PG_HBA" | grep -v '^$'

systemctl restart postgresql
sleep 3
echo "=== testing connection ==="
PGPASSWORD=famosi_local psql -h 127.0.0.1 -U famosi -d famosi -c "SELECT version();" && echo "DB_TCP_OK"

# Load pgvector extension
PGPASSWORD=famosi_local psql -h 127.0.0.1 -U famosi -d famosi -c "CREATE EXTENSION IF NOT EXISTS vector;" && echo "VECTOR_OK"
echo "=== pg fix done ==="
