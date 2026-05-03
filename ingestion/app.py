# SPDX-License-Identifier: MIT-0
import os

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks, NagSuppressions

from osi_load.osi_load_stack import OsiLoadStack

# Check required environment variables
account = os.getenv('CDK_DEFAULT_ACCOUNT')
region = os.getenv('CDK_DEFAULT_REGION')

if not account:
    raise ValueError("CDK_DEFAULT_ACCOUNT environment variable must be set")
if not region:
    raise ValueError("CDK_DEFAULT_REGION environment variable must be set")

app = cdk.App()
stack = OsiLoadStack(app, "OsiLoadStack",
    env=cdk.Environment(account=account, region=region),
    )

# Add cdk-nag AWS Solutions checks
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

app.synth()
