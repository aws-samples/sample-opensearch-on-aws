# Recommended CloudWatch Alarms for Amazon OpenSearch Service

Deploy [AWS-recommended CloudWatch alarms](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/cloudwatch-alarms.html) for your Amazon OpenSearch Service domain in minutes — with a single command or through the AWS Console.

**No IAM roles. No Lambda functions. No custom resources.**
The template creates only CloudWatch alarms and an SNS topic — nothing else.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Parameters](#parameters)
- [What Gets Deployed](#what-gets-deployed)
- [Customizing This Template](#customizing-this-template)
- [Updating After Domain Changes](#updating-after-domain-changes)
- [Cleanup](#cleanup)
- [FAQ](#faq)
- [Related Resources](#related-resources)

---

## Prerequisites

### Requirements

| Requirement | Details |
|-------------|---------|
| AWS CLI v2 | [Install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| python3 | Used by the deploy script for JSON parsing |
| AWS credentials | Configured via `aws configure`, environment variables, or SSO |
| AWS region | Set via `aws configure`, `--region` flag, or `AWS_DEFAULT_REGION` env var |

### Region selection

The deploy script determines which region to use in this order of precedence:

1. `--region` flag passed to the script (highest priority)
2. `AWS_DEFAULT_REGION` environment variable
3. Default region in your AWS CLI config (`~/.aws/config`)

If no region is configured, the script exits with an error and instructions.

```bash
# Option 1: Pass region directly
./deploy.sh --region us-west-2

# Option 2: Set environment variable
export AWS_DEFAULT_REGION=us-west-2
./deploy.sh

# Option 3: Set in CLI config (persists across sessions)
aws configure set region us-west-2
./deploy.sh
```

### AWS profile support

If you use named profiles (e.g., for multiple accounts), pass `--profile`:

```bash
./deploy.sh --profile production
./deploy.sh --profile staging --region eu-west-1
```

### IAM permissions

The script displays your AWS account, region, and identity at startup so you can verify you're targeting the right environment before any changes are made.

**Minimum permissions for `deploy.sh`:**

| Permission | Why |
|------------|-----|
| `sts:GetCallerIdentity` | Verify credentials and display account context |
| `es:ListDomainNames` | List available OpenSearch domains |
| `es:DescribeDomain` | Detect instance type, node count, and EBS volume |
| `ec2:DescribeInstanceTypes` | Look up instance memory for threshold calculation |
| `cloudformation:CreateStack` | Create the alarm stack |
| `cloudformation:UpdateStack` | Update an existing stack on re-run |
| `cloudformation:DescribeStacks` | Detect if stack already exists |
| `cloudwatch:PutMetricAlarm` | Create/update alarms (used by CloudFormation) |
| `cloudwatch:DeleteAlarms` | For stack updates/deletes (used by CloudFormation) |
| `sns:CreateTopic` | Create the notification topic (used by CloudFormation) |
| `sns:Subscribe` | Add your email as subscriber (used by CloudFormation) |
| `sns:DeleteTopic` | For stack deletes (used by CloudFormation) |

**If deploying via AWS CLI or Console directly** (without `deploy.sh`), you only need the CloudFormation, CloudWatch, and SNS permissions.

<details>
<summary>Example IAM policy (click to expand)</summary>

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DeployScriptReadPermissions",
      "Effect": "Allow",
      "Action": [
        "sts:GetCallerIdentity",
        "es:ListDomainNames",
        "es:DescribeDomain",
        "ec2:DescribeInstanceTypes",
        "cloudformation:DescribeStacks"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudFormationDeployPermissions",
      "Effect": "Allow",
      "Action": [
        "cloudformation:CreateStack",
        "cloudformation:UpdateStack",
        "cloudformation:DeleteStack"
      ],
      "Resource": "arn:aws:cloudformation:*:*:stack/opensearch-alarms-*/*"
    },
    {
      "Sid": "AlarmAndNotificationPermissions",
      "Effect": "Allow",
      "Action": [
        "cloudwatch:PutMetricAlarm",
        "cloudwatch:DeleteAlarms",
        "cloudwatch:DescribeAlarms",
        "sns:CreateTopic",
        "sns:DeleteTopic",
        "sns:Subscribe",
        "sns:Unsubscribe"
      ],
      "Resource": "*"
    }
  ]
}
```

</details>

---

## Quick Start

### Fastest: Use the interactive script

```bash
chmod +x deploy.sh   # first time only
./deploy.sh
```

The script will:

1. Validate prerequisites (AWS CLI, python3, credentials, region)
2. Display your AWS account, region, and identity for confirmation
3. List your OpenSearch domains — pick one
4. Ask for your notification email
5. Auto-detect instance type, node count, EBS volume, and memory
6. Compute the correct shards and storage thresholds
7. Deploy (or update) the CloudFormation stack

### Alternative: AWS CLI (manual)

```bash
aws cloudformation create-stack \
  --stack-name opensearch-alarms-my-domain \
  --template-body file://OpenSearch_cloudwatch_alarms.yaml \
  --parameters \
    ParameterKey=OpenSearchDomainName,ParameterValue=my-domain \
    ParameterKey=Email,ParameterValue=alerts@example.com \
    ParameterKey=NumberOfDataNodes,ParameterValue=3 \
    ParameterKey=ShardsThreshold,ParameterValue=2400 \
    ParameterKey=FreeStorageSpaceWarningThreshold,ParameterValue=25600 \
    ParameterKey=FreeStorageSpaceCriticalThreshold,ParameterValue=10240 \
    ParameterKey=IsOldGenDataNodes,ParameterValue=false \
    ParameterKey=IsOldGenMasterNodes,ParameterValue=false
```

### Alternative: AWS Console

1. Open the [CloudFormation Console](https://console.aws.amazon.com/cloudformation/home#/stacks/new)
2. Upload `OpenSearch_cloudwatch_alarms.yaml`
3. Fill in the parameters and deploy

### After deploying

**Check your inbox** — you'll receive an SNS confirmation email. You must click **Confirm subscription** to start receiving alarm notifications.

---

## Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `OpenSearchDomainName` | Your OpenSearch domain name | *(required)* |
| `Email` | Where to send alarm notifications | *(required)* |
| `NumberOfDataNodes` | Number of data nodes in your cluster | `3` |
| `ShardsThreshold` | Max active shards (see calculator below) | `2400` |
| `FreeStorageSpaceWarningThreshold` | WARNING: min(25% of disk, 25 GiB) in MiB — early alert to plan action | `20480` |
| `FreeStorageSpaceCriticalThreshold` | CRITICAL: min(20% of disk, 20 GiB) in MiB — act immediately | `10240` |
| `HasDedicatedMasterNodes` | `true` if domain has dedicated master nodes; `false` skips master alarms | `true` |
| `IsOldGenDataNodes` | `true` if data nodes are m3, m4, r3, r4, c4, i2, or t2 | `false` |
| `IsOldGenMasterNodes` | `true` if master nodes are m3, m4, r3, r4, c4, i2, or t2 | `false` |

### Shards threshold calculator

**Formula:** `25 × JVM heap (GiB) × number of data nodes`

JVM heap = half of your instance's RAM, capped at 32 GiB.

| Your instance type | RAM | JVM Heap | 3 Nodes | 6 Nodes | 10 Nodes |
|--------------------|-----|----------|---------|---------|----------|
| t3.medium.search | 4 GiB | 2 GiB | **150** | 300 | 500 |
| m6g.large.search | 8 GiB | 4 GiB | **300** | 600 | 1,000 |
| m6g.xlarge.search | 16 GiB | 8 GiB | **600** | 1,200 | 2,000 |
| r6g.large.search | 16 GiB | 8 GiB | **600** | 1,200 | 2,000 |
| r6g.xlarge.search | 32 GiB | 16 GiB | **1,200** | 2,400 | 4,000 |
| r6g.2xlarge.search | 64 GiB | 32 GiB | **2,400** | 4,800 | 8,000 |
| r6g.4xlarge.search | 128 GiB | 32 GiB | **2,400** | 4,800 | 8,000 |
| i3.2xlarge.search | 61 GiB | 30 GiB | **2,250** | 4,500 | 7,500 |

### Free storage space thresholds

This template uses a **two-tier approach** with the formula `min(percentage, cap)`:

| Tier | Formula | Cap | Purpose |
|------|---------|-----|---------|
| **Warning** | min(25% of disk, 25 GiB) | 25 GiB | Early alert — time to plan |
| **Critical** | min(20% of disk, 20 GiB) | 20 GiB | Immediate action needed |

**How it works:** On small disks the percentage applies (proportional to your capacity). On large disks the cap prevents the alarm from being too generous.

| EBS Volume | Warning | Critical |
|------------|---------|----------|
| 10 GiB | 2.5 GiB (25%) | 2 GiB (20%) |
| 50 GiB | 12.5 GiB (25%) | 10 GiB (20%) |
| 100 GiB | 25 GiB (capped) | 20 GiB (capped) |
| 500 GiB | 25 GiB (capped) | 20 GiB (capped) |
| 1,000 GiB | 25 GiB (capped) | 20 GiB (capped) |

> **Why two thresholds?** A single threshold either fires too late on small disks or gets ignored on large disks. The two-tier approach ensures you always get an early heads-up (warning) and a safety net (critical) proportional to your actual storage capacity.

> **Tip:** Don't want to calculate? Just use `./deploy.sh` — it computes both thresholds automatically from your domain's actual EBS volume size.

---

## What Gets Deployed

23 CloudWatch alarms covering cluster health, performance, and stability (19 always deployed + 4 master node alarms deployed only if dedicated master nodes are present):

| Alarm | Trigger | Issue |
|-------|---------|-------|
| ClusterStatus.red | maximum >= 1 for 1 min, 1 time | At least one primary shard and its replicas are not allocated to a node. |
| ClusterStatus.yellow | maximum >= 1 for 1 min, 5 times | At least one replica shard is not allocated to a node. |
| FreeStorageSpace (warning) | minimum <= warning threshold for 1 min, 1 time | Early warning — a node is running low on storage. Plan capacity action. |
| FreeStorageSpace (critical) | minimum <= critical threshold for 1 min, 1 time | Immediate action required — storage dangerously low, writes may be blocked. |
| ClusterIndexWritesBlocked | >= 1 for 5 min, 1 time | Your cluster is blocking write requests. |
| Nodes | minimum < node count for 1 day, 1 time | At least one node has been unreachable within one day. |
| AutomatedSnapshotFailure | maximum >= 1 for 1 min, 1 time | An automated snapshot failed (often caused by red cluster status). |
| CPUUtilization | maximum >= 80% for 15 min, 3 times | Sustained high CPU utilization on data nodes. |
| JVMMemoryPressure | maximum >= 95% (or 80% old-gen) for 1 min, 3 times | JVM memory pressure on data nodes is high. |
| OldGenJVMMemoryPressure | maximum >= 80% for 1 min, 3 times | Old generation JVM memory pressure (more accurate for current-gen instances). |
| MasterCPUUtilization | maximum >= 50% for 15 min, 3 times | Dedicated master node CPU is high. |
| MasterJVMMemoryPressure | maximum >= 95% (or 80% old-gen) for 1 min, 3 times | JVM memory pressure on master nodes is high. |
| MasterOldGenJVMMemoryPressure | maximum >= 80% for 1 min, 3 times | Old generation JVM pressure on master nodes. |
| KMSKeyError | >= 1 for 1 min, 1 time | KMS encryption key has been disabled. |
| KMSKeyInaccessible | >= 1 for 1 min, 1 time | KMS key has been deleted or grants revoked (domain unrecoverable). |
| Shards.active | >= threshold for 1 min, 1 time | Active shards exceed recommended limit (25 per GiB heap per node). |
| 5xx Error Rate | >= 10% of OpenSearchRequests (when requests > 10/min) | Data nodes may be overloaded or requests timing out. |
| MasterReachableFromNode | maximum < 1 for 5 min, 1 time | Master node stopped or is unreachable. |
| ThreadpoolWriteQueue | average >= 100 for 1 min, 1 time | High indexing concurrency. |
| ThreadpoolSearchQueue (avg) | average >= 500 for 1 min, 1 time | High search concurrency. |
| ThreadpoolSearchQueue (max) | maximum >= 5000 for 1 min, 1 time | Search queue spike to critical levels. |
| ThreadpoolWriteRejected | DIFF(SUM) >= 1 for 1 min, 1 time | Write requests are being rejected. |
| ThreadpoolSearchRejected | DIFF(SUM) >= 1 for 1 min, 1 time | Search requests are being rejected. |

> **Note:** The `KMSKeyError` and `KMSKeyInaccessible` alarms will show "Insufficient Data" — this is expected. These metrics only appear when your domain encounters a KMS key problem.

> **Note:** The 5xx Error Rate alarm uses the `OpenSearchRequests` metric, which applies to domains running OpenSearch engine (1.x, 2.x). If your domain runs a legacy Elasticsearch engine (5.x, 6.x, 7.x), change `OpenSearchRequests` to `ElasticsearchRequests` in the template.

> **Note:** The 5xx alarm includes a low-traffic guard (`IF(m2 > 10, ...)`). On idle or low-traffic domains, a single internal request can produce a 100% error rate, causing alarm flapping. The guard only evaluates the error rate when there are more than 10 requests per minute.

---

## Customizing This Template

This template is a **starting point** — fork it and adapt to your needs.

### Adjust thresholds

Every environment is different. Edit `Threshold`, `Period`, or `EvaluationPeriods` in the YAML:

```yaml
# Example: Make CPU alarm less sensitive (trigger at 90% instead of 80%)
CPUUtilization:
  Properties:
    Threshold: 90
    EvaluationPeriods: 5
```

### Add alarms for UltraWarm or other features

The [full list of OpenSearch CloudWatch metrics](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/managedomains-cloudwatchmetrics.html) includes many more options. The AWS docs also list additional alarms to consider:

- `WarmCPUUtilization` / `WarmJVMMemoryPressure` — if you use UltraWarm storage
- `WarmFreeStorageSpace` — alert at 10% of warm storage remaining
- `HotToWarmMigrationQueueSize` / `WarmToColdMigrationQueueSize` — migration queue monitoring
- `AlertingDegraded` — if you use the alerting plugin
- `ADPluginUnhealthy` — anomaly detection plugin health
- `SQLUnhealthy` — SQL plugin health

### Change notification targets

Replace email with Slack, PagerDuty, or a Lambda:

```yaml
AlarmActions:
  - arn:aws:sns:us-east-1:123456789012:my-pagerduty-topic
```

### Deploy for multiple domains

Run the stack once per domain — each gets its own stack:

```bash
./deploy.sh  # Select domain-1, deploys as opensearch-alarms-domain-1
./deploy.sh  # Select domain-2, deploys as opensearch-alarms-domain-2
```

---

## Updating After Domain Changes

If you change your instance type, node count, or EBS volume size, re-run the script:

```bash
./deploy.sh
```

It detects the existing stack and updates it with new thresholds automatically.

Or update manually via CLI:

```bash
aws cloudformation update-stack \
  --stack-name opensearch-alarms-my-domain \
  --template-body file://OpenSearch_cloudwatch_alarms.yaml \
  --parameters \
    ParameterKey=OpenSearchDomainName,UsePreviousValue=true \
    ParameterKey=Email,UsePreviousValue=true \
    ParameterKey=NumberOfDataNodes,ParameterValue=6 \
    ParameterKey=ShardsThreshold,ParameterValue=4800 \
    ParameterKey=FreeStorageSpaceWarningThreshold,ParameterValue=51200 \
    ParameterKey=FreeStorageSpaceCriticalThreshold,UsePreviousValue=true \
    ParameterKey=IsOldGenDataNodes,UsePreviousValue=true \
    ParameterKey=IsOldGenMasterNodes,UsePreviousValue=true
```

---

## Cleanup

To remove all alarms and the SNS topic:

```bash
aws cloudformation delete-stack --stack-name opensearch-alarms-my-domain
```

This deletes only the CloudWatch alarms and SNS topic — your OpenSearch domain is not affected.

---

## FAQ

**Q: Will this affect my OpenSearch domain?**
A: No. This template only creates CloudWatch alarms and an SNS topic. It does not modify your domain.

**Q: Why are KMS alarms showing "Insufficient Data"?**
A: This is expected. These metrics only appear if your domain encounters a KMS key problem.

**Q: Can I deploy this for multiple domains?**
A: Yes. Deploy one stack per domain. Each stack is named `opensearch-alarms-{domain-name}`.

**Q: What if I resize my cluster?**
A: Re-run `./deploy.sh` — it detects the existing stack and updates it with new thresholds.

**Q: How much does this cost?**
A: CloudWatch charges per alarm per month (~$0.10/alarm). This stack creates 23 alarms ≈ $2.30/month per domain. See [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/).

**Q: What's the difference between JVMMemoryPressure and OldGenJVMMemoryPressure?**
A: `OldGenJVMMemoryPressure` provides a more accurate picture of heap usage for current-generation instance types. Both are included for comprehensive coverage.

**Q: Why are there two FreeStorageSpace alarms?**
A: A single percentage-based threshold can be too generous on large volumes (e.g., 256 GiB free on a 1 TiB volume) — teams may ignore it. The two-tier approach gives an early warning to plan, plus a critical floor that fires before writes get blocked regardless of volume size.

**Q: The script says "No OpenSearch domains found" — what do I do?**
A: Check that you're targeting the correct region (`--region`) and account (`--profile`). The script only finds domains in the region it's configured for.

---

## Related Resources

- [Recommended CloudWatch alarms for Amazon OpenSearch Service](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/cloudwatch-alarms.html) — AWS documentation
- [Monitoring OpenSearch cluster metrics with Amazon CloudWatch](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/managedomains-cloudwatchmetrics.html) — full metrics reference
- [Troubleshooting Amazon OpenSearch Service](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/handling-errors.html) — what to do when alarms fire
- [Best practices for Amazon OpenSearch Service](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/bp.html) — sizing, sharding, and architecture guidance
