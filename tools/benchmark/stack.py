"""Resolve the deployed CASCADE resources from the CloudFormation stack.

Nothing account-specific lives in the repo: the state machine ARN, the
bucket, the function names and the log groups are all looked up at run time
from the stack name.
"""
import dataclasses

import boto3

REGION = "us-west-2"
STACK = "cascade-glof"
FUNCTIONS = ("plan", "measure", "route", "report")


@dataclasses.dataclass(frozen=True)
class Stack:
    name: str
    region: str
    state_machine_arn: str
    bucket: str
    functions: dict   # "plan" -> physical function name
    log_groups: dict  # "plan", ..., "statemachine" -> log group name


def lookup(name=STACK, region=REGION):
    cfn = boto3.client("cloudformation", region_name=region)
    stack = cfn.describe_stacks(StackName=name)["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
    functions, log_groups = {}, {}
    for page in cfn.get_paginator("list_stack_resources").paginate(
            StackName=name):
        for resource in page["StackResourceSummaries"]:
            logical = resource["LogicalResourceId"]
            physical = resource["PhysicalResourceId"]
            if resource["ResourceType"] == "AWS::Lambda::Function":
                functions[logical.removesuffix("Function").lower()] = physical
            elif resource["ResourceType"] == "AWS::Logs::LogGroup":
                log_groups[logical.removesuffix("LogGroup").lower()] = physical
    return Stack(name, region, outputs["PipelineArn"],
                 outputs["ResultsBucketName"], functions, log_groups)
