#!/usr/bin/env python3
"""
Generates /tmp/famosi-deploy.sh with secrets baked in.
Usage: python3 generate_deploy.py <telegram_token> <openai_key> <admin_id> <github_token>
"""
import sys
import os

tok          = sys.argv[1]
key          = sys.argv[2]
admin        = sys.argv[3]
github_token = sys.argv[4]

# .env content as a plain Python string — no shell interpretation
env_lines = [
    "BOT_MODE=polling",
    f"TELEGRAM_BOT_TOKEN={tok}",
    f"OPENAI_API_KEY={key}",
    "ROUTER_MODEL=gpt-4.1-nano",
    "EXTRACTION_MODEL=gpt-4.1-nano",
    "CONVERSATION_MODEL=gpt-4.1-mini",
    "REASONING_MODEL=anthropic.claude-sonnet-4-5-20251001-v1:0",
    "ESCALATION_MODEL=anthropic.claude-sonnet-4-5-20251001-v1:0",
    "EMBEDDING_MODEL=text-embedding-3-small",
    "LLM_PROVIDER_NANO=openai",
    "LLM_PROVIDER_MINI=openai",
    "LLM_PROVIDER_REASONING=bedrock",
    "LLM_PROVIDER_ESCALATION=bedrock",
    "DATABASE_URL=postgresql+asyncpg://famosi:famosi_local@127.0.0.1/famosi",
    "REDIS_URL=",
    "AWS_REGION=us-east-1",
    "AWS_BEDROCK_REGION=us-east-1",
    "S3_BUCKET_SUMMARIES=famosi-summaries-441870953351",
    "S3_BUCKET_BACKUPS=famosi-backups-441870953351",
    f"ADMIN_TELEGRAM_USER_ID={admin}",
    "CURRENT_POLICY_VERSION=1.0",
    "LOG_LEVEL=INFO",
]
env_content = "\n".join(env_lines) + "\n"

# Embed env content as a Python string literal inside the bash script
# Encode as single line with \n so no literal newlines break the -c string
env_single_line = env_content.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

deploy_script = f"""#!/bin/bash
set -euo pipefail
exec > >(tee -a /var/log/famosi-deploy.log) 2>&1
echo "=== Famosi deploy ==="

systemctl stop famosi 2>/dev/null || true
systemctl reset-failed famosi 2>/dev/null || true

# Clone using token-authenticated URL
rm -rf /opt/famosi
git clone https://{github_token}@github.com/ahujahaymang/famosi.git /opt/famosi
cd /opt/famosi
python3.11 -m pip install -r requirements.txt --quiet
echo "pip install OK"

python3.11 -c "content=\\"{env_single_line}\\"; open('/opt/famosi/.env','w').write(content.replace('\\\\n','\\n')); print('.env written')"

python3.11 -m alembic upgrade head
echo "MIGRATIONS OK"

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

mkdir -p /var/log/famosi
systemctl daemon-reload
systemctl enable famosi
systemctl start famosi
sleep 6
systemctl status famosi --no-pager
echo "=== DONE ==="
"""

out = "/tmp/famosi-deploy.sh"
with open(out, "w") as f:
    f.write(deploy_script)
os.chmod(out, 0o755)
print(f"Written {len(deploy_script)} bytes to {out}")
