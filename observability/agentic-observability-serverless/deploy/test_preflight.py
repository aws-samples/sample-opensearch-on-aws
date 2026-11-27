"""Offline checks for the read-only preflight helpers."""
import importlib.util
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("preflight", HERE / "00_preflight.py")
pf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pf)


class FakeEC2:
    def describe_vpcs(self):
        return {"Vpcs": [{"VpcId": "vpc-default", "IsDefault": True}, {"VpcId": "vpc-other", "IsDefault": False}]}

    def describe_route_tables(self):
        igw = [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1", "State": "active"}]
        nat = [{"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1", "State": "active"}]
        return {"RouteTables": [
            {"VpcId": "vpc-default", "Routes": igw, "Associations": [{"Main": True}]},
            {"VpcId": "vpc-other", "Routes": nat, "Associations": [{"Main": True}]},
            {"VpcId": "vpc-other", "Routes": igw, "Associations": [{"SubnetId": "subnet-other-public"}]},
            {"VpcId": "vpc-default", "Routes": nat, "Associations": [{"SubnetId": "subnet-default-private"}]},
        ]}

    def describe_subnets(self):
        def sn(i, vpc, az, free):
            return {"SubnetId": i, "VpcId": vpc, "AvailabilityZone": az, "AvailableIpAddressCount": free}
        return {"Subnets": [sn("subnet-other-public", "vpc-other", "us-east-1a", 9000),
                            sn("subnet-other-private", "vpc-other", "us-east-1a", 9000),
                            sn("subnet-default-a", "vpc-default", "us-east-1a", 100),
                            sn("subnet-default-b", "vpc-default", "us-east-1b", 4000),
                            sn("subnet-default-private", "vpc-default", "us-east-1c", 5000)]}


def test_public_subnets_prefers_default_vpc_and_skips_private():
    got = [sid for sid, _, _ in pf.public_subnets(FakeEC2())]
    assert got == ["subnet-default-b", "subnet-default-a", "subnet-other-public"]


def test_role_session_maps_to_role_arn():
    assert (pf.principal_for_data_access("arn:aws:sts::123456789012:assumed-role/Admin/jane")
            == "arn:aws:iam::123456789012:role/Admin")
    assert pf.principal_for_data_access("arn:aws:iam::123456789012:user/jane") == "arn:aws:iam::123456789012:user/jane"


def test_model_label():
    assert pf.model_label("us.anthropic.claude-haiku-5-5") == "Claude Haiku 5.5"
