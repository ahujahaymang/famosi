#!/usr/bin/env bash
# =============================================================================
# put-secrets.sh — Store Famosi secrets in AWS SSM Parameter Store
#
# Run this once before `cdk deploy`. It reads values from your local .env
# and uploads them as SSM parameters in the target AWS account.
#
# Usage:
#   cd infra
#   AWS_PROFILE=your-profile ./scripts/put-secrets.sh
#
# Or with explicit credentials:
#   AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_DEFAULT_REGION=us-east-1 \
#     ./scripts/put-secrets.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "❌ .env not found at $ENV_FILE"
  echo "   Run this script from the infra/ directory."
  exit 1
fi

# Parse .env with Python — handles inline comments, quoted values, and
# special characters in values (colons, slashes, etc.) correctly.
get_env() {
  local key="$1"
  python3 - "$ENV_FILE" "$key" << 'PYEOF'
import sys, re
path, key = sys.argv[1], sys.argv[2]
for line in open(path):
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    # Strip inline comments (space + #)
    line = re.sub(r'\s+#.*$', '', line)
    if '=' not in line:
        continue
    k, _, v = line.partition('=')
    if k.strip() == key:
        # Strip optional surrounding quotes
        v = v.strip().strip('"').strip("'")
        print(v, end='')
        sys.exit(0)
PYEOF
}

echo "=== Famosi — storing secrets in SSM Parameter Store ==="
echo ""

put_secure() {
  local name="$1"
  local value="$2"
  if [[ -z "$value" ]]; then
    echo "  ⚠️  Skipping $name (empty in .env)"
    return
  fi
  aws ssm put-parameter \
    --name "/famosi/$name" \
    --value "$value" \
    --type SecureString \
    --overwrite \
    --no-cli-pager \
    --output text \
    > /dev/null
  echo "  ✓ /famosi/$name"
}

put_plain() {
  local name="$1"
  local value="$2"
  if [[ -z "$value" ]]; then
    echo "  ⚠️  Skipping $name (empty in .env)"
    return
  fi
  aws ssm put-parameter \
    --name "/famosi/$name" \
    --value "$value" \
    --type String \
    --overwrite \
    --no-cli-pager \
    --output text \
    > /dev/null
  echo "  ✓ /famosi/$name"
}

# Required
put_secure "TELEGRAM_BOT_TOKEN"      "$(get_env TELEGRAM_BOT_TOKEN)"
put_secure "OPENAI_API_KEY"          "$(get_env OPENAI_API_KEY)"

# Admin
put_plain  "ADMIN_TELEGRAM_USER_ID"  "$(get_env ADMIN_TELEGRAM_USER_ID)"

# Payment (skipped if empty)
put_secure "RAZORPAY_KEY_ID"         "$(get_env RAZORPAY_KEY_ID)"
put_secure "RAZORPAY_KEY_SECRET"     "$(get_env RAZORPAY_KEY_SECRET)"
put_secure "RAZORPAY_WEBHOOK_SECRET" "$(get_env RAZORPAY_WEBHOOK_SECRET)"
put_secure "STRIPE_SECRET_KEY"       "$(get_env STRIPE_SECRET_KEY)"
put_secure "STRIPE_WEBHOOK_SECRET"   "$(get_env STRIPE_WEBHOOK_SECRET)"
put_plain  "STRIPE_PRICE_ID"         "$(get_env STRIPE_PRICE_ID)"

echo ""
echo "✅ Done. Now run: cdk deploy"
echo ""
echo "   View stored params:"
echo "   aws ssm get-parameters-by-path --path /famosi/ --with-decryption --query 'Parameters[*].{Name:Name,Value:Value}' --output table"
