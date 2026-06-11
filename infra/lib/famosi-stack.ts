import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatchActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import { Construct } from 'constructs';

export interface FamosiStackProps extends cdk.StackProps {
  /** Email for CloudWatch alarms and budget alerts. Optional. */
  alertEmail?: string;
  /** Monthly budget alert threshold in USD. Default: 15. */
  monthlyBudgetUsd?: number;
}

/**
 * Famosi deployment stack.
 *
 * Single t3.micro EC2 running everything:
 *   - PostgreSQL 16 + pgvector extension (local install, no RDS cost)
 *   - Famosi Python bot (systemd service, polling mode)
 *   - Two S3 buckets: summaries (PDF exports) and backups (pg_dump)
 *
 * Secrets stored in SSM Parameter Store under /famosi/:
 *   TELEGRAM_BOT_TOKEN   — bot token from @BotFather
 *   OPENAI_API_KEY       — OpenAI API key
 *   ADMIN_TELEGRAM_USER_ID — your Telegram user ID (integer)
 *   AWS_ACCESS_KEY_ID    — IAM credentials for Bedrock + S3 (optional if using instance role)
 *   AWS_SECRET_ACCESS_KEY
 *
 * Cost estimate (us-east-1):
 *   t3.micro EC2 + 20GB EBS = free tier / ~$8/month after
 *   S3 (minimal use)        = ~$0.05/month
 *   CloudWatch logs         = ~$0.50/month
 *   Total                   = ~$8.55/month (free in first 12 months)
 */
export class FamosiStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: FamosiStackProps = {}) {
    super(scope, id, props);

    const { alertEmail, monthlyBudgetUsd = 15 } = props;
    const prefix = 'famosi';
    const ssmPrefix = '/famosi';

    // ── VPC ──────────────────────────────────────────────────────────────
    // Single public subnet — no NAT gateway, saves ~$32/month.
    const vpc = new ec2.Vpc(this, 'Vpc', {
      vpcName: `${prefix}-vpc`,
      maxAzs: 1,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: 'public',
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 28,
        },
      ],
    });

    // ── Security group ────────────────────────────────────────────────────
    // Outbound: all (bot needs to reach Telegram + OpenAI + Bedrock + S3).
    // Inbound:  nothing — polling mode needs no open ports.
    //           Only port 443 is opened for future webhook mode or health checks.
    const appSg = new ec2.SecurityGroup(this, 'AppSg', {
      vpc,
      securityGroupName: `${prefix}-app-sg`,
      description: 'Famosi app - polling mode needs no inbound ports',
      allowAllOutbound: true,
    });
    // Port 443 reserved for future webhook mode. Harmless to have now.
    appSg.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      'HTTPS future webhook mode',
    );

    // ── IAM role for EC2 ─────────────────────────────────────────────────
    const role = new iam.Role(this, 'Ec2Role', {
      roleName: `${prefix}-ec2-role`,
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
      managedPolicies: [
        // SSM Session Manager — SSH without opening port 22
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'),
      ],
    });

    // Read all /famosi/* secrets from SSM
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter', 'ssm:GetParameters'],
      resources: [
        `arn:aws:ssm:${this.region}:${this.account}:parameter${ssmPrefix}/*`,
      ],
    }));

    // Bedrock — Claude Sonnet for reasoning/escalation tiers
    role.addToPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
      ],
      resources: ['arn:aws:bedrock:*::foundation-model/anthropic.claude-*'],
    }));

    // CloudWatch Logs — app writes structured JSON logs here
    role.addToPolicy(new iam.PolicyStatement({
      actions: [
        'logs:CreateLogGroup',
        'logs:CreateLogStream',
        'logs:PutLogEvents',
        'logs:DescribeLogStreams',
      ],
      resources: [
        `arn:aws:logs:${this.region}:${this.account}:log-group:/famosi/*`,
      ],
    }));

    // ── S3 buckets ────────────────────────────────────────────────────────

    // Summaries bucket — doctor visit PDF exports
    const summariesBucket = new s3.Bucket(this, 'SummariesBucket', {
      bucketName: `${prefix}-summaries-${this.account}`,
      versioned: false,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      lifecycleRules: [{
        id: 'expire-old-summaries',
        expiration: cdk.Duration.days(90),
      }],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    summariesBucket.grantReadWrite(role);

    // Backups bucket — nightly pg_dump
    const backupsBucket = new s3.Bucket(this, 'BackupsBucket', {
      bucketName: `${prefix}-backups-${this.account}`,
      versioned: false,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      lifecycleRules: [{
        id: 'expire-old-backups',
        expiration: cdk.Duration.days(7),
      }],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    backupsBucket.grantReadWrite(role);

    // ── EC2 user-data (runs once on first boot) ───────────────────────────
    const userData = ec2.UserData.forLinux();
    userData.addCommands(
      'set -euo pipefail',
      'exec > >(tee /var/log/famosi-init.log) 2>&1',
      'echo "=== Famosi bootstrap started ==="',

      // ── System packages ──────────────────────────────────────────────
      'dnf update -y',
      'dnf install -y python3.11 python3.11-pip git gcc make',

      // ── PostgreSQL 16 + pgvector ─────────────────────────────────────
      // Add PostgreSQL 16 repo for AL2023
      'dnf install -y https://download.postgresql.org/pub/repos/yum/reporpms/EL-9-x86_64/pgdg-redhat-repo-latest.noarch.rpm || true',
      'dnf -qy module disable postgresql || true',
      'dnf install -y postgresql16-server postgresql16-contrib postgresql16-devel',

      // Install pgvector from source (not in the PGDG repo for AL2023 yet)
      'dnf install -y git gcc make postgresql16-devel',
      'git clone --branch v0.7.0 https://github.com/pgvector/pgvector.git /tmp/pgvector',
      'cd /tmp/pgvector && make PG_CONFIG=/usr/pgsql-16/bin/pg_config && make install PG_CONFIG=/usr/pgsql-16/bin/pg_config',

      // Initialise the cluster
      '/usr/pgsql-16/bin/postgresql-16-setup initdb',
      'systemctl enable postgresql-16',
      'systemctl start postgresql-16',

      // Create DB and user
      `sudo -u postgres psql -c "CREATE USER famosi WITH PASSWORD 'famosi_local' CREATEDB;"`,
      `sudo -u postgres psql -c "CREATE DATABASE famosi OWNER famosi;"`,
      `sudo -u postgres psql -d famosi -c "CREATE EXTENSION IF NOT EXISTS vector;"`,

      // Allow local TCP connections (needed by asyncpg)
      `sed -i "s/#listen_addresses = 'localhost'/listen_addresses = 'localhost'/" /var/lib/pgsql/16/data/postgresql.conf`,
      // Replace peer auth with md5 for local connections
      `sed -i 's/^local   all             all                                     peer/local   all             all                                     md5/' /var/lib/pgsql/16/data/pg_hba.conf`,
      `echo "host    all             all             127.0.0.1/32            md5" >> /var/lib/pgsql/16/data/pg_hba.conf`,
      'systemctl restart postgresql-16',

      // ── CloudWatch agent ─────────────────────────────────────────────
      'dnf install -y amazon-cloudwatch-agent',
      `mkdir -p /var/log/famosi`,
      `cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json << 'CWEOF'
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/famosi/app.log",
            "log_group_name": "/famosi/app",
            "log_stream_name": "{instance_id}",
            "timestamp_format": "%Y-%m-%dT%H:%M:%S"
          }
        ]
      }
    }
  }
}
CWEOF`,
      '/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json',

      // ── Clone app ────────────────────────────────────────────────────
      'mkdir -p /opt/famosi',
      'git clone https://github.com/ahujahaymang/famosi.git /opt/famosi || (cd /opt/famosi && git pull)',
      'cd /opt/famosi && python3.11 -m pip install -r requirements.txt',

      // ── Fetch secrets from SSM ───────────────────────────────────────
      `TELEGRAM_BOT_TOKEN=$(aws ssm get-parameter --name ${ssmPrefix}/TELEGRAM_BOT_TOKEN --with-decryption --query Parameter.Value --output text)`,
      `OPENAI_API_KEY=$(aws ssm get-parameter --name ${ssmPrefix}/OPENAI_API_KEY --with-decryption --query Parameter.Value --output text)`,
      `ADMIN_TELEGRAM_USER_ID=$(aws ssm get-parameter --name ${ssmPrefix}/ADMIN_TELEGRAM_USER_ID --query Parameter.Value --output text 2>/dev/null || echo "0")`,

      // ── Write .env ───────────────────────────────────────────────────
      // DATABASE_URL uses asyncpg driver with local postgres
      `cat > /opt/famosi/.env << EOF
BOT_MODE=polling
TELEGRAM_BOT_TOKEN=\${TELEGRAM_BOT_TOKEN}
OPENAI_API_KEY=\${OPENAI_API_KEY}
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
DATABASE_URL=postgresql+asyncpg://famosi:famosi_local@localhost/famosi
REDIS_URL=
AWS_REGION=${this.region}
AWS_BEDROCK_REGION=${this.region}
S3_BUCKET_SUMMARIES=${summariesBucket.bucketName}
S3_BUCKET_BACKUPS=${backupsBucket.bucketName}
ADMIN_TELEGRAM_USER_ID=\${ADMIN_TELEGRAM_USER_ID}
CURRENT_POLICY_VERSION=1.0
LOG_LEVEL=INFO
EOF`,

      // ── Run Alembic migrations ───────────────────────────────────────
      'cd /opt/famosi && python3.11 -m alembic upgrade head',

      // ── Systemd service ──────────────────────────────────────────────
      `cat > /etc/systemd/system/famosi.service << 'EOF'
[Unit]
Description=Famosi Telegram Bot
After=network.target postgresql-16.service
Requires=postgresql-16.service

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
EOF`,
      'systemctl daemon-reload',
      'systemctl enable famosi',
      'systemctl start famosi',

      // ── Nightly backup cron ──────────────────────────────────────────
      // pg_dump → gzip → S3. Runs at 02:00 UTC daily.
      `cat > /etc/cron.d/famosi-backup << 'EOF'
0 2 * * * root PGPASSWORD=famosi_local /usr/pgsql-16/bin/pg_dump -U famosi -h localhost famosi | gzip | aws s3 cp - s3://${backupsBucket.bucketName}/$(date +\\%Y-\\%m-\\%d).sql.gz
EOF`,

      'echo "=== Famosi bootstrap complete ==="',
    );

    // ── EC2 instance (t3.micro — free tier eligible) ──────────────────────
    const instance = new ec2.Instance(this, 'App', {
      instanceName: `${prefix}-app`,
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      instanceType: ec2.InstanceType.of(
        ec2.InstanceClass.T3,
        ec2.InstanceSize.MICRO,
      ),
      machineImage: ec2.MachineImage.latestAmazonLinux2023({
        cpuType: ec2.AmazonLinuxCpuType.X86_64,
      }),
      securityGroup: appSg,
      role,
      userData,
      // Don't replace instance when user-data changes — secrets are
      // re-fetched at runtime via the deploy script instead.
      userDataCausesReplacement: false,
      blockDevices: [{
        deviceName: '/dev/xvda',
        volume: ec2.BlockDeviceVolume.ebs(20, {
          volumeType: ec2.EbsDeviceVolumeType.GP3,
          encrypted: true,
          deleteOnTermination: false, // keep data volume on instance replacement
        }),
      }],
    });

    // ── CloudWatch Log Group ──────────────────────────────────────────────
    const logGroup = new logs.LogGroup(this, 'AppLogGroup', {
      logGroupName: '/famosi/app',
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ── SNS alarm topic ───────────────────────────────────────────────────
    const alarmTopic = new sns.Topic(this, 'AlarmTopic', {
      topicName: `${prefix}-alarms`,
      displayName: 'Famosi Alarms',
    });
    if (alertEmail) {
      alarmTopic.addSubscription(
        new snsSubscriptions.EmailSubscription(alertEmail),
      );
    }

    // CPU alarm — fires when average > 80% for 5 minutes
    new cloudwatch.Alarm(this, 'CpuAlarm', {
      alarmName: `${prefix}-cpu-high`,
      alarmDescription: 'EC2 CPU > 80% for 5 min',
      metric: new cloudwatch.Metric({
        namespace: 'AWS/EC2',
        metricName: 'CPUUtilization',
        dimensionsMap: { InstanceId: instance.instanceId },
        period: cdk.Duration.minutes(5),
        statistic: 'Average',
      }),
      threshold: 80,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(new cloudwatchActions.SnsAction(alarmTopic));

    // Status check alarm — fires if instance or system check fails
    new cloudwatch.Alarm(this, 'StatusCheckAlarm', {
      alarmName: `${prefix}-status-check-failed`,
      alarmDescription: 'EC2 status check failed',
      metric: new cloudwatch.Metric({
        namespace: 'AWS/EC2',
        metricName: 'StatusCheckFailed',
        dimensionsMap: { InstanceId: instance.instanceId },
        period: cdk.Duration.minutes(5),
        statistic: 'Maximum',
      }),
      threshold: 1,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(new cloudwatchActions.SnsAction(alarmTopic));

    // Monthly budget alert
    new budgets.CfnBudget(this, 'MonthlyBudget', {
      budget: {
        budgetName: `${prefix}-monthly-budget`,
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: { amount: monthlyBudgetUsd, unit: 'USD' },
      },
      notificationsWithSubscribers: alertEmail ? [
        {
          notification: {
            notificationType: 'FORECASTED',
            comparisonOperator: 'GREATER_THAN',
            threshold: 80,
            thresholdType: 'PERCENTAGE',
          },
          subscribers: [{ subscriptionType: 'EMAIL', address: alertEmail }],
        },
      ] : [],
    });

    // ── Outputs ───────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'InstanceId', {
      value: instance.instanceId,
      description: 'Connect via: aws ssm start-session --target <id>',
    });
    new cdk.CfnOutput(this, 'PublicIp', {
      value: instance.instancePublicIp,
      description: 'Public IP address of the instance',
    });
    new cdk.CfnOutput(this, 'SummariesBucketName', {
      value: summariesBucket.bucketName,
    });
    new cdk.CfnOutput(this, 'BackupsBucketName', {
      value: backupsBucket.bucketName,
    });
    new cdk.CfnOutput(this, 'LogGroup', {
      value: logGroup.logGroupName,
      description: 'View app logs: aws logs tail /famosi/app --follow',
    });
    new cdk.CfnOutput(this, 'SsmSessionCommand', {
      value: `aws ssm start-session --target ${instance.instanceId}`,
      description: 'Connect to instance (no SSH key needed)',
    });
  }
}
