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
templates/   index templates for spans and logs; tuned/ adds index:false, a bloom filter and low_cardinality
pipeline/    OpenSearch Ingestion pipeline; pipeline-storage-comparison.yaml adds a General Purpose sink to compare storage
iam/         pipeline role, EC2 host role, domain access policy, fine-grained access control role mapping
collector/   the OpenTelemetry Demo Collector override (SigV4 to the pipeline)
demo/        OpenTelemetry Demo overlay: LiteLLM proxy to Amazon Bedrock, weighted model split, EC2 user data
queries/     example PPL queries
scripts/     render.py (fill placeholders), install-templates.sh, check-ingestion.sh, cleanup.sh
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

- **Region:** any Region where the Optimized engine and Anthropic Claude Haiku 5.5 and Sonnet 5.5 on Amazon Bedrock are
  available. `demo/litellm/config.yaml` uses the `us.` cross-Region inference profiles; outside the US, change the
  prefix to `eu.` or `apac.` and update the two ARNs in `iam/host-role-policy.json`.
- **OpenSearch version:** the newest OpenSearch 3.x the service offers. 3.7 is the minimum.
- **Tools:** bash, `jq`, Python 3.10 or later, `pip install -r requirements.txt` (pytest and PyYAML, for the tests),
  and `pip install awscurl`. On Windows, use Git Bash, not WSL.
- **AWS CLI 2.35.14 or later.** `--engine-mode` and `--use-case` on `create-domain` are new in that version; with an
  older CLI, create the domain in the console and run every other command as written.
- **Permissions:** an IAM identity with rights on OpenSearch Service, OpenSearch Ingestion, IAM, S3, EC2, SSM,
  CloudWatch Logs, and Bedrock.
- **Bedrock model access** for the two models. Check with:
  ```bash
  aws bedrock-runtime converse --model-id us.anthropic.claude-haiku-5-5 --messages '[{"role":"user","content":[{"text":"hi"}]}]' --query 'output.message.content[0].text'
  ```
- **Cost:** about $1.05 an hour while it runs, at November 2026 list prices in us-east-1: two `or2.large.search` nodes
  at $0.20, one ingestion OCU at $0.24, one `m6i.2xlarge` at $0.384, and about $0.01 of storage. Bedrock calls from
  the demo's load generator are extra. Run `scripts/cleanup.sh --delete` when you are done.
- **Time:** about an hour end to end; the AWS waits are about 15 minutes of it.

## Deploying with a coding agent

This README is written so a coding agent can run it. If you are an agent: check the prerequisites first, fixing or
working around anything the README allows. Then send the person this message, filled in, and wait:

> Hi! I'll set up the OpenTelemetry optimized-engine sample. Nothing is created yet.
>
> - **Account:** `<account id>` in `<region>`, as `<identity>`
> - **Builds:** an OpenSearch `<version>` domain on the Optimized engine, an ingestion pipeline, and an EC2 instance running the OpenTelemetry Demo with Amazon Bedrock
> - **Cost:** about $1.05 an hour while it runs, plus Bedrock calls
> - **Takes:** about an hour. **Undo:** `scripts/cleanup.sh --delete`
>
> Which IAM user or role do you sign in to the AWS console with? I'll give it access to OpenSearch UI. Then reply **ok** to start.

Send only this. Add a note only if something blocks you or needs a decision, at most two notes, one short line each.
Do not explain your reasoning or list what you checked. Do not create anything until the person replies. When Agent
Traces in OpenSearch UI lists traces from the demo, say so, and list every step where this README was unclear or wrong.

## Deployment

Fill `config.env` and render once; every command below uses the files in `rendered/`. Two values (`DOMAIN_ENDPOINT`,
`INGEST_ENDPOINT`) are only known after steps 1 and 3, so you render again after each. Every resource is created with
the tag `project=$PROJECT_TAG`; `scripts/cleanup.sh` deletes only resources that carry it.

```bash
cp config.env.example config.env   # edit
python scripts/render.py
set -a; source config.env; set +a; export AWS_DEFAULT_REGION=$REGION   # the aws CLI does not read config.env
for t in aws jq python awscurl; do command -v $t >/dev/null || echo "missing: $t"; done
TAG="Key=project,Value=$PROJECT_TAG"
```

1. **Domain.** In the console: Create domain, Standard create, use case Observability, engine mode Optimized,
   the newest OpenSearch version offered (3.7 or later), two `or2.large.search` data nodes, 20 GB gp3 per node,
   fine-grained access control with your IAM identity as master user, and the tag `project=$PROJECT_TAG`. Or from the CLI:
   ```bash
   VERSION=$(aws opensearch list-versions --query "Versions[?starts_with(@, 'OpenSearch_3.')] | [0]" --output text)   # newest first
   aws opensearch create-domain --domain-name $DOMAIN_NAME --engine-version $VERSION \
     --engine-mode OPTIMIZED --use-case OBSERVABILITY \
     --cluster-config "InstanceType=or2.large.search,InstanceCount=2,ZoneAwarenessEnabled=true,ZoneAwarenessConfig={AvailabilityZoneCount=2}" \
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
   you out.
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
   Instance: Amazon Linux 2023, **x86_64** (the compose binary in user data is amd64), any type with 32 GB RAM or
   more (`m6i.2xlarge` in the command below), root volume 60 GB or more (the demo pulls about 30 images), instance profile `$HOST_ROLE`, IMDSv2
   with hop limit 2 so containers can use the instance role. It needs outbound internet (a public IP in a default-VPC
   subnet is enough) and nothing inbound:
   ```bash
   SG_ID=$(aws ec2 create-security-group --group-name $PIPELINE_NAME-host --description "demo host, no inbound" \
     --vpc-id $(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text) \
     --tag-specifications "ResourceType=security-group,Tags=[{$TAG}]" --query GroupId --output text)
   aws ec2 run-instances --instance-type m6i.2xlarge --count 1 --associate-public-ip-address --security-group-ids $SG_ID \
     --image-id $(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text) \
     --iam-instance-profile Name=$HOST_ROLE --user-data file://rendered/demo/user-data.sh \
     --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=60,VolumeType=gp3,Encrypted=true}' \
     --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2 \
     --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$PIPELINE_NAME},{$TAG}]" "ResourceType=volume,Tags=[{$TAG}]"
   ```
   If `run-instances` returns `InsufficientInstanceCapacity`, retry in another Availability Zone (add `--subnet-id`) or
   with another 32 GB type such as `m6a.2xlarge` or `m7i.2xlarge`. Progress is written to
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
5. **OpenSearch UI.** In the console, OpenSearch UI (Dashboards): create an application, attach the domain as a data
   source, and add yourself as admin (for a console session that is the assumed-role session ARN, not the role ARN).
   Open it, create an Observability workspace on that data source, and in the workspace create a **traces dataset**:
   index pattern `otel-v2-apm-span-*`, time field `startTime`, signal type Traces. Then open Agent Monitoring > Traces.
   Without the dataset the page shows "Start tracing your AI agents"; its built-in default is `otel-v1-apm-span-*`,
   which does not exist here. From the CLI, the application is:
   ```bash
   aws opensearch create-application --name $DOMAIN_NAME-ui \
     --data-sources dataSourceArn=arn:aws:es:$REGION:$ACCOUNT_ID:domain/$DOMAIN_NAME \
     --app-configs key=opensearchDashboards.dashboardAdmin.users,value=$ADMIN_ARN \
     --tag-list $TAG
   ```
   The application endpoint accepts SigV4 for service name `opensearch` (`awscurl --service opensearch`), so the
   workspace (`POST /api/workspaces`, feature `use-case-observability`) and the dataset
   (`POST /w/<workspace>/api/saved_objects/index-pattern` with `signalType: "traces"`) can be created from a shell.
   You open OpenSearch UI signed in to the AWS console. That identity needs access, so if it is not the one that ran
   the commands above, add it as an admin of the application and to the `all_access` mapping.
   Agent Traces lists a trace when its root span satisfies `parentSpanId = "" AND isnotnull(attributes.gen_ai.operation.name)`.

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
templates. After a day, force-merge both and compare `pri.store.size` from `_cat/indices`. Count spans on the
Optimized domain: on General Purpose, nested span events count as extra Lucene documents.

## Tests

```bash
pip install -r requirements.txt
python -m pytest tests/
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
