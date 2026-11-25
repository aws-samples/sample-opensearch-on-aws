"""Cleanup: delete what the deploy scripts created for one --name, in reverse order. Idempotent.

Ownership is checked before every deletion: a resource is deleted only if it carries the tag
project=agentic-observability-serverless that the deploy scripts set. Anything with a matching name but without that
tag is reported and left alone.

  EC2 instance <name>-shop, security group <name>-host-sg, instance profile + role <name>-host-role
  OpenSearch UI application <name>-ui
  OpenSearch Ingestion pipeline <name>-otlp (and its log group, if the pipeline is owned)
  IAM role <name>-pipeline-role
  collection <name>-ts (and its data / network / encryption policies, if the collection is owned), collection group <name>
  S3: only objects under dlq/<name>/ (and under --demo-app-s3, if given). The bucket itself is deleted only if this sample
  created it (tagged) and it is empty afterwards; other contents of a reused bucket are kept.

Usage:
  python deploy/99_cleanup.py --name agentobs --region us-east-1 --profile default --dlq-bucket <bucket> [--demo-app-s3 s3://b/p] [--yes]
Without --yes it shows what it would delete and asks for confirmation.
"""
import argparse
import sys
import time

import boto3
from botocore.exceptions import ClientError

TAG_KEY, TAG_VALUE = "project", "agentic-observability-serverless"


def code(e):
    return getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)


def owned(tags):
    """tags: list of {Key/key, Value/value} or dict."""
    if isinstance(tags, dict):
        return tags.get(TAG_KEY) == TAG_VALUE
    for t in tags or []:
        k, v = t.get("Key", t.get("key")), t.get("Value", t.get("value"))
        if k == TAG_KEY and v == TAG_VALUE:
            return True
    return False


class Cleanup:
    def __init__(self, a):
        self.a, self.n = a, a.name
        s = boto3.Session(profile_name=a.profile, region_name=a.region)
        self.ec2, self.iam, self.osis, self.aoss, self.osc, self.logs, self.s3 = (
            s.client(x) for x in ("ec2", "iam", "osis", "opensearchserverless", "opensearch", "logs", "s3"))
        self.plan, self.skipped = [], []

    # ---------- discovery (read-only)
    def find(self):
        n = self.n
        r = self.ec2.describe_instances(Filters=[{"Name": "tag:Name", "Values": [f"{n}-shop"]},
                                                 {"Name": f"tag:{TAG_KEY}", "Values": [TAG_VALUE]},
                                                 {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
        self.instances = [i["InstanceId"] for res in r["Reservations"] for i in res["Instances"]]
        other = self.ec2.describe_instances(Filters=[{"Name": "tag:Name", "Values": [f"{n}-shop"]},
                                                     {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
        for res in other["Reservations"]:
            for i in res["Instances"]:
                if i["InstanceId"] not in self.instances:
                    self.skipped.append(f"instance {i['InstanceId']} named {n}-shop (no {TAG_KEY} tag)")
        self.plan += [f"EC2 instance {i}" for i in self.instances]

        sgs = self.ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [f"{n}-host-sg"]}])["SecurityGroups"]
        self.sgs = [g["GroupId"] for g in sgs if owned(g.get("Tags"))]
        self.skipped += [f"security group {g['GroupId']} (no {TAG_KEY} tag)" for g in sgs if not owned(g.get("Tags"))]
        self.plan += [f"security group {g}" for g in self.sgs]

        self.roles = []
        for role in (f"{n}-host-role", f"{n}-pipeline-role"):
            try:
                tags = self.iam.list_role_tags(RoleName=role)["Tags"]
            except ClientError as e:
                if code(e) == "NoSuchEntity":
                    continue
                raise
            (self.roles.append(role) if owned(tags) else self.skipped.append(f"IAM role {role} (no {TAG_KEY} tag)"))
        self.plan += [f"IAM role {r} (and its instance profile, if any)" for r in self.roles]

        self.apps = []
        for app in self.osc.list_applications().get("ApplicationSummaries", []):
            if app.get("name") == f"{n}-ui":
                arn = self.osc.get_application(id=app["id"]).get("arn")
                try:
                    tags = self.osc.list_tags(ARN=arn).get("TagList", []) if arn else []
                except ClientError:
                    tags = []  # cannot verify ownership: treat as not ours
                (self.apps.append(app["id"]) if owned(tags) else self.skipped.append(f"UI application {n}-ui (no {TAG_KEY} tag)"))
        self.plan += [f"OpenSearch UI application {n}-ui"] * len(self.apps)

        self.pipeline = None
        try:
            p = self.osis.get_pipeline(PipelineName=f"{n}-otlp")["Pipeline"]
            tags = self.osis.list_tags_for_resource(Arn=p["PipelineArn"]).get("Tags", [])
            if owned(tags):
                self.pipeline = f"{n}-otlp"
                self.plan.append(f"pipeline {n}-otlp and its log group")
            else:
                self.skipped.append(f"pipeline {n}-otlp (no {TAG_KEY} tag)")
        except ClientError as e:
            if code(e) != "ResourceNotFoundException":
                raise

        self.collection = None
        cols = self.aoss.batch_get_collection(names=[f"{n}-ts"]).get("collectionDetails", [])
        if cols:
            tags = self.aoss.list_tags_for_resource(resourceArn=cols[0]["arn"]).get("tags", [])
            if owned(tags):
                self.collection = cols[0]["id"]
                self.plan.append(f"collection {n}-ts and policies {n}-data / {n}-net / {n}-enc")
            else:
                self.skipped.append(f"collection {n}-ts (no {TAG_KEY} tag); its policies are kept too")

        self.group = None
        for g in self.aoss.list_collection_groups().get("collectionGroupSummaries", []):
            if g["name"] == n:
                tags = self.aoss.list_tags_for_resource(resourceArn=g["arn"]).get("tags", [])
                if owned(tags):
                    self.group = g["id"]
                    self.plan.append(f"collection group {n}")
                else:
                    self.skipped.append(f"collection group {n} (no {TAG_KEY} tag)")

        self.prefixes = []
        if self.a.dlq_bucket:
            self.prefixes.append((self.a.dlq_bucket, f"dlq/{self.n}/"))
            self.plan.append(f"S3 objects under s3://{self.a.dlq_bucket}/dlq/{self.n}/ (bucket deleted only if created by this sample and then empty)")
        if self.a.demo_app_s3:
            b, _, p = self.a.demo_app_s3.replace("s3://", "").partition("/")
            p = (p.rstrip("/") or "demo-app") + "/"
            self.prefixes.append((b, p))
            self.plan.append(f"S3 objects under s3://{b}/{p}")

    # ---------- deletion
    def run(self):
        ec2, iam = self.ec2, self.iam
        if self.instances:
            ec2.terminate_instances(InstanceIds=self.instances)
            print(f"terminating {self.instances}")
            ec2.get_waiter("instance_terminated").wait(InstanceIds=self.instances)
        for sg in self.sgs:
            try:
                ec2.delete_security_group(GroupId=sg)
                print(f"security group {sg} deleted")
            except ClientError as e:
                print(f"security group {sg}: {code(e)}")
        for app in self.apps:
            self.osc.delete_application(id=app)
            print(f"UI application {self.n}-ui delete requested")
        if self.pipeline:
            self.osis.delete_pipeline(PipelineName=self.pipeline)
            print(f"pipeline {self.pipeline} delete requested")
            for _ in range(60):
                time.sleep(10)
                try:
                    self.osis.get_pipeline(PipelineName=self.pipeline)
                except ClientError:
                    break
            try:
                self.logs.delete_log_group(logGroupName=f"/aws/vendedlogs/OpenSearchIngestion/{self.pipeline}/audit-logs")
            except ClientError as e:
                if code(e) != "ResourceNotFoundException":
                    raise
        for role in self.roles:
            delete_role(iam, role)
        for bucket, prefix in self.prefixes:
            delete_prefix(self.s3, bucket, prefix)
        if self.a.dlq_bucket:
            delete_bucket_if_owned_and_empty(self.s3, self.a.dlq_bucket)
        if self.collection:
            self.aoss.delete_collection(id=self.collection)
            print(f"collection {self.n}-ts delete requested")
            for _ in range(60):
                time.sleep(10)
                if not self.aoss.batch_get_collection(names=[f"{self.n}-ts"]).get("collectionDetails"):
                    break
            for name, ptype, fn in ((f"{self.n}-data", "data", self.aoss.delete_access_policy),
                                    (f"{self.n}-net", "network", self.aoss.delete_security_policy),
                                    (f"{self.n}-enc", "encryption", self.aoss.delete_security_policy)):
                try:
                    fn(name=name, type=ptype)
                    print(f"{ptype} policy {name} deleted")
                except ClientError as e:
                    if code(e) != "ResourceNotFoundException":
                        raise
        if self.group:
            try:
                self.aoss.delete_collection_group(id=self.group)
                print(f"collection group {self.n} delete requested")
            except ClientError as e:
                print(f"collection group {self.n}: {code(e)} (still has collections?)")


def delete_role(iam, role):
    try:
        for prof in iam.list_instance_profiles_for_role(RoleName=role)["InstanceProfiles"]:
            iam.remove_role_from_instance_profile(InstanceProfileName=prof["InstanceProfileName"], RoleName=role)
            if prof["InstanceProfileName"] == role:  # created by 03_host.py with the same name
                iam.delete_instance_profile(InstanceProfileName=role)
        for p in iam.list_role_policies(RoleName=role)["PolicyNames"]:
            iam.delete_role_policy(RoleName=role, PolicyName=p)
        for p in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
        iam.delete_role(RoleName=role)
        print(f"IAM role {role} deleted")
    except ClientError as e:
        if code(e) != "NoSuchEntity":
            raise


def delete_prefix(s3, bucket, prefix):
    """Delete every object version and delete marker under one prefix only."""
    removed = 0
    try:
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix):
            objs = [{"Key": o["Key"], "VersionId": o["VersionId"]} for k in ("Versions", "DeleteMarkers") for o in page.get(k, [])
                    if o["Key"].startswith(prefix)]
            if objs:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})
                removed += len(objs)
        print(f"s3://{bucket}/{prefix}: {removed} object versions deleted")
    except ClientError as e:
        print(f"s3://{bucket}/{prefix}: {code(e)}")


def delete_bucket_if_owned_and_empty(s3, bucket):
    try:
        tags = s3.get_bucket_tagging(Bucket=bucket).get("TagSet", [])
    except ClientError as e:
        if code(e) in ("NoSuchTagSet", "NoSuchBucket"):
            tags = []
        else:
            raise
    if not owned(tags):
        print(f"bucket {bucket}: not created by this sample (no {TAG_KEY} tag); kept")
        return
    left = s3.list_object_versions(Bucket=bucket, MaxKeys=1)
    if left.get("Versions") or left.get("DeleteMarkers"):
        print(f"bucket {bucket}: still holds other objects; kept")
        return
    s3.delete_bucket(Bucket=bucket)
    print(f"bucket {bucket} deleted")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="agentobs")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--dlq-bucket", default=None)
    ap.add_argument("--demo-app-s3", default=None, metavar="s3://bucket/prefix", help="the value passed to 03_host.py, if any")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    c = Cleanup(a)
    c.find()
    print("Will delete (created by this sample):\n  " + ("\n  ".join(c.plan) if c.plan else "nothing"))
    if c.skipped:
        print("Will NOT touch (matching name, not created by this sample):\n  " + "\n  ".join(c.skipped))
    if not c.plan:
        return 0
    if not a.yes and input("Type the name prefix to confirm: ").strip() != a.name:
        print("aborted")
        return 1
    c.run()
    print("cleanup done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
