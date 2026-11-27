# OpenTelemetry traces and logs on the Amazon OpenSearch Service optimized engine

This sample shows how to run OpenTelemetry traces and logs, including AI agent traces, on an Amazon OpenSearch Service
domain that uses the Optimized engine. It covers the parts that are specific to the engine: index templates that map
OpenTelemetry attributes onto columnar storage without rejecting documents, an Amazon OpenSearch Ingestion pipeline
that prepares spans for PPL and for Agent Traces in OpenSearch UI, and a method to measure storage against a General
Purpose domain fed with the same data.

Use it to decide how to map your own OpenTelemetry fields on the Optimized engine, to run analytical PPL queries over
raw spans, and to see what the same telemetry costs to store on each engine. The OpenTelemetry Demo, with its LLM
shopping agent on Amazon Bedrock, is the traffic source; it is not the subject.

For dashboards, cost tracking, and online evaluation of an agent's answers, see the sibling sample
[agentic-observability-serverless](../agentic-observability-serverless/), which uses the same demo on an OpenSearch
Serverless time series collection.

Requires OpenSearch 3.7 or later; the commands below create the domain on the newest OpenSearch 3.x version available.
Validated in November 2026 on OpenSearch 3.7 with OpenTelemetry Demo 3.1.0 in us-east-1.

```
templates/   index templates for spans and logs; tuned/ adds index:false, a bloom filter and low_cardinality;
             general-purpose/ is the same mapping without multi_value, for the storage comparison
pipeline/    OpenSearch Ingestion pipeline; pipeline-storage-comparison.yaml adds a General Purpose sink to compare storage
iam/         pipeline role, EC2 host role, domain access policy, fine-grained access control role mapping
collector/   the OpenTelemetry Demo Collector override (SigV4 to the pipeline)
demo/        OpenTelemetry Demo overlay: LiteLLM proxy to Amazon Bedrock, weighted model split, EC2 user data
queries/     example PPL queries
scripts/     render.py (fill placeholders), install-templates.sh, check-ingestion.sh, setup_ui.py, cleanup.sh
tests/       offline tests (python -m pytest)
```

## Mapping OpenTelemetry attributes

The templates in `templates/` follow four rules. Fields you search for words in are `text` at the document root (the
pipeline copies the agent's prompt, answer, tool arguments, and tool result there from `attributes.gen_ai.*`). Fields
you aggregate or filter by time are numbers or dates (`durationInNanos`, token counts, `startTime`). Fields that can
hold a list are `multi_value: true`; lists of objects (`events`, `links`) are `nested`. Every other value inside an
attribute bag is a `keyword`, so a value that one service sends as a number and another as a string is accepted from
both. Two exceptions keep OpenSearch UI's Agent Traces page working: `attributes.derived.gen_ai.is_root` is `boolean`
and the token counts under `attributes.gen_ai.usage` are `long`.

Keep a field's type constant across the indices behind one pattern. On the Optimized engine, indices that disagree on a
type fail every query over the wildcard pattern, with nothing in the dead-letter queue; check with
`GET /otel-v2-apm-span-*/_mapping` before changing a template. To aggregate a field that is already a keyword, have the
pipeline copy it into a new numeric field. The pipeline also renames attributes that arrive as a scalar from one
instrumentation and as an object from another (`error`, `http`, `rpc.system`), and drops a log attribute that one
logger emits twice. See the
[mappings page](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/optimized-log-analytics-mappings.html).

## Prerequisites

- **Where to run the commands:** the simplest is [AWS CloudShell](https://docs.aws.amazon.com/cloudshell/latest/userguide/welcome.html)
  in your Region. It has a current AWS CLI, bash, Python 3, pip, jq and git; add `pip3 install --user awscurl`.
  Any Linux or macOS shell works too, and so does Git Bash on Windows.
- **Region:** any Region where the Optimized engine and Anthropic Claude Haiku 5.5 and Sonnet 5.5 on Amazon Bedrock are
  available. Set `MODEL_PREFIX` in `config.env` to the prefix of the Bedrock cross-Region inference profiles your
  Region offers: `us` (the default), `eu`, `apac`, `au`, `jp` or `global`. To list them:
  `aws bedrock list-inference-profiles --query "inferenceProfileSummaries[?contains(inferenceProfileId,'claude-haiku-5-5')].inferenceProfileId" --output text`
- **OpenSearch version:** the newest OpenSearch 3.x the service offers. 3.7 is the minimum.
- **Instance types:** the first type in each list that is offered in your Region and Availability Zone. The commands
  in steps 1 and 4 pick it for you.
  - Domain data nodes, OpenSearch Optimized Instances: `or2.large.search`, then `or1.large.search`.
  - Demo host (x86_64, 32 GB RAM): `m6i.2xlarge`, `m6a.2xlarge`, `m7i.2xlarge`, `m7a.2xlarge`.
- **Tools:** bash, `jq`, Python 3.10 or later (boto3 and botocore 1.44 require it), and `awscurl` (`python3 -m pip install awscurl`). `scripts/setup_ui.py` and
  the tests also need `python3 -m pip install -r requirements.txt`. On Windows, use `python` wherever this README says
  `python3`, and use Git Bash (`C:\Program Files\Git\bin\bash.exe`), not the WSL `bash` that may come first on PATH.
- **AWS CLI 2.35.14 or later.** `--engine-mode` and `--use-case` on `create-domain` are new in that version; with an
  older CLI, create the domain in the console, or with boto3 (`create_domain` accepts `EngineMode="OPTIMIZED"` and
  `UseCase="OBSERVABILITY"`), and run every other command as written. CloudShell has a current CLI.
  On Windows without admin rights, extract the installer to a user folder and put it first on PATH:
  `msiexec /a AWSCLIV2.msi /qn TARGETDIR=%USERPROFILE%\awscli-v2`, then use `%USERPROFILE%\awscli-v2\Amazon\AWSCLIV2\aws.exe`.
- **Permissions:** an IAM identity with rights on OpenSearch Service, OpenSearch Ingestion, IAM, S3, EC2, SSM,
  CloudWatch Logs, and Bedrock.
- **Bedrock model access** for the two models. Check each with your prefix (`us` here). The message is read from a
  file, so the same command works in bash and PowerShell:
  ```bash
  echo '[{"role":"user","content":[{"text":"hi"}]}]' > msg.json
  aws bedrock-runtime converse --model-id us.anthropic.claude-haiku-5-5 --messages file://msg.json --query 'output.message.content[0].text'
  aws bedrock-runtime converse --model-id us.anthropic.claude-sonnet-5-5 --messages file://msg.json --query 'output.message.content[0].text'
  ```
- **Cost:** about $1 an hour (about $25 a day) while it runs, at November 2026 list prices in us-east-1: two
  `or2.large.search` nodes at $0.20, one ingestion OCU at $0.24, one `m6i.2xlarge` at $0.384, and about $0.01 of
  storage. Any combination from the instance lists costs $1.00 to $1.15 an hour. Bedrock calls from the demo's load
  generator are extra. Run `scripts/cleanup.sh --delete` when you are done.
- **Time:** about an hour end to end; the AWS waits are about 15 minutes of it.

## Deploying with a coding agent

This README is written so a coding agent can run it. If you are an agent: check the prerequisites first, fixing or
working around anything the README allows, and resolve the instance types, the default VPC subnet and the OpenSearch
version with the commands in steps 1 and 4. Fill `config.env` (set `MODEL_PREFIX` for the Region) and run the name
check at the start of Deployment; if it lists anything, change the names and `PROJECT_TAG` in `config.env` until it
lists nothing, and say so in a note. Then send the person this message, filled in, and wait:

> Hi! I'll set up the **Amazon OpenSearch optimized engine sample** from AWS Samples. It shows one engine running analytics and text search together over your OpenTelemetry logs and traces, including traces from an AI agent.
>
> I've done read-only checks. Nothing is created yet.
>
> **Where:** account `<account>`, `<region>`, as `<identity>`
>
> **What I'll build:**
> - An EC2 instance (`<ec2 type>`, 60 GB) running the OpenTelemetry Demo shop. The shop's AI assistant calls Claude Haiku 5.5 and Sonnet 5.5 on Amazon Bedrock. The instance uses the default VPC's public subnet `<subnet>` to pull images and reach Bedrock. Nothing is open inbound.
> - An OpenSearch Ingestion pipeline that carries the shop's traces and logs to the domain.
> - An OpenSearch domain on the optimized engine: OpenSearch `<version>`, 2 x `<node type>`, 20 GB gp3 per node.
> - An OpenSearch UI application to explore the logs, traces, and agent traces.
>
> **Cost:** about $25 a day ($1 an hour) in us-east-1, plus Bedrock calls. Please use a test account.
> **Time:** about an hour.
> **Cleanup:** everything is tagged `project=<tag>`. Tell me when you're done, or run `scripts/cleanup.sh --delete`.
>
> **Before I start:**
> 1. Which IAM user or role do you sign in to the AWS console with? OpenSearch UI uses that sign-in, so I'll give it access.
> 2. Do you want a different account or Region? Say so now.
>
> **Good to go?** (yes / no)
>
> When it's running, I'll send you the OpenSearch UI link and show you where to find agent traces.

Send only this. Add a note only if something blocks you or needs a decision, at most two notes, one short line each.
Do not explain your reasoning or list what you checked. Do not create anything until the person replies.

When step 5 has run, confirm that the shop's AI assistant is sending spans before you say anything is working:
`source=otel-v2-apm-span-* | where serviceName = 'agent' | stats count()` (POST it to `/_plugins/_ppl` with
`awscurl --service es`) must return more than zero. If it is still zero 15 minutes after the containers start, say so
and check the containers on the instance instead. When it is above zero, send this message, filled in:

> Data's flowing: `<n>` services are sending traces and logs, and the shop's AI assistant is answering on Amazon Bedrock. Let's look at it in OpenSearch UI.
>
> 1. **Open the UI.** Sign in to the AWS console as `<console identity>`, then open `<UI_ENDPOINT>`. You can also find it under Amazon OpenSearch Service → OpenSearch UI → Applications → `<name>-ui`.
> 2. **Pick the workspace.** Choose **Astronomy Shop**. I've created it, attached the `<domain>` domain, and given you edit access.
> 3. **Add two datasets.** In the workspace, open Data → Datasets → Create dataset, choose the `<domain>` data source, and add:
>
>    | Index pattern | Time field | Signal type |
>    |---|---|---|
>    | `otel-v2-apm-span-*` | `startTime` | Traces |
>    | `logs-otel-v1-*` | `time` | Logs |
>
>    The time field is what lets OpenSearch UI plot each dataset over time, so pick exactly these.
> 4. **Check the data.** In Discover, choose the traces dataset and run this PPL query: `source=otel-v2-apm-span-* | stats count() by serviceName`. You'll see one row per service.
> 5. **Explore.**
>    - **Discover → Traces:** raw spans from every service.
>    - **Agent Monitoring → Traces:** one row per agent run, with its latency and tokens. Open one to see the model and tool calls.
>    - **Ask AI** (top right, already on): try "slowest operations in the last hour".
>    - **More queries:** `queries/blog-queries.ppl` has the PPL from the post.
>
> When you're done, say "clean up" and I'll remove everything, or run `scripts/cleanup.sh --delete`.

Then list every step where this README was unclear or wrong.

## Deployment

Fill `config.env` and render once; every command below uses the files in `rendered/`. Two values (`DOMAIN_ENDPOINT`,
`INGEST_ENDPOINT`) are only known after steps 1 and 3, so you render again after each. Every resource is created with
the tag `project=$PROJECT_TAG`; `scripts/cleanup.sh` deletes only resources that carry it.

```bash
cp config.env.example config.env   # edit
PY=python3; "$PY" -c "" >/dev/null 2>&1 || PY=python   # python3 on Linux and macOS, python on Windows
$PY scripts/render.py
set -a; source config.env; set +a; export AWS_DEFAULT_REGION=$REGION   # the aws CLI does not read config.env
export MSYS_NO_PATHCONV=1   # Git Bash only: keep arguments such as /aws/vendedlogs/... as they are
for t in aws jq python awscurl; do command -v $t >/dev/null || echo "missing: $t"; done
TAG="Key=project,Value=$PROJECT_TAG"
```

Before you create anything, check that the names in `config.env` are new in this account. Run the check in bash
(Git Bash on Windows), in the same shell as the block above. `scripts/cleanup.sh` deletes
the resources named in `config.env` that carry `project=$PROJECT_TAG`, so a second deployment with the same names and
tag would be deleted with this one, and step 2 fails on names that exist. The check is read-only; if it prints
anything, change the names and `PROJECT_TAG` in `config.env`, render again, and repeat until it prints nothing:

```bash
aws s3api head-bucket --bucket $DLQ_BUCKET >/dev/null 2>&1 && echo "exists: bucket $DLQ_BUCKET"
aws s3api head-bucket --bucket $CONFIG_BUCKET >/dev/null 2>&1 && echo "exists: bucket $CONFIG_BUCKET"
aws iam get-role --role-name $PIPELINE_ROLE >/dev/null 2>&1 && echo "exists: role $PIPELINE_ROLE"
aws iam get-role --role-name $HOST_ROLE >/dev/null 2>&1 && echo "exists: role $HOST_ROLE"
aws iam get-instance-profile --instance-profile-name $HOST_ROLE >/dev/null 2>&1 && echo "exists: instance profile $HOST_ROLE"
aws opensearch describe-domain --domain-name $DOMAIN_NAME >/dev/null 2>&1 && echo "exists: domain $DOMAIN_NAME"
aws osis get-pipeline --pipeline-name $PIPELINE_NAME >/dev/null 2>&1 && echo "exists: pipeline $PIPELINE_NAME"
aws logs describe-log-groups --log-group-name-prefix /aws/vendedlogs/OpenSearchIngestion/$PIPELINE_NAME \
  --query "logGroups[?logGroupName=='/aws/vendedlogs/OpenSearchIngestion/$PIPELINE_NAME'].logGroupName" --output text \
  | tr -d '\r' | grep -q . && echo "exists: log group /aws/vendedlogs/OpenSearchIngestion/$PIPELINE_NAME"
aws opensearch list-applications --query "ApplicationSummaries[?name=='$DOMAIN_NAME-ui'].id" --output text \
  | tr -d '\r' | grep -q . && echo "exists: OpenSearch UI application $DOMAIN_NAME-ui"
```

The sample needs its own, new OpenSearch UI application (`$DOMAIN_NAME-ui`). `scripts/setup_ui.py` adds admins to an
existing application of that name only if it carries `project=$PROJECT_TAG` and has this domain as its data source;
otherwise it stops without changing anything.

1. **Domain.** In the console: Create domain, Standard create, use case Observability, engine mode Optimized,
   the newest OpenSearch version offered (3.7 or later), two data nodes of the first type offered from the list in
   Prerequisites (`or2.large.search`, then `or1.large.search`), 20 GB gp3 per node,
   fine-grained access control with your IAM identity as master user, and the tag `project=$PROJECT_TAG`. Or from the CLI:
   ```bash
   VERSION=$(aws opensearch list-versions --query "Versions[?starts_with(@, 'OpenSearch_3.')] | [0]" --output text | tr -d '\r')   # newest first
   for t in or2.large.search or1.large.search; do   # first OpenSearch Optimized Instance type offered for this version
     aws opensearch describe-instance-type-limits --instance-type $t --engine-version $VERSION >/dev/null 2>&1 && OS_TYPE=$t && break
   done; echo "$VERSION on $OS_TYPE"
   aws opensearch create-domain --domain-name $DOMAIN_NAME --engine-version $VERSION \
     --engine-mode OPTIMIZED --use-case OBSERVABILITY \
     --cluster-config "InstanceType=$OS_TYPE,InstanceCount=2,ZoneAwarenessEnabled=true,ZoneAwarenessConfig={AvailabilityZoneCount=2}" \
     --ebs-options "EBSEnabled=true,VolumeType=gp3,VolumeSize=20" \
     --encryption-at-rest-options Enabled=true --node-to-node-encryption-options Enabled=true \
     --domain-endpoint-options EnforceHTTPS=true \
     --access-policies "$(jq -c . rendered/iam/domain-access-policy.json)" \
     --advanced-security-options "Enabled=true,InternalUserDatabaseEnabled=false,MasterUserOptions={MasterUserARN=$ADMIN_ARN}" \
     --tag-list $TAG
   ```
   The access policy must be one line; the API rejects the pretty-printed file. Engine mode cannot be changed after
   creation. The domain takes 10 to 15 minutes. Put its endpoint in `config.env` as `DOMAIN_ENDPOINT` and render again.
2. **Buckets, roles, log group.**
   ```bash
   aws s3 mb s3://$DLQ_BUCKET && aws s3api put-bucket-tagging --bucket $DLQ_BUCKET --tagging "TagSet=[{$TAG}]"
   aws s3 mb s3://$CONFIG_BUCKET && aws s3api put-bucket-tagging --bucket $CONFIG_BUCKET --tagging "TagSet=[{$TAG}]"
   aws iam create-role --role-name $PIPELINE_ROLE --assume-role-policy-document file://rendered/iam/pipeline-role-trust.json --tags $TAG
   aws iam put-role-policy --role-name $PIPELINE_ROLE --policy-name sink --policy-document file://rendered/iam/pipeline-role-policy.json
   aws iam create-role --role-name $HOST_ROLE --assume-role-policy-document file://rendered/iam/host-role-trust.json --tags $TAG
   aws iam put-role-policy --role-name $HOST_ROLE --policy-name demo --policy-document file://rendered/iam/host-role-policy.json
   aws iam attach-role-policy --role-name $HOST_ROLE --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore   # to read the instance log over SSM
   aws iam create-instance-profile --instance-profile-name $HOST_ROLE --tags $TAG
   aws iam add-role-to-instance-profile --instance-profile-name $HOST_ROLE --role-name $HOST_ROLE
   aws logs create-log-group --log-group-name /aws/vendedlogs/OpenSearchIngestion/$PIPELINE_NAME --tags project=$PROJECT_TAG   # must exist before create-pipeline
   ```
3. **Templates and pipeline.**
   ```bash
   scripts/install-templates.sh rendered/templates $DOMAIN_ENDPOINT $REGION
   aws osis create-pipeline --pipeline-name $PIPELINE_NAME --min-units 1 --max-units 4 \
     --pipeline-configuration-body file://rendered/pipeline/pipeline.yaml \
     --log-publishing-options "IsLoggingEnabled=true,CloudWatchLogDestination={LogGroup=/aws/vendedlogs/OpenSearchIngestion/$PIPELINE_NAME}" \
     --tags $TAG
   ```
   Map the pipeline role into `all_access` **additively**. The file lists your `ADMIN_ARN` in both `backend_roles`
   (matches an IAM role) and `users` (matches an IAM user), so you stay in; a PUT with only the pipeline role locks
   you out. Step 5 adds the identity you sign in to the
   AWS console with.
   ```bash
   awscurl --service es --region $REGION -X PUT "https://$DOMAIN_ENDPOINT/_plugins/_security/api/rolesmapping/all_access" \
     -H "Content-Type: application/json" -d @rendered/iam/all-access-rolesmapping.json
   ```
   When the pipeline is ACTIVE (2 to 3 minutes), put its ingest endpoint in `config.env` and render again:
   ```bash
   aws osis get-pipeline --pipeline-name $PIPELINE_NAME --query 'Pipeline.IngestEndpointUrls[0]' --output text
   ```
4. **Demo application.** Pack the overlay, then launch an EC2 instance with `rendered/demo/user-data.sh` as user data.
   ```bash
   rm -rf overlay && mkdir overlay && cp -r rendered/demo/. overlay/ && cp rendered/collector/otelcol-config-extras.yml overlay/ \
     && tar czf demo-overlay.tgz -C overlay . && aws s3 cp demo-overlay.tgz s3://$CONFIG_BUCKET/
   ```
   Instance: Amazon Linux 2023, **x86_64** (the compose binary in user data is amd64), the first type offered from the
   list in Prerequisites, root volume 60 GB (the demo pulls about 30 images), instance profile `$HOST_ROLE`, IMDSv2 with
   hop limit 2 so containers can use the instance role. It needs outbound internet (a public IP in a default-VPC
   subnet is enough) and nothing inbound:
   ```bash
   VPC=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text | tr -d '\r')
   read SUBNET AZ < <(aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC Name=default-for-az,Values=true \
     --query 'Subnets[0].[SubnetId,AvailabilityZone]' --output text | tr -d '\r')
   OFFERED=$(aws ec2 describe-instance-type-offerings --location-type availability-zone --filters Name=location,Values=$AZ \
     Name=instance-type,Values=m6i.2xlarge,m6a.2xlarge,m7i.2xlarge,m7a.2xlarge --query 'InstanceTypeOfferings[].InstanceType' --output text | tr -d '\r')
   for t in m6i.2xlarge m6a.2xlarge m7i.2xlarge m7a.2xlarge; do echo "$OFFERED" | grep -qw "$t" && EC2_TYPE=$t && break; done
   echo "$EC2_TYPE in $SUBNET ($AZ)"
   SG_ID=$(aws ec2 create-security-group --group-name $PIPELINE_NAME-host --description "demo host, no inbound" --vpc-id $VPC \
     --tag-specifications "ResourceType=security-group,Tags=[{$TAG}]" --query GroupId --output text | tr -d '\r')
   AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text | tr -d '\r')
   aws ec2 run-instances --instance-type $EC2_TYPE --count 1 --subnet-id $SUBNET --associate-public-ip-address --security-group-ids $SG_ID \
     --image-id $AMI \
     --iam-instance-profile Name=$HOST_ROLE --user-data file://rendered/demo/user-data.sh \
     --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=60,VolumeType=gp3,Encrypted=true}' \
     --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2 \
     --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$PIPELINE_NAME},{$TAG}]" "ResourceType=volume,Tags=[{$TAG}]"
   ```
   If `run-instances` returns `InsufficientInstanceCapacity`, use the next type in the list, or another default subnet
   (`Subnets[1]` in the `describe-subnets` query). Progress is written to
   `/var/log/otel-demo-userdata.log` on the instance. The SSM agent registers about two minutes after launch; then:
   ```bash
   aws ssm send-command --instance-ids <id> --document-name AWS-RunShellScript --parameters 'commands=["tail -n 30 /var/log/otel-demo-userdata.log"]'
   aws ssm get-command-invocation --command-id <command-id> --instance-id <id> --query StandardOutputContent --output text
   ```
   Within a few minutes of the containers starting, `otel-v2-apm-span-*` and `logs-otel-v1-*` exist (the CloudWatch
   `documentsSuccess` metric lags the index count by a few minutes):
   ```bash
   scripts/check-ingestion.sh $DOMAIN_ENDPOINT $REGION $PIPELINE_NAME $DLQ_BUCKET
   ```
5. **OpenSearch UI.** One script adds the identity you sign in to the AWS console with to the domain's `all_access`
   mapping, creates the OpenSearch UI application with that identity as admin, turns on Ask AI, and creates an
   Observability workspace named **Astronomy Shop** with the domain attached:
   ```bash
   $PY scripts/setup_ui.py --admins <console identity>
   ```
   `<console identity>` is the ARN you sign in to the console with: the user ARN for an IAM user, the session ARN
   (`arn:aws:sts::<account>:assumed-role/<role>/<session>`) for a role. The script also makes the identity running it
   an admin of the application, which it needs to attach the domain to the workspace. If it prints "data source not
   ready yet", run it again after a few minutes. It prints `UI_ENDPOINT`. Open that URL signed
   in to the console, choose the workspace, and create two datasets under Data → Datasets (the UI fills in their
   field lists, the API does not):

   | Index pattern | Time field | Signal type |
   |---|---|---|
   | `otel-v2-apm-span-*` | `startTime` | Traces |
   | `logs-otel-v1-*` | `time` | Logs |

   Then open Agent Monitoring → Traces. It lists a trace when its root span satisfies
   `parentSpanId = "" AND isnotnull(attributes.gen_ai.operation.name)`; without a Traces dataset on
   `otel-v2-apm-span-*` the page shows "Start tracing your AI agents".

## Operating notes

- **Pipeline ACTIVE does not mean ingesting.** Read the pipeline's CloudWatch log group before debugging the Collector.
- **401 `Missing Authentication Token` lines in that log group are not the Collector.** The ingest endpoint is public
  and unsigned probes log as ERROR. Collector failures show as `documentErrors` or in `docker logs otel-collector`.
- **The exporter URL must include the pipeline-name segment** declared in the pipeline's `*_path`, or the Collector
  gets 404 and drops the batch.
- **Put a CloudWatch alarm on the pipeline's `documentErrors` metric** on day one. A rejected document is silent otherwise.
- **`_count` and `_search` are not available** on an Optimized domain. Use `_cat/indices` or PPL `stats count()`.
- **Re-running the demo by hand over SSM** needs a detached unit (`systemd-run --unit=otel-demo-start make start-agentic ...`);
  SSM ends the process group when the command returns and the image pull stops with it.

## Storage comparison

`pipeline/pipeline-storage-comparison.yaml` writes every document to a second, General Purpose domain with the same
mapping. Install it from `templates/general-purpose/`, which is `templates/` without `multi_value` (an Optimized-engine
parameter that General Purpose rejects; General Purpose accepts lists in any field):
`scripts/install-templates.sh rendered/templates/general-purpose $GP_DOMAIN_ENDPOINT $REGION`.
After a day, force-merge both and compare `pri.store.size` from `_cat/indices`. Count spans on the
Optimized domain: on General Purpose, nested span events count as extra Lucene documents.

## Tests

```bash
$PY -m pip install -r requirements.txt
$PY -m pytest tests/
```

The tests run offline. They check that `render.py` fills every placeholder and fails on a missing one, that the
templates and pipeline parse and follow the mapping rules above, that every sink has a dead-letter queue, and that no
tracked file contains an account id, endpoint, or key.

## Cleanup

`scripts/cleanup.sh` lists every resource named in `config.env` that carries the tag `project=$PROJECT_TAG` and asks
for confirmation before deleting, in dependency order: the instance and its security group, the OpenSearch UI
application, the pipeline, the domain, the two buckets (only if the sample created them, after emptying them), the
log group, the instance profile and the two roles. Resources with the right name but without the tag are listed and
skipped.

```bash
scripts/cleanup.sh            # list only
scripts/cleanup.sh --delete   # list, confirm, delete
```

## Security

See [THREAT_MODEL.md](THREAT_MODEL.md).
