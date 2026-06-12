#!/bin/bash
set -euo pipefail
echo "=== pg_hba fix ==="
PG_HBA=$(su -s /bin/bash postgres -c "psql -t -c \"SHOW hba_file;\"" | xargs)
PG_CONF=$(su -s /bin/bash postgres -c "psql -t -c \"SHOW config_file;\"" | xargs)
echo "HBA: $PG_HBA  CONF: $PG_CONF"
sed -i "s/^#listen_addresses.*/listen_addresses = 'localhost'/" "$PG_CONF" || true
grep -q '127.0.0.1.*md5' "$PG_HBA" || echo "host all all 127.0.0.1/32 md5" >> "$PG_HBA"
sed -i 's/^local[[:space:]]*all[[:space:]]*all[[:space:]]*peer/local   all             all             md5/' "$PG_HBA" || true
systemctl restart postgresql
sleep 3
PGPASSWORD=famosi_local psql -h 127.0.0.1 -U famosi -d famosi -c "SELECT 1;" && echo "DB_TCP_OK"
su -s /bin/bash postgres -c "psql -d famosi -c \"CREATE EXTENSION IF NOT EXISTS vector;\"" 2>/dev/null || echo "vector already present"
su -s /bin/bash postgres -c "psql -d famosi -c \"\dx\"" | grep -i vector && echo "PGVECTOR_OK"
echo "=== pg fix done ==="
