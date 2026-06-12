#!/usr/bin/env python3
"""Writes the recovery script to /tmp/famosi-final.sh with secrets interpolated."""
import sys, os

telegram_token = sys.argv[1]
openai_key = sys.argv[2]
admin_id = sys.argv[3]

script = f"""#!/bin/bash
set -euo pipefail
exec > >(tee -a /var/log/famosi-init.log) 2>&1
echo "=== Final recovery at $(date) ==="

# Install the correct server devel package (provides pg_server_config)
dnf install -y postgresql16-server-devel

# Find pg_config binary (AL2023 names it pg_server_config)
PG_CONFIG=$(rpm -ql postgresql16-server-devel 2>/dev/null | grep -E 'bin.*(pg_config|pg_server_config)' | head -1)
echo "PG_CONFIG: $PG_CONFIG"
$PG_CONFIG --version

# Build pgvector v0.7.0
rm -rf /tmp/pgvector
git clone --branch v0.7.0 https://github.com/pgvector/pgvector.git /tmp/pgvector
cd /tmp/pgvector
make PG_CONFIG=$PG_CONFIG
make install PG_CONFIG=$PG_CONFIG
echo "pgvector installed"

# Initialise PostgreSQL
postgresql-setup --initdb 2>/dev/null || echo "already initialised"
systemctl enable postgresql
systemctl start postgresql
sleep 3

# Create user and database
sudo -u postgres psql -c "CREATE USER famosi WITH PASSWORD 'famosi_local' CREATEDB;" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE famosi OWNER famosi;" 2>/dev/null || true
sudo -u postgres psql -d famosi -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>/dev/null || true

# Enable TCP connections for asyncpg
PG_HBA=$(sudo -u postgres psql -t -c "SHOW hba_file;" | xargs)
PG_CONF=$(sudo -u postgres psql -t -c "SHOW config_file;" | xargs)
sed -i "s/#listen_addresses = 'localhost'/listen_addresses = 'localhost'/" "$PG_CONF" || true
grep -q '127.0.0.1.*md5' "$PG_HBA" || echo "host all all 127.0.0.1/32 md5" >> "$PG_HBA"
sed -i 's/^local[[:space:]]*all[[:space:]]*all[[:space:]]*peer$/local   all             all             md5/' "$PG_HBA" || true
systemctl restart postgresql
sleep 3
PGPASSWORD=famosi_local psql -h 127.0.0.1 -U famosi -d famosi -c "SELECT 1;" && echo "DB connection OK"

# Clone / update app
mkdir -p /var/log/famosi
if [ -d /opt/famosi/.git ]; then
  cd /opt/famosi && git pull
else
  git clone https://github.com/ahujahaymang/famosi.git /opt/famosi
fi
cd /opt/famosi
python3.11 -m pip install -r requirements.txt --quiet

# Write .env
cat > /opt/famosi/.env << 'ENVEOF'
BOT_MODE=polling
TELEGRAM_BOT_TOKEN={telegram_token}
OPENAI_API_KEY={openai_key}
ROUTER_MODEL=gpt-4.1-nano
EXTRACTION_MODEL=gpt-4.1-nano
CONVERSATION_MODEL=gpt-4.1-mini
REASONING_MODEL=anthropic.claude-sonnet-4-5-20251001-v1:0
ESCALATION_MODEL=anthropic.claude-sonnet-4-5-20251001-v1:0
EMBEDDING_MODEL=text-embedding-3-small
LLM_PROVIDER_NANO=openai
LLM_PROVIDER_MINI=openai
LLM_PROVIDER_REASONING=bedrock
LLM_PROVIDER_ESCALATION=bedrock
DATABASE_URL=postgresql+asyncpg://famosi:famosi_local@127.0.0.1/famosi
REDIS_URL=
AWS_REGION=us-east-1
AWS_BEDROCK_REGION=us-east-1
S3_BUCKET_SUMMARIES=famosi-summaries-441870953351
S3_BUCKET_BACKUPS=famosi-backups-441870953351
ADMIN_TELEGRAM_USER_ID={admin_id}
CURRENT_POLICY_VERSION=1.0
LOG_LEVEL=INFO
ENVEOF

# Run Alembic migrations
cd /opt/famosi && python3.11 -m alembic upgrade head
echo "Migrations done"

# Create systemd service
cat > /etc/systemd/system/famosi.service << 'SVCEOF'
[Unit]
Description=Famosi Telegram Bot
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/famosi
EnvironmentFile=/opt/famosi/.env
ExecStart=/usr/bin/python3.11 -m app.main
Restart=always
RestartSec=10
StandardOutput=append:/var/log/famosi/app.log
StandardError=append:/var/log/famosi/app.log

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable famosi
systemctl restart famosi
sleep 5
systemctl status famosi --no-pager
echo "=== Recovery complete at $(date) ==="
"""

with open('/tmp/famosi-final.sh', 'w') as f:
    f.write(script)

os.chmod('/tmp/famosi-final.sh', 0o755)
print(f"Written: {len(script)} bytes")
