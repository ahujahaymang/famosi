#!/usr/bin/env bash
# =============================================================================
# recover-instance.sh — Run this to finish setting up the EC2 instance
# after the user-data bootstrap script failed partway through.
#
# Usage (from your Mac):
#   cd infra
#   ./scripts/recover-instance.sh
#
# What it does:
#   1. Sends the full setup script to the instance via SSM
#   2. Waits for it to complete
#   3. Prints the output
# =============================================================================

set -euo pipefail

INSTANCE_ID="i-0a60550ccc4867f40"
REGION="us-east-1"

# Read secrets from SSM to embed in the .env on the instance
echo "=== Fetching secrets from SSM ==="
TELEGRAM_TOKEN=$(aws ssm get-parameter --name /famosi/TELEGRAM_BOT_TOKEN --with-decryption --query Parameter.Value --output text)
OPENAI_KEY=$(aws ssm get-parameter --name /famosi/OPENAI_API_KEY --with-decryption --query Parameter.Value --output text)
ADMIN_ID=$(aws ssm get-parameter --name /famosi/ADMIN_TELEGRAM_USER_ID --query Parameter.Value --output text 2>/dev/null || echo "0")
SUMMARIES_BUCKET="famosi-summaries-441870953351"
BACKUPS_BUCKET="famosi-backups-441870953351"

echo "=== Sending recovery script to instance $INSTANCE_ID ==="

CMD_ID=$(aws ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --timeout-seconds 600 \
  --parameters "commands=[
    \"set -euo pipefail\",
    \"exec > >(tee -a /var/log/famosi-init.log) 2>&1\",
    \"echo '=== Recovery started at '\\$(date) '==='\",

    \"# Fix pgvector — find pg_config wherever AL2023 put it\",
    \"PG_CONFIG=\\$(find /usr -name pg_config 2>/dev/null | head -1)\",
    \"echo pg_config: \\$PG_CONFIG\",
    \"if [ -z \\\"\\$PG_CONFIG\\\" ]; then echo 'pg_config not found, installing devel'; dnf install -y postgresql-devel postgresql-server-devel || true; PG_CONFIG=\\$(find /usr -name pg_config 2>/dev/null | head -1); fi\",
    \"echo Final pg_config: \\$PG_CONFIG\",
    \"dnf install -y gcc make git || true\",
    \"rm -rf /tmp/pgvector && git clone --branch v0.7.0 https://github.com/pgvector/pgvector.git /tmp/pgvector\",
    \"cd /tmp/pgvector && make PG_CONFIG=\\$PG_CONFIG && make install PG_CONFIG=\\$PG_CONFIG\",

    \"# Init and start postgres\",
    \"postgresql-setup --initdb 2>/dev/null || /usr/bin/postgresql-setup --initdb 2>/dev/null || true\",
    \"systemctl enable postgresql\",
    \"systemctl start postgresql\",
    \"sleep 3\",

    \"# Create DB, user, extension\",
    \"sudo -u postgres psql -c \\\"CREATE USER famosi WITH PASSWORD 'famosi_local' CREATEDB;\\\" 2>/dev/null || true\",
    \"sudo -u postgres psql -c \\\"CREATE DATABASE famosi OWNER famosi;\\\" 2>/dev/null || true\",
    \"sudo -u postgres psql -d famosi -c \\\"CREATE EXTENSION IF NOT EXISTS vector;\\\" 2>/dev/null || true\",

    \"# Configure pg_hba and listen address for asyncpg (TCP)\",
    \"PG_HBA=\\$(sudo -u postgres psql -t -c 'SHOW hba_file;' | xargs)\",
    \"PG_CONF=\\$(sudo -u postgres psql -t -c 'SHOW config_file;' | xargs)\",
    \"echo pg_hba: \\$PG_HBA\",
    \"echo pg_conf: \\$PG_CONF\",
    \"sed -i \\\"s/#listen_addresses = 'localhost'/listen_addresses = 'localhost'/\\\" \\$PG_CONF\",
    \"grep -q '127.0.0.1' \\$PG_HBA || echo 'host    all             all             127.0.0.1/32            md5' >> \\$PG_HBA\",
    \"sed -i 's/^local   all             all                                     peer/local   all             all                                     md5/' \\$PG_HBA || true\",
    \"sed -i 's/^local   all             all             peer/local   all             all             md5/' \\$PG_HBA || true\",
    \"systemctl restart postgresql\",
    \"sleep 3\",

    \"# Test DB connection\",
    \"PGPASSWORD=famosi_local psql -h 127.0.0.1 -U famosi -d famosi -c 'SELECT 1;'\",

    \"# Install cloudwatch agent\",
    \"dnf install -y amazon-cloudwatch-agent || true\",
    \"mkdir -p /var/log/famosi\",

    \"# Clone / update app\",
    \"if [ -d /opt/famosi/.git ]; then cd /opt/famosi && git pull; else git clone https://github.com/ahujahaymang/famosi.git /opt/famosi; fi\",
    \"cd /opt/famosi && python3.11 -m pip install -r requirements.txt\",

    \"# Write .env\",
    \"cat > /opt/famosi/.env << ENVEOF\",
    \"BOT_MODE=polling\",
    \"TELEGRAM_BOT_TOKEN=$TELEGRAM_TOKEN\",
    \"OPENAI_API_KEY=$OPENAI_KEY\",
    \"ROUTER_MODEL=gpt-4.1-nano\",
    \"EXTRACTION_MODEL=gpt-4.1-nano\",
    \"CONVERSATION_MODEL=gpt-4.1-mini\",
    \"REASONING_MODEL=anthropic.claude-sonnet-4-5-20251001-v1:0\",
    \"ESCALATION_MODEL=anthropic.claude-sonnet-4-5-20251001-v1:0\",
    \"EMBEDDING_MODEL=text-embedding-3-small\",
    \"LLM_PROVIDER_NANO=openai\",
    \"LLM_PROVIDER_MINI=openai\",
    \"LLM_PROVIDER_REASONING=bedrock\",
    \"LLM_PROVIDER_ESCALATION=bedrock\",
    \"DATABASE_URL=postgresql+asyncpg://famosi:famosi_local@127.0.0.1/famosi\",
    \"REDIS_URL=\",
    \"AWS_REGION=$REGION\",
    \"AWS_BEDROCK_REGION=$REGION\",
    \"S3_BUCKET_SUMMARIES=$SUMMARIES_BUCKET\",
    \"S3_BUCKET_BACKUPS=$BACKUPS_BUCKET\",
    \"ADMIN_TELEGRAM_USER_ID=$ADMIN_ID\",
    \"CURRENT_POLICY_VERSION=1.0\",
    \"LOG_LEVEL=INFO\",
    \"ENVEOF\",

    \"# Run migrations\",
    \"cd /opt/famosi && python3.11 -m alembic upgrade head\",

    \"# Install systemd service\",
    \"cat > /etc/systemd/system/famosi.service << 'SVCEOF'\",
    \"[Unit]\",
    \"Description=Famosi Telegram Bot\",
    \"After=network.target postgresql.service\",
    \"Requires=postgresql.service\",
    \"\",
    \"[Service]\",
    \"Type=simple\",
    \"User=root\",
    \"WorkingDirectory=/opt/famosi\",
    \"EnvironmentFile=/opt/famosi/.env\",
    \"ExecStart=/usr/bin/python3.11 -m app.main\",
    \"Restart=always\",
    \"RestartSec=10\",
    \"StandardOutput=append:/var/log/famosi/app.log\",
    \"StandardError=append:/var/log/famosi/app.log\",
    \"\",
    \"[Install]\",
    \"WantedBy=multi-user.target\",
    \"SVCEOF\",
    \"systemctl daemon-reload\",
    \"systemctl enable famosi\",
    \"systemctl restart famosi\",

    \"# Nightly backup cron\",
    \"echo '0 2 * * * root PGPASSWORD=famosi_local pg_dump -U famosi -h 127.0.0.1 famosi | gzip | aws s3 cp - s3://$BACKUPS_BUCKET/\\$(date +\\\\%Y-\\\\%m-\\\\%d).sql.gz' > /etc/cron.d/famosi-backup\",

    \"echo '=== Recovery complete at '\\$(date) '==='\",
    \"systemctl status famosi --no-pager\"
  ]" \
  --query 'Command.CommandId' --output text)

echo "Command sent: $CMD_ID"
echo "Waiting for completion (up to 10 minutes)..."

# Poll until done
for i in $(seq 1 60); do
  sleep 10
  STATUS=$(aws ssm get-command-invocation \
    --command-id "$CMD_ID" \
    --instance-id "$INSTANCE_ID" \
    --query 'Status' --output text 2>/dev/null || echo "Pending")
  echo "  [$((i*10))s] Status: $STATUS"
  if [[ "$STATUS" == "Success" || "$STATUS" == "Failed" || "$STATUS" == "Cancelled" ]]; then
    break
  fi
done

echo ""
echo "=== Output ==="
aws ssm get-command-invocation \
  --command-id "$CMD_ID" \
  --instance-id "$INSTANCE_ID" \
  --query 'StandardOutputContent' --output text

echo ""
echo "=== Errors (if any) ==="
aws ssm get-command-invocation \
  --command-id "$CMD_ID" \
  --instance-id "$INSTANCE_ID" \
  --query 'StandardErrorContent' --output text
