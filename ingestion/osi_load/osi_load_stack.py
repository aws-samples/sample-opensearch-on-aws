# SPDX-License-Identifier: MIT-0

import os
import json
from aws_cdk import (
    CfnOutput,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_opensearchservice as opensearch,
    aws_osis as osis,
    aws_logs as logs,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    custom_resources as cr,
)
from aws_cdk.aws_s3_assets import Asset
from cdk_nag import NagSuppressions
from constructs import Construct

# Constants
OSI_LOAD_CFN_PREFIX = 'OsiLoad'
OSI_LOAD_RESOURCE_PREFIX = 'osi-load-'

class OsiLoadStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Configurable security settings via CDK context (-c flag)
        allowed_jumphost_cidr = self.node.try_get_context("jumphost_allowed_cidr")
        enable_detailed_monitoring = self.node.try_get_context("enable_detailed_monitoring") == "true"

        # VPC with public and private subnets
        vpc = ec2.Vpc(self, f"{OSI_LOAD_CFN_PREFIX}VPC", 
                     vpc_name=f"{OSI_LOAD_RESOURCE_PREFIX}vpc",
                     max_azs=3)

        # Security group for OpenSearch
        os_security_group = ec2.SecurityGroup(self, f"{OSI_LOAD_CFN_PREFIX}OSSecurityGroup",
                                            vpc=vpc,
                                            security_group_name=f"{OSI_LOAD_RESOURCE_PREFIX}os-sg",
                                            description="Security group for OpenSearch domain - allows HTTPS from VPC",
                                            allow_all_outbound=True)
        os_security_group.add_ingress_rule(ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(443))

        # Security group for jumphost - HTTPS only, no SSH (use SSM Session Manager)
        jumphost_security_group = ec2.SecurityGroup(self, f"{OSI_LOAD_CFN_PREFIX}JumphostSecurityGroup",
                                                   vpc=vpc,
                                                   security_group_name=f"{OSI_LOAD_RESOURCE_PREFIX}jumphost-sg",
                                                   description="Security group for jumphost - HTTPS access for OpenSearch Dashboards proxy",
                                                   allow_all_outbound=True)
        # Note: SSH removed for security. Use SSM Session Manager instead:
        # aws ssm start-session --target <instance-id>
        jumphost_peer = ec2.Peer.ipv4(allowed_jumphost_cidr) if allowed_jumphost_cidr else ec2.Peer.any_ipv4()
        jumphost_security_group.add_ingress_rule(
            jumphost_peer, ec2.Port.tcp(443),
            "HTTPS access - restrict with: cdk deploy -c jumphost_allowed_cidr=YOUR_IP/32"
        )

        # S3 buckets with SSL enforcement
        source_bucket = s3.Bucket(self, f"{OSI_LOAD_CFN_PREFIX}SourceBucket",
                                 bucket_name=f"{OSI_LOAD_RESOURCE_PREFIX}source-bucket-{self.account}-{self.region}",
                                 removal_policy=RemovalPolicy.DESTROY,
                                 auto_delete_objects=True,
                                 enforce_ssl=True,
                                 block_public_access=s3.BlockPublicAccess.BLOCK_ALL)

        dlq_bucket = s3.Bucket(self, f"{OSI_LOAD_CFN_PREFIX}DLQBucket",
                              bucket_name=f"{OSI_LOAD_RESOURCE_PREFIX}dlq-bucket-{self.account}-{self.region}",
                              removal_policy=RemovalPolicy.DESTROY,
                              auto_delete_objects=True,
                              enforce_ssl=True,
                              block_public_access=s3.BlockPublicAccess.BLOCK_ALL)

        # IAM role for OSI and OpenSearch
        osi_os_role = self._create_osi_os_role(source_bucket, dlq_bucket)

        # Master user credentials in Secrets Manager (auto-generated secure password)
        master_user_secret = secretsmanager.Secret(self, f"{OSI_LOAD_CFN_PREFIX}MasterUserSecret",
            secret_name=f"{OSI_LOAD_RESOURCE_PREFIX}master-user",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({"username": "admin"}),
                generate_string_key="password",
                password_length=16,
                exclude_punctuation=False,
                exclude_characters='"@/\\'
            )
        )

        # CloudWatch log groups for OpenSearch slow logs
        slow_search_log_group = logs.LogGroup(self, f"{OSI_LOAD_CFN_PREFIX}SlowSearchLogGroup",
            log_group_name=f"/aws/opensearch/domains/{OSI_LOAD_RESOURCE_PREFIX}domain/slow-search-logs",
            removal_policy=RemovalPolicy.DESTROY
        )
        slow_index_log_group = logs.LogGroup(self, f"{OSI_LOAD_CFN_PREFIX}SlowIndexLogGroup",
            log_group_name=f"/aws/opensearch/domains/{OSI_LOAD_RESOURCE_PREFIX}domain/slow-index-logs",
            removal_policy=RemovalPolicy.DESTROY
        )

        # OpenSearch domain
        os_domain = opensearch.Domain(self, f"{OSI_LOAD_CFN_PREFIX}OpenSearchDomain",
                                      domain_name=f"{OSI_LOAD_RESOURCE_PREFIX}domain",
                                      version=opensearch.EngineVersion.OPENSEARCH_3_3,
                                      capacity=opensearch.CapacityConfig(
                                          data_node_instance_type="r7g.large.search",
                                          data_nodes=3,
                                          master_node_instance_type="r7g.large.search",
                                          master_nodes=3
                                      ),
                                      ebs=opensearch.EbsOptions(
                                          enabled=True,
                                          volume_size=20,
                                          volume_type=ec2.EbsDeviceVolumeType.GP3
                                      ),
                                      vpc=vpc,
                                      vpc_subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],
                                      security_groups=[os_security_group],
                                      zone_awareness=opensearch.ZoneAwarenessConfig(
                                          enabled=True,
                                          availability_zone_count=3
                                      ),
                                      use_unsigned_basic_auth=True,
                                      access_policies=[iam.PolicyStatement(
                                          effect=iam.Effect.ALLOW,
                                          principals=[iam.ArnPrincipal(osi_os_role.role_arn)],
                                          actions=["es:ESHttp*"],
                                          resources=[f"arn:aws:es:{self.region}:{self.account}:domain/{OSI_LOAD_RESOURCE_PREFIX}domain/*"]
                                      )],
                                      fine_grained_access_control=opensearch.AdvancedSecurityOptions(
                                          master_user_name="admin",
                                          master_user_password=master_user_secret.secret_value_from_json("password")
                                      ),
                                      removal_policy=RemovalPolicy.DESTROY,
                                      enforce_https=True,
                                      node_to_node_encryption=True,
                                      encryption_at_rest=opensearch.EncryptionAtRestOptions(enabled=True),
                                      logging=opensearch.LoggingOptions(
                                          slow_search_log_enabled=True,
                                          slow_search_log_group=slow_search_log_group,
                                          slow_index_log_enabled=True,
                                          slow_index_log_group=slow_index_log_group,
                                      )
        )

        # CloudWatch log group for OSI pipeline (must exist before pipeline creation)
        osi_log_group = logs.LogGroup(self, f"{OSI_LOAD_CFN_PREFIX}OSILogGroup",
            log_group_name=f"/aws/vendedlogs/OpenSearchIngestion/{OSI_LOAD_RESOURCE_PREFIX}pipeline/osis-logs",
            removal_policy=RemovalPolicy.DESTROY
        )

        # OpenSearch Ingestion pipeline
        osi_pipeline = osis.CfnPipeline(self, f"{OSI_LOAD_CFN_PREFIX}OSIPipeline",
                                        pipeline_name=f"{OSI_LOAD_RESOURCE_PREFIX}pipeline",
                                        pipeline_configuration_body=self._get_pipeline_config(source_bucket, dlq_bucket, os_domain, osi_os_role),
                                        min_units=1,
                                        max_units=4,
                                        log_publishing_options=osis.CfnPipeline.LogPublishingOptionsProperty(
                                            is_logging_enabled=True,
                                            cloud_watch_log_destination=osis.CfnPipeline.CloudWatchLogDestinationProperty(
                                                log_group=f"/aws/vendedlogs/OpenSearchIngestion/{OSI_LOAD_RESOURCE_PREFIX}pipeline/osis-logs"
                                            )
                                        )
        )
        
        # Add explicit dependencies on OpenSearch domain and log group
        osi_pipeline.add_dependency(os_domain.node.default_child)
        osi_pipeline.node.add_dependency(osi_log_group)

        # Custom resource to configure FGAC backend role
        opensearch_layer = lambda_.LayerVersion(self, f"{OSI_LOAD_CFN_PREFIX}OpenSearchLayer",
            code=lambda_.Code.from_asset("lambda-layer"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12]
        )

        fgac_lambda = lambda_.Function(self, f"{OSI_LOAD_CFN_PREFIX}FGACLambda",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(self._get_fgac_lambda_code()),
            timeout=Duration.minutes(5),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            role=osi_os_role,
            layers=[opensearch_layer]
        )

        fgac_provider = cr.Provider(self, f"{OSI_LOAD_CFN_PREFIX}FGACProvider",
            on_event_handler=fgac_lambda
        )

        CustomResource(self, f"{OSI_LOAD_CFN_PREFIX}FGACCustomResource",
            service_token=fgac_provider.service_token,
            properties={
                "DomainEndpoint": os_domain.domain_endpoint,
                "RoleArn": osi_os_role.role_arn,
                "SecretArn": master_user_secret.secret_arn
            }
        )
        
        # Add explicit dependencies to ensure proper ordering
        fgac_lambda.node.add_dependency(master_user_secret)
        fgac_lambda.node.add_dependency(os_domain)

        # Jumphost EC2 instance
        amzn_linux = ec2.MachineImage.latest_amazon_linux2()
        
        jumphost_role = iam.Role(self, f"{OSI_LOAD_CFN_PREFIX}JumphostRole",
                               role_name=f"{OSI_LOAD_RESOURCE_PREFIX}jumphost-role",
                               assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"))
        jumphost_role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"))

        jumphost = ec2.Instance(self, f"{OSI_LOAD_CFN_PREFIX}Jumphost",
                               instance_type=ec2.InstanceType("t3.small"),
                               machine_image=amzn_linux,
                               vpc=vpc,
                               vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
                               security_group=jumphost_security_group,
                               role=jumphost_role,
                               detailed_monitoring=enable_detailed_monitoring,
                               block_devices=[ec2.BlockDevice(
                                   device_name="/dev/xvda",
                                   volume=ec2.BlockDeviceVolume.ebs(8, encrypted=True)
                               )])

        # Create nginx config asset
        dirname = os.path.dirname(__file__)
        nginx_asset = Asset(self, f"{OSI_LOAD_CFN_PREFIX}NginxAsset", 
                           path=os.path.join(dirname, 'nginx_opensearch.conf'))
        nginx_asset.grant_read(jumphost_role)
        
        nginx_asset_path = jumphost.user_data.add_s3_download_command(
            bucket=nginx_asset.bucket,
            bucket_key=nginx_asset.s3_object_key
        )

        # Configure jumphost with nginx
        jumphost.user_data.add_commands(
            "yum update -y",
            "amazon-linux-extras install nginx1 -y",
            "mkdir -p /home/ec2-user/assets",
            "cd /home/ec2-user/assets",
            f"mv {nginx_asset_path} nginx_opensearch.conf",
            "openssl req -x509 -nodes -days 365 -newkey rsa:2048 -keyout /etc/nginx/cert.key -out /etc/nginx/cert.crt -subj '/C=US/ST=./L=./O=./CN=.'",
            "cp nginx_opensearch.conf /etc/nginx/conf.d/",
            f"sed -i 's/DOMAIN_ENDPOINT/{os_domain.domain_endpoint}/g' /etc/nginx/conf.d/nginx_opensearch.conf",
            "systemctl enable nginx",
            "systemctl start nginx"
        )

        # Outputs
        CfnOutput(self, f"{OSI_LOAD_CFN_PREFIX}SourceBucketName",
                 value=source_bucket.bucket_name,
                 description="Source S3 bucket name")

        CfnOutput(self, f"{OSI_LOAD_CFN_PREFIX}DLQBucketName",
                 value=dlq_bucket.bucket_name,
                 description="DLQ S3 bucket name")

        CfnOutput(self, f"{OSI_LOAD_CFN_PREFIX}OpenSearchDomainEndpoint",
                 value=os_domain.domain_endpoint,
                 description="OpenSearch domain endpoint")

        CfnOutput(self, f"{OSI_LOAD_CFN_PREFIX}JumphostPublicIP",
                 value=jumphost.instance_public_ip,
                 description="Jumphost public IP for HTTPS access")

        CfnOutput(self, f"{OSI_LOAD_CFN_PREFIX}JumphostInstanceId",
                 value=jumphost.instance_id,
                 description="Jumphost instance ID for SSM Session Manager access")

        CfnOutput(self, f"{OSI_LOAD_CFN_PREFIX}OpenSearchDashboardsURL",
                 value=f"https://{jumphost.instance_public_ip}",
                 description="OpenSearch Dashboards URL via jumphost")

        CfnOutput(self, f"{OSI_LOAD_CFN_PREFIX}MasterUserSecretArn",
                 value=master_user_secret.secret_arn,
                 description="Secrets Manager ARN for OpenSearch credentials - use: aws secretsmanager get-secret-value --secret-id <arn>")

        # CDK-Nag Suppressions for sample code
        self._add_nag_suppressions(vpc, source_bucket, dlq_bucket, os_security_group, 
                                   jumphost_security_group, osi_os_role, jumphost_role, 
                                   jumphost, master_user_secret, os_domain, fgac_lambda)

    def _get_pipeline_config(self, source_bucket, dlq_bucket, os_domain, osi_os_role):
        config_path = os.path.join(os.path.dirname(__file__), 'pipeline_config.json')
        with open(config_path, 'r') as f:
            config_template = f.read()
        
        # Replace placeholders
        config = config_template.replace('${AWS_REGION}', self.region)
        config = config.replace('${OSI_ROLE_ARN}', osi_os_role.role_arn)
        config = config.replace('${SOURCE_BUCKET}', source_bucket.bucket_name)
        config = config.replace('${OPENSEARCH_ENDPOINT}', os_domain.domain_endpoint)
        config = config.replace('${DLQ_BUCKET}', dlq_bucket.bucket_name)
        
        return config

    def _create_osi_os_role(self, source_bucket, dlq_bucket):
        """Create IAM role with least-privilege permissions for OSI and OpenSearch access."""
        role = iam.Role(self, f"{OSI_LOAD_CFN_PREFIX}OSIOpenSearchRole",
                       role_name=f"{OSI_LOAD_RESOURCE_PREFIX}osi-os-role",
                       assumed_by=iam.CompositePrincipal(
                           iam.ServicePrincipal("osis-pipelines.amazonaws.com"),
                           iam.ServicePrincipal("opensearch.amazonaws.com"),
                           iam.ServicePrincipal("lambda.amazonaws.com")
                       ))

        # S3 permissions - scoped to specific buckets
        role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["s3:GetObject", "s3:ListBucket", "s3:PutObject", "s3:DeleteObject"],
            resources=[
                source_bucket.bucket_arn, f"{source_bucket.bucket_arn}/*",
                dlq_bucket.bucket_arn, f"{dlq_bucket.bucket_arn}/*"
            ]
        ))

        # OpenSearch permissions - scoped to specific domain
        os_domain_arn = f"arn:aws:es:{self.region}:{self.account}:domain/{OSI_LOAD_RESOURCE_PREFIX}domain"
        role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "es:ESHttpPost", "es:ESHttpPut", "es:ESHttpGet", "es:ESHttpDelete", "es:ESHttpHead",
                "es:DescribeDomain", "es:DescribeDomains"
            ],
            resources=[os_domain_arn, f"{os_domain_arn}/*"]
        ))

        # Secrets Manager permissions - scoped to specific secret prefix
        role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["secretsmanager:GetSecretValue"],
            resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{OSI_LOAD_RESOURCE_PREFIX}*"]
        ))

        role.add_managed_policy(iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaVPCAccessExecutionRole"))

        return role

    def _get_fgac_lambda_code(self):
        return """
import json
import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection

def handler(event, context):
    # Use existing physical resource ID if available, otherwise use default
    physical_resource_id = event.get('PhysicalResourceId', 'fgac-config')
    
    if event['RequestType'] == 'Delete':
        return {'Status': 'SUCCESS', 'PhysicalResourceId': physical_resource_id}
    
    try:
        props = event['ResourceProperties']
        domain_endpoint = props['DomainEndpoint']
        role_arn = props['RoleArn']
        secret_arn = props['SecretArn']
        
        # Get credentials from Secrets Manager
        secrets_client = boto3.client('secretsmanager')
        secret = secrets_client.get_secret_value(SecretId=secret_arn)
        creds = json.loads(secret['SecretString'])
        
        # Create OpenSearch client with basic auth
        client = OpenSearch(
            hosts=[{'host': domain_endpoint, 'port': 443}],
            http_auth=(creds['username'], creds['password']),
            use_ssl=True,
            verify_certs=False,
            connection_class=RequestsHttpConnection
        )
        
        # Get current all_access role mapping
        try:
            response = client.transport.perform_request('GET', '/_plugins/_security/api/rolesmapping/all_access')
            role_mapping = response['all_access']
        except:
            role_mapping = {"backend_roles": [], "users": []}
        
        # Add OSI role to backend_roles if not already present
        if role_arn not in role_mapping.get('backend_roles', []):
            role_mapping.setdefault('backend_roles', []).append(role_arn)
            
            # Remove read-only fields before updating
            update_mapping = {
                "backend_roles": role_mapping.get('backend_roles', []),
                "users": role_mapping.get('users', []),
                "hosts": role_mapping.get('hosts', []),
                "and_backend_roles": role_mapping.get('and_backend_roles', [])
            }
            
            # Update role mapping
            client.transport.perform_request('PUT', '/_plugins/_security/api/rolesmapping/all_access', body=update_mapping)
        
        return {'Status': 'SUCCESS', 'PhysicalResourceId': physical_resource_id}
    except Exception as e:
        print(f"Error: {str(e)}")
        return {'Status': 'FAILED', 'Reason': str(e), 'PhysicalResourceId': physical_resource_id}
"""

    def _add_nag_suppressions(self, vpc, source_bucket, dlq_bucket, os_security_group,
                              jumphost_security_group, osi_os_role, jumphost_role,
                              jumphost, master_user_secret, os_domain, fgac_lambda):
        """Add cdk-nag suppressions with justifications for sample code."""
        
        # VPC suppressions
        NagSuppressions.add_resource_suppressions(vpc, [
            {"id": "AwsSolutions-VPC7", "reason": "VPC Flow Logs not required for sample code. Users should enable for production deployments."}
        ])

        # S3 bucket suppressions
        for bucket in [source_bucket, dlq_bucket]:
            NagSuppressions.add_resource_suppressions(bucket, [
                {"id": "AwsSolutions-S1", "reason": "S3 access logging not required for sample code. Documented as P1 recommendation for production."}
            ])

        # Security group suppressions
        NagSuppressions.add_resource_suppressions(os_security_group, [
            {"id": "AwsSolutions-EC23", "reason": "OpenSearch security group only allows access from within VPC CIDR, not 0.0.0.0/0."}
        ])
        NagSuppressions.add_resource_suppressions(jumphost_security_group, [
            {"id": "AwsSolutions-EC23", "reason": "Jumphost HTTPS open by default for sample. Restrict with: cdk deploy -c jumphost_allowed_cidr=YOUR_IP/32"}
        ])

        # IAM role suppressions
        NagSuppressions.add_resource_suppressions(osi_os_role, [
            {"id": "AwsSolutions-IAM4", "reason": "AWSLambdaVPCAccessExecutionRole managed policy required for Lambda VPC access."},
            {"id": "AwsSolutions-IAM5", "reason": "Wildcard used for S3 object paths (bucket/*) which is required for object-level operations. Resources are scoped to specific buckets."}
        ], apply_to_children=True)

        NagSuppressions.add_resource_suppressions(jumphost_role, [
            {"id": "AwsSolutions-IAM4", "reason": "AmazonSSMManagedInstanceCore managed policy required for SSM Session Manager access."},
            {"id": "AwsSolutions-IAM5", "reason": "Wildcard permissions from S3 asset grant for nginx config download. Scoped to specific asset bucket."}
        ], apply_to_children=True)

        # Secrets Manager suppression
        NagSuppressions.add_resource_suppressions(master_user_secret, [
            {"id": "AwsSolutions-SMG4", "reason": "Secret rotation not configured for sample code. Documented as recommendation for production."}
        ])

        # OpenSearch domain suppressions
        NagSuppressions.add_resource_suppressions(os_domain, [
            {"id": "AwsSolutions-OS3", "reason": "OpenSearch access policy restricts to specific IAM role ARN, not IP allowlist. FGAC provides additional access control."},
            {"id": "AwsSolutions-OS5", "reason": "Unsigned basic auth enabled for FGAC master user. Access restricted to OSI role via access policy."}
        ], apply_to_children=True)

        # EC2 jumphost suppressions
        NagSuppressions.add_resource_suppressions(jumphost, [
            {"id": "AwsSolutions-EC28", "reason": "Detailed monitoring off by default. Enable with: cdk deploy -c enable_detailed_monitoring=true"},
            {"id": "AwsSolutions-EC29", "reason": "Termination protection not required for sample jumphost. Stack uses RemovalPolicy.DESTROY for easy cleanup."}
        ])

        # Lambda suppressions
        NagSuppressions.add_resource_suppressions(fgac_lambda, [
            {"id": "AwsSolutions-L1", "reason": "Python 3.12 is a recent runtime. Python 3.13 not yet fully supported in Lambda."}
        ])

        # Stack-level suppressions for CDK-generated resources
        NagSuppressions.add_stack_suppressions(self, [
            {"id": "AwsSolutions-IAM4", "reason": "CDK custom resource providers use AWS managed policies by design."},
            {"id": "AwsSolutions-IAM5", "reason": "CDK custom resource providers require wildcard permissions for Lambda invocation."},
            {"id": "AwsSolutions-L1", "reason": "CDK custom resource provider Lambda runtime is managed by CDK."}
        ])
