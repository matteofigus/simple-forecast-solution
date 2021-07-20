from aws_cdk import core as cdk

from aws_cdk import (
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_sagemaker as sm,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
    aws_lambda as lambda_,
    core
)

#
# Lifecycle config raw strings
#

# This is run *each time* the notebook instance is started
LCC_ONSTART_STR = """#!/bin/bash
set -e
initctl restart jupyter-server --no-wait

# Get the notebook URL
DASHBOARD_URL=https://$(aws sagemaker describe-notebook-instance \
    --notebook-instance-name {notebook_instance_name} \
    --query "Url" \
    --output text)/proxy/8501/

# Get the instructions ipynb notebook URL (email to user)
LANDING_PAGE_URL=https://$DASHBOARD_URL/lab/tree/SFS_Landing_Page.ipynb

# Send SNS email
aws lambda invoke --function-name {sns_lambda_function} \
    --payload '{{"landing_page_url": "'$LANDING_PAGE_URL'", "dashboard_url": "'$DASHBOARD_URL'"}}' \
    /dev/stdout

#
# Start SFS dashboard in the background
#
source /home/ec2-user/anaconda3/bin/activate sfs
streamlit hello &
"""

# This is run *once* ever, upon the *creation* of the notebook
LCC_ONCREATE_STR = """#!/bin/bash
set -e
export LC_ALL=en_US.utf-8 && export LANG=en_US.utf-8
source /home/ec2-user/anaconda3/bin/activate JupyterSystemEnv

#
# Install SFS
#
/home/ec2-user/anaconda3/bin/conda create -q -n sfs python=3.8.10
source /home/ec2-user/anaconda3/bin/activate sfs

# Install the dashboard
git clone --recurse-submodules https://github.com/aws-samples/simple-forecast-solution.git
cd ./simple-forecast-solution
pip install -q -e .

# Copy the landing page to the user SFS workspace
cp -rp ./sm-bootstrap/SFS_Landing_Page.ipynb /home/ec2-user/SageMaker/
chmod a+rwx /home/ec2-user/SageMaker/SFS_Landing_Page.ipynb

# Install the lambdamap python library
cd ./sfs/lambdamap/
pip install -q -e .

# Install aws-cdk
curl -sL https://rpm.nodesource.com/setup_14.x | bash - \
    && yum install -y nodejs \
    && npm install -g aws-cdk@1.114.0

# Install the SfsLambdaMapStack
cd ./lambdamap_cdk/
pip install -q -r ./requirements.txt
cdk deploy --require-approval never \
    --context stack_name={sfs_lambdamap_stack_name} \
    --context function_name=SfsLambdaMapFunction \
    --context extra_cmds='pip install -q git+https://github.com/aws-samples/simple-forecast-solution.git#egg=sfs'

#
# Upgrade jupyter-server-proxy
#
source /home/ec2-user/anaconda3/bin/activate JupyterSystemEnv

pip uninstall -q --yes nbserverproxy || true
pip install -q --upgrade jupyter-server-proxy

sudo -u ec2-user mkdir -p /home/ec2-user/SageMaker/output/
"""

# This is the lambda that sends the notification email to the user once
# the dashboard is deployed, it contains the URL to the landing page
# sagemaker notebook.
SNS_EMAIL_LAMBDA_INLINE = """import os
import json
import boto3
import textwrap

def lambda_handler(event, context):
    landing_page_url = event["landing_page_url"]
    dashboard_url = event["dashboard_url"]

    client = boto3.client("sns")
    response = client.publish(
        TopicArn=os.environ["TOPIC_ARN"],
        Subject="Your Amazon SFS Dashboard is Ready!",
        Message=textwrap.dedent(f'''
        Congratulations!
        
        Amazon SFS has been successfully deployed into your AWS account.
        
        Your Amazon SFS landing page is available here:
        ‣ {landing_page_url}
        
        You Amazon SFS dashboard is available here:
        ‣ {dashboard_url}

        Sincerely,
        The Amazon SFS Team
        ‣ github: https://github.com/aws-samples/simple-forecast-solution
        '''))
    
    return response
"""


class BootstrapStack(cdk.Stack):
    def __init__(self, scope: cdk.Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        email_address = core.CfnParameter(self, "emailAddress")
        instance_type = core.CfnParameter(self, "instanceType")

        #
        # SNS topic for email notification
        #
        topic = sns.Topic(self, f"{construct_id}-NotificationTopic",
                topic_name=f"{construct_id}-NotificationTopic")

        topic.add_subscription(
            subscriptions.EmailSubscription(email_address.value_as_string))

        sns_lambda_role = iam.Role(
            self,
            f"{construct_id}-SnsEmailLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSNSFullAccess")
            ])

        sns_lambda = lambda_.Function(self, f"{construct_id}-SnsEmailLambda",
            runtime=lambda_.Runtime.PYTHON_3_8,
            environment={"TOPIC_ARN": f"arn:aws:sns:{self.region}:{self.account}:{topic.topic_name}"},
            code=lambda_.Code.from_inline(SNS_EMAIL_LAMBDA_INLINE),
            handler="index.lambda_handler",
            role=sns_lambda_role)

        #
        # Notebook lifecycle configuration
        #
        notebook_instance_name=f"{construct_id}-NotebookInstance"

        lcc_onstart_obj = \
            sm.CfnNotebookInstanceLifecycleConfig \
              .NotebookInstanceLifecycleHookProperty(
                  content=core.Fn.base64(LCC_ONSTART_STR.format(
                      notebook_instance_name=notebook_instance_name,
                      sns_lambda_function=sns_lambda.function_name)))

        lcc_oncreate_obj = \
            sm.CfnNotebookInstanceLifecycleConfig \
              .NotebookInstanceLifecycleHookProperty(
                  content=core.Fn.base64(LCC_ONCREATE_STR.format(
                      sfs_lambdamap_stack_name=f"{construct_id}-SfsLambdaMapStack")))

        lcc = sm.CfnNotebookInstanceLifecycleConfig(
            self,
            f"{construct_id}-NotebookLifecycleConfig",
            on_create=[lcc_oncreate_obj],
            on_start=[lcc_onstart_obj])

        #
        # Notebook role
        #
        sm_role = iam.Role(
            self,
            f"{construct_id}-NotebookRole",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSageMakerFullAccess"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AWSCloudFormationFullAccess"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AWSLambda_FullAccess"),
                iam.ManagedPolicy.from_aws_managed_policy_name("EC2InstanceProfileForImageBuilderECRContainerBuilds"),
                iam.ManagedPolicy.from_aws_managed_policy_name("IAMFullAccess")
            ])

        #
        # Notebook instance
        #
        sm.CfnNotebookInstance(
            self,
            f"{construct_id}-NotebookInstance",
            role_arn=sm_role.role_arn,
            instance_type="ml.t3.large",
            notebook_instance_name=notebook_instance_name,
            lifecycle_config_name=lcc.attr_notebook_instance_lifecycle_config_name)
