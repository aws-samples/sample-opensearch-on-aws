#!/bin/bash
# EC2 user data (Amazon Linux 2023, x86_64, >= 32 GB RAM): install Docker, clone the OpenTelemetry Demo at tag 3.1.0,
# apply this sample's overlay, point the Collector at the pipeline and start the agentic demo.
# Before use: render the repo (scripts/render.py), upload rendered/demo as demo-overlay.tgz to {{CONFIG_BUCKET}}.
set -euxo pipefail
exec > >(tee -a /var/log/otel-demo-userdata.log) 2>&1
dnf install -y docker git make jq
systemctl enable --now docker
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64 -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
cd /root
git clone https://github.com/open-telemetry/opentelemetry-demo.git
cd /root/opentelemetry-demo
git checkout 3.1.0
aws s3 cp s3://{{CONFIG_BUCKET}}/demo-overlay.tgz /tmp/demo-overlay.tgz
tar xzf /tmp/demo-overlay.tgz -C /root/opentelemetry-demo
chmod 600 /root/opentelemetry-demo/litellm/litellm.env /root/opentelemetry-demo/.env.override
mkdir -p src/otel-collector && mv -f otelcol-config-extras.yml src/otel-collector/otelcol-config-extras.yml
# ACTIVE does not mean ingesting: check the pipeline's CloudWatch log group if nothing arrives.
for i in $(seq 1 80); do
  ST=$(aws osis get-pipeline --pipeline-name {{PIPELINE_NAME}} --region {{REGION}} --query Pipeline.Status --output text 2>/dev/null || echo UNKNOWN)
  echo "pipeline status: $ST"; [ "$ST" = "ACTIVE" ] && break; sleep 30
done
make start-agentic DOCKER_COMPOSE_FILES_AGENT="-f compose.agent.yaml -f compose.bedrock.yaml -f compose.patches.yaml -f compose.extras.yaml"
sleep 60
docker ps --format '{{.Names}} {{.Status}}' | sort
