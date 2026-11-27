#!/bin/bash
# Delete the resources this sample created, and only those: each one is checked for the tag project=$PROJECT_TAG
# before deletion. Same-named resources without the tag are listed and skipped. Buckets are emptied and deleted only
# when tagged. Default is list-only; --delete asks for confirmation, then deletes in dependency order.
# Usage: scripts/cleanup.sh [--delete]        (reads config.env in the repository root)
set -euo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: do not rewrite /aws/... arguments as file paths
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$ROOT/config.env" ] || { echo "config.env not found in $ROOT"; exit 1; }
set -a; source "$ROOT/config.env"; set +a
export AWS_DEFAULT_REGION="$REGION"
: "${PROJECT_TAG:?PROJECT_TAG missing from config.env}"
DELETE=0; [ "${1:-}" = "--delete" ] && DELETE=1
PLAN=()   # "label|command" pairs, in deletion order

tagged() {  # $1 = label, $2 = tag list as JSON array of {Key,Value} (or empty on error)
  if echo "$2" | jq -e --arg t "$PROJECT_TAG" 'map(select(.Key=="project" and .Value==$t)) | length > 0' >/dev/null 2>&1; then
    echo "  [tagged]   $1"; return 0
  else
    echo "  [SKIP]     $1 exists but is not tagged project=$PROJECT_TAG"; return 1
  fi
}
plan() { PLAN+=("$1|$2"); }

echo "Resources for project=$PROJECT_TAG in $REGION"

# 1. EC2 instances and their security group (tag filter on the API itself)
IDS=$(aws ec2 describe-instances --filters "Name=tag:project,Values=$PROJECT_TAG" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
      --query 'Reservations[].Instances[].InstanceId' --output text)
for id in $IDS; do echo "  [tagged]   instance $id"; plan "instance $id" "aws ec2 terminate-instances --instance-ids $id >/dev/null && aws ec2 wait instance-terminated --instance-ids $id"; done
SG=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=$PIPELINE_NAME-host" --query 'SecurityGroups[0]' --output json 2>/dev/null || echo null)
if [ "$SG" != "null" ]; then
  SG_ID=$(echo "$SG" | jq -r .GroupId)
  tagged "security group $SG_ID" "$(echo "$SG" | jq -c '.Tags // []')" && plan "security group $SG_ID" "aws ec2 delete-security-group --group-id $SG_ID"
fi

# 2. OpenSearch UI application
APP=$(aws opensearch list-applications --query "ApplicationSummaries[?name=='$DOMAIN_NAME-ui'].id | [0]" --output text 2>/dev/null || echo None)
if [ "$APP" != "None" ] && [ -n "$APP" ]; then
  APP_ARN=$(aws opensearch get-application --id "$APP" --query arn --output text)
  tagged "application $APP" "$(aws opensearch list-tags --arn "$APP_ARN" --query TagList --output json 2>/dev/null || echo '[]')" \
    && plan "application $APP" "aws opensearch delete-application --id $APP"
fi

# 3. Pipeline
if aws osis get-pipeline --pipeline-name "$PIPELINE_NAME" >/dev/null 2>&1; then
  P_ARN=$(aws osis get-pipeline --pipeline-name "$PIPELINE_NAME" --query Pipeline.PipelineArn --output text)
  tagged "pipeline $PIPELINE_NAME" "$(aws osis list-tags-for-resource --arn "$P_ARN" --query Tags --output json)" \
    && plan "pipeline $PIPELINE_NAME" "aws osis delete-pipeline --pipeline-name $PIPELINE_NAME"
fi

# 4. Domain
if aws opensearch describe-domain --domain-name "$DOMAIN_NAME" >/dev/null 2>&1; then
  D_ARN=$(aws opensearch describe-domain --domain-name "$DOMAIN_NAME" --query DomainStatus.ARN --output text)
  tagged "domain $DOMAIN_NAME" "$(aws opensearch list-tags --arn "$D_ARN" --query TagList --output json)" \
    && plan "domain $DOMAIN_NAME" "aws opensearch delete-domain --domain-name $DOMAIN_NAME >/dev/null"
fi

# 5. Buckets: emptied and deleted only when tagged (a reused, untagged bucket is left alone)
for b in "$DLQ_BUCKET" "$CONFIG_BUCKET"; do
  if aws s3api head-bucket --bucket "$b" >/dev/null 2>&1; then
    tagged "bucket $b" "$(aws s3api get-bucket-tagging --bucket "$b" --query TagSet --output json 2>/dev/null || echo '[]')" \
      && plan "bucket $b" "aws s3 rm s3://$b --recursive --quiet && aws s3api delete-bucket --bucket $b"
  fi
done

# 6. Log group (tags are a map, not a list)
LG="/aws/vendedlogs/OpenSearchIngestion/$PIPELINE_NAME"
if aws logs describe-log-groups --log-group-name-prefix "$LG" --query "logGroups[?logGroupName=='$LG'] | length(@)" --output text | grep -q '^1$'; then
  LG_TAGS=$(aws logs list-tags-log-group --log-group-name "$LG" --query tags --output json 2>/dev/null || echo '{}')
  tagged "log group $LG" "$(echo "$LG_TAGS" | jq -c 'to_entries | map({Key:.key, Value:.value})')" \
    && plan "log group $LG" "aws logs delete-log-group --log-group-name $LG"
fi

# 7. Instance profile and roles
if aws iam get-instance-profile --instance-profile-name "$HOST_ROLE" >/dev/null 2>&1; then
  tagged "instance profile $HOST_ROLE" "$(aws iam get-instance-profile --instance-profile-name "$HOST_ROLE" --query InstanceProfile.Tags --output json)" \
    && plan "instance profile $HOST_ROLE" "aws iam remove-role-from-instance-profile --instance-profile-name $HOST_ROLE --role-name $HOST_ROLE 2>/dev/null; aws iam delete-instance-profile --instance-profile-name $HOST_ROLE"
fi
for r in "$HOST_ROLE" "$PIPELINE_ROLE"; do
  if aws iam get-role --role-name "$r" >/dev/null 2>&1; then
    tagged "role $r" "$(aws iam get-role --role-name "$r" --query Role.Tags --output json)" \
      && plan "role $r" "for p in \$(aws iam list-role-policies --role-name $r --query PolicyNames --output text); do aws iam delete-role-policy --role-name $r --policy-name \$p; done; for a in \$(aws iam list-attached-role-policies --role-name $r --query 'AttachedPolicies[].PolicyArn' --output text); do aws iam detach-role-policy --role-name $r --policy-arn \$a; done; aws iam delete-role --role-name $r"
  fi
done

echo; echo "${#PLAN[@]} resource(s) would be deleted."
[ "$DELETE" = 1 ] || { echo "Run again with --delete to remove them."; exit 0; }
[ "${#PLAN[@]}" -gt 0 ] || exit 0
read -r -p "Type the project tag ($PROJECT_TAG) to confirm deletion: " ANSWER
[ "$ANSWER" = "$PROJECT_TAG" ] || { echo "Not confirmed; nothing deleted."; exit 1; }
for item in "${PLAN[@]}"; do
  label="${item%%|*}"; cmd="${item#*|}"
  echo "deleting $label"; bash -c "$cmd" || echo "  failed: $label (continue with the rest)"
done
echo "done"
