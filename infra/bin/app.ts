#!/usr/bin/env node
/**
 * Famosi CDK App
 *
 * Deploys a single t3.micro EC2 instance running:
 *   - PostgreSQL 16 + pgvector (installed on the instance)
 *   - Famosi Python bot (polling mode, systemd service)
 *   - S3 buckets for summaries and DB backups
 *
 * All secrets are pulled from SSM Parameter Store at boot — never baked
 * into the AMI or CloudFormation templates.
 *
 * Usage:
 *   # 1. Bootstrap CDK (first time only)
 *   cdk bootstrap
 *
 *   # 2. Store secrets in SSM (run scripts/put-secrets.sh first)
 *   ./scripts/put-secrets.sh
 *
 *   # 3. Deploy
 *   cdk deploy
 *
 *   # 4. (Optional) with email alerts and budget cap
 *   cdk deploy --context alertEmail=you@example.com --context monthlyBudgetUsd=15
 *
 * Optional context flags:
 *   alertEmail       — email for CloudWatch alarms (default: none)
 *   monthlyBudgetUsd — USD monthly budget alert threshold (default: 15)
 */

import * as cdk from 'aws-cdk-lib';
import { FamosiStack } from '../lib/famosi-stack';

const app = new cdk.App();

const alertEmail      = app.node.tryGetContext('alertEmail') as string | undefined;
const monthlyBudgetUsd = app.node.tryGetContext('monthlyBudgetUsd')
  ? Number(app.node.tryGetContext('monthlyBudgetUsd'))
  : 15;

new FamosiStack(app, 'FamosiStack', {
  alertEmail,
  monthlyBudgetUsd,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region:  process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
  },
  description: 'Famosi — AI pregnancy companion (EC2 + local PostgreSQL)',
});
