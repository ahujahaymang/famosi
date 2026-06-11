# Famosi — Deployment Guide

Single t3.micro EC2 with local PostgreSQL. Free tier eligible (~$0 for 12 months).

## Architecture

```
Internet ──► EC2 t3.micro (Amazon Linux 2023)
               ├── PostgreSQL 16 + pgvector (local)
               ├── Famosi bot (systemd, polling mode)
               └── CloudWatch agent (logs → /famosi/app)

S3: famosi-summaries-<account>   (PDF doctor visit exports)
S3: famosi-backups-<account>     (nightly pg_dump, 7-day retention)
SSM: /famosi/*                   (all secrets)
```

## Prerequisites

```bash
# 1. Install AWS CLI v2
# https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html

# 2. Configure credentials for the new account
aws configure
# Enter: Access Key ID, Secret Access Key, Region (us-east-1), output (json)

# 3. Verify access
aws sts get-caller-identity
```

> **Note:** Use `npx cdk` (not bare `cdk`) — Homebrew installs a broken Python CDK
> shim at `/opt/homebrew/bin/cdk`. The correct one lives in `node_modules/.bin/cdk`
> and `npx cdk` picks it up automatically from there.

## Deploy steps

```bash
# 1. Store secrets (reads from your local .env automatically)
cd infra
./scripts/put-secrets.sh

# 2. Bootstrap CDK in the new account (one-time only)
npx cdk bootstrap

# 3. Deploy
npx cdk deploy

# Optional: with email alerts
npx cdk deploy --context alertEmail=your@email.com --context monthlyBudgetUsd=15
```

## After deploy

The stack outputs:
- **InstanceId** — use this to connect via SSM
- **PublicIp** — the instance's public IP
- **SsmSessionCommand** — copy-paste to connect without SSH

```bash
# Connect to the instance (no key pair needed)
aws ssm start-session --target <InstanceId>

# Check bot status
systemctl status famosi

# View live logs
journalctl -u famosi -f
# or
tail -f /var/log/famosi/app.log

# View logs in CloudWatch
aws logs tail /famosi/app --follow
```

## Updating the bot after a code push

```bash
# Connect to the instance
aws ssm start-session --target <InstanceId>

# Pull latest code and restart
cd /opt/famosi
git pull
pip3.11 install -r requirements.txt
python3.11 -m alembic upgrade head
systemctl restart famosi
```

## Rotating secrets

```bash
# Re-run the put-secrets script to update any value
cd infra
./scripts/put-secrets.sh

# Then restart the bot on the instance to pick up the new values
aws ssm send-command \
  --instance-ids <InstanceId> \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["cd /opt/famosi && source <(aws ssm get-parameters-by-path --path /famosi/ --with-decryption --query Parameters[*].[Name,Value] --output text | sed s|/famosi/||g | awk \"{print \\$1=\\$2}\") && systemctl restart famosi"]'

# Simpler: just SSM in and restart manually
```

## Costs (us-east-1)

| Resource | Free tier | After 12 months |
|---|---|---|
| t3.micro EC2 | Free (750h/month) | ~$8/month |
| 20GB gp3 EBS | Free (30GB/month) | ~$1.60/month |
| S3 (minimal) | Free (5GB) | ~$0.05/month |
| CloudWatch logs | Free (5GB) | ~$0.50/month |
| Data transfer | Free (100GB out) | minimal |
| **Total** | **$0** | **~$10/month** |

## Teardown

```bash
cdk destroy
# Note: S3 buckets have RemovalPolicy.RETAIN — delete manually if needed
# Note: EBS root volume has deleteOnTermination=false — delete manually
```
