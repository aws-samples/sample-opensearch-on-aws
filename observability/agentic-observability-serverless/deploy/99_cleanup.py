"""Cleanup: delete everything the deploy scripts created for one --name, in reverse order. Idempotent.

  EC2 instance <name>-shop, security group <name>-host-sg, instance profile + role <name>-host-role
  OpenSearch UI application <name>-ui
  OpenSearch Ingestion pipeline <name>-otlp and its CloudWatch log group
  DLQ bucket (emptied first) if --dlq-bucket is given
  IAM role <name>-pipeline-role
  collection <name>-ts, data access / network / encryption policies, collection group <name>

Usage:
  python deploy/99_cleanup.py --name agentobs --region us-east-1 --profile default --dlq-bucket <bucket> [--yes]
Without --yes it lists what it would delete and asks for confirmation.
"""
import argparse
import sys
import time

import boto3
from botocore.exceptions import ClientError


def code(e):
    return getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)


def ignore(fn, *codes):
    try:
        return fn()
    except ClientError as e:
        if code(e) in codes or not codes:
            return None
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="agentobs")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--dlq-bucket", default=None)
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    s = boto3.Session(profile_name=a.profile, region_name=a.region)
    ec2, iam, osis, aoss, osc, logs, s3 = (s.client(x) for x in ("ec2", "iam", "osis", "opensearchserverless", "opensearch", "logs", "s3"))
    n = a.name
    plan = [f"EC2 instance {n}-shop", f"security group {n}-host-sg", f"instance profile and role {n}-host-role",
            f"OpenSearch UI application {n}-ui", f"pipeline {n}-otlp and log group", f"IAM role {n}-pipeline-role",
            f"collection {n}-ts, policies {n}-data/{n}-net/{n}-enc, collection group {n}"]
    if a.dlq_bucket:
        plan.append(f"S3 bucket {a.dlq_bucket} (emptied, then deleted)")
    print("Will delete:\n  " + "\n  ".join(plan))
    if not a.yes and input("Type the name prefix to confirm: ").strip() != n:
        print("aborted")
        return 1

    # EC2
    r = ec2.describe_instances(Filters=[{"Name": "tag:Name", "Values": [f"{n}-shop"]},
                                        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
    ids = [i["InstanceId"] for res in r["Reservations"] for i in res["Instances"]]
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
        print(f"terminating {ids}")
        ec2.get_waiter("instance_terminated").wait(InstanceIds=ids)
    for sg in ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [f"{n}-host-sg"]}])["SecurityGroups"]:
        ignore(lambda: ec2.delete_security_group(GroupId=sg["GroupId"]), "DependencyViolation", "InvalidGroup.NotFound")
        print(f"security group {sg['GroupId']} deleted")
    for role in (f"{n}-host-role",):
        ignore(lambda: iam.remove_role_from_instance_profile(InstanceProfileName=role, RoleName=role), "NoSuchEntity")
        ignore(lambda: iam.delete_instance_profile(InstanceProfileName=role), "NoSuchEntity")
        delete_role(iam, role)

    # UI application
    for app in osc.list_applications().get("ApplicationSummaries", []):
        if app.get("name") == f"{n}-ui":
            osc.delete_application(id=app["id"])
            print(f"ui application {n}-ui delete requested")

    # pipeline + logs
    if ignore(lambda: osis.delete_pipeline(PipelineName=f"{n}-otlp"), "ResourceNotFoundException") is not None:
        print(f"pipeline {n}-otlp delete requested")
        for _ in range(60):
            time.sleep(10)
            try:
                osis.get_pipeline(PipelineName=f"{n}-otlp")
            except ClientError:
                break
    ignore(lambda: logs.delete_log_group(logGroupName=f"/aws/vendedlogs/OpenSearchIngestion/{n}-otlp/audit-logs"), "ResourceNotFoundException")
    delete_role(iam, f"{n}-pipeline-role")

    # DLQ bucket
    if a.dlq_bucket:
        try:
            paginator = s3.get_paginator("list_object_versions")
            for page in paginator.paginate(Bucket=a.dlq_bucket):
                objs = [{"Key": o["Key"], "VersionId": o["VersionId"]} for k in ("Versions", "DeleteMarkers") for o in page.get(k, [])]
                if objs:
                    s3.delete_objects(Bucket=a.dlq_bucket, Delete={"Objects": objs})
            s3.delete_bucket(Bucket=a.dlq_bucket)
            print(f"bucket {a.dlq_bucket} deleted")
        except ClientError as e:
            print(f"bucket {a.dlq_bucket}: {code(e)}")

    # collection, policies, group
    cols = aoss.batch_get_collection(names=[f"{n}-ts"]).get("collectionDetails", [])
    if cols:
        aoss.delete_collection(id=cols[0]["id"])
        print(f"collection {n}-ts delete requested")
        for _ in range(60):
            time.sleep(10)
            if not aoss.batch_get_collection(names=[f"{n}-ts"]).get("collectionDetails"):
                break
    ignore(lambda: aoss.delete_access_policy(name=f"{n}-data", type="data"), "ResourceNotFoundException")
    ignore(lambda: aoss.delete_security_policy(name=f"{n}-net", type="network"), "ResourceNotFoundException")
    ignore(lambda: aoss.delete_security_policy(name=f"{n}-enc", type="encryption"), "ResourceNotFoundException")
    for g in aoss.list_collection_groups().get("collectionGroupSummaries", []):
        if g["name"] == n:
            ignore(lambda: aoss.delete_collection_group(id=g["id"]))
            print(f"collection group {n} delete requested")
    print("cleanup done")
    return 0


def delete_role(iam, role):
    try:
        for p in iam.list_role_policies(RoleName=role)["PolicyNames"]:
            iam.delete_role_policy(RoleName=role, PolicyName=p)
        for p in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=role)
        print(f"iam role {role} deleted")
    except ClientError as e:
        if code(e) != "NoSuchEntity":
            raise


if __name__ == "__main__":
    sys.exit(main())
