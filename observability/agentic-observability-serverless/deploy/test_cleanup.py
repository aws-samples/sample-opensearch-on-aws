"""Offline tests for 99_cleanup.py: only resources tagged by this sample are deleted; reused buckets keep other objects.
Run: python -m pytest deploy/   or   python deploy/test_cleanup.py"""
import importlib.util
import pathlib
import types

from botocore.exceptions import ClientError

spec = importlib.util.spec_from_file_location("cleanup", pathlib.Path(__file__).resolve().parent / "99_cleanup.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
OURS = [{"Key": "project", "Value": "agentic-observability-serverless"}]
THEIRS = [{"Key": "project", "Value": "something-else"}]


def not_found(op):
    return ClientError({"Error": {"Code": "NoSuchEntity"}}, op)


class FakeS3:
    def __init__(self, keys, tags):
        self.keys, self.tags, self.deleted_bucket = set(keys), tags, False

    def get_paginator(self, _):
        outer = self

        class P:
            def paginate(self, Bucket, Prefix=""):
                yield {"Versions": [{"Key": k, "VersionId": "v1"} for k in sorted(outer.keys) if k.startswith(Prefix)]}
        return P()

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.keys.discard(o["Key"])

    def get_bucket_tagging(self, Bucket):
        if self.tags is None:
            raise ClientError({"Error": {"Code": "NoSuchTagSet"}}, "GetBucketTagging")
        return {"TagSet": self.tags}

    def list_object_versions(self, Bucket, MaxKeys=1):
        return {"Versions": [{"Key": k} for k in sorted(self.keys)][:MaxKeys]}

    def delete_bucket(self, Bucket):
        self.deleted_bucket = True


def test_owned():
    assert c.owned(OURS) and c.owned([{"key": "project", "value": "agentic-observability-serverless"}])
    assert not c.owned(THEIRS) and not c.owned([]) and not c.owned(None)


def test_reused_bucket_keeps_unrelated_objects():
    s3 = FakeS3({"dlq/traces/a.json", "dlq/logs/b.json", "other-project/keep.txt"}, tags=None)
    c.delete_prefix(s3, "b", "dlq/")
    c.delete_bucket_if_owned_and_empty(s3, "b")
    assert s3.keys == {"other-project/keep.txt"} and not s3.deleted_bucket


def test_owned_bucket_with_other_objects_is_kept():
    s3 = FakeS3({"dlq/x", "someone-else/y"}, tags=OURS)
    c.delete_prefix(s3, "b", "dlq/")
    c.delete_bucket_if_owned_and_empty(s3, "b")
    assert s3.keys == {"someone-else/y"} and not s3.deleted_bucket


def test_owned_empty_bucket_is_deleted():
    s3 = FakeS3({"dlq/x"}, tags=OURS)
    c.delete_prefix(s3, "b", "dlq/")
    c.delete_bucket_if_owned_and_empty(s3, "b")
    assert s3.deleted_bucket


def test_find_skips_untagged_resources():
    """Instance, security group and role with matching names but another project's tags are reported, not planned."""
    inst_theirs = {"InstanceId": "i-theirs"}

    class EC2:
        def describe_instances(self, Filters):
            if any(f["Name"] == "tag:project" for f in Filters):
                return {"Reservations": []}  # nothing carries our tag
            return {"Reservations": [{"Instances": [inst_theirs]}]}

        def describe_security_groups(self, Filters):
            return {"SecurityGroups": [{"GroupId": "sg-theirs", "Tags": THEIRS}]}

    class IAM:
        def list_role_tags(self, RoleName):
            if RoleName.endswith("host-role"):
                return {"Tags": THEIRS}
            raise not_found("ListRoleTags")

    empty = types.SimpleNamespace(
        list_applications=lambda: {"ApplicationSummaries": []},
        get_pipeline=lambda PipelineName: (_ for _ in ()).throw(ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetPipeline")),
        batch_get_collection=lambda names: {"collectionDetails": []},
        list_collection_groups=lambda: {"collectionGroupSummaries": []},
    )
    obj = c.Cleanup.__new__(c.Cleanup)
    obj.a = types.SimpleNamespace(name="agentobs", dlq_bucket=None, demo_app_s3=None)
    obj.n = "agentobs"
    obj.ec2, obj.iam, obj.osis, obj.aoss, obj.osc = EC2(), IAM(), empty, empty, empty
    obj.plan, obj.skipped = [], []
    obj.find()
    assert obj.instances == [] and obj.sgs == [] and obj.roles == []
    assert obj.plan == []
    assert any("i-theirs" in s for s in obj.skipped) and any("sg-theirs" in s for s in obj.skipped)
    assert any("agentobs-host-role" in s for s in obj.skipped)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
