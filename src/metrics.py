"""
Bouncer - CloudWatch Embedded Metric Format (EMF) helper

Emits custom business metrics via stdout in EMF format.
CloudWatch automatically picks these up from Lambda logs.
"""

import json
import time

from aws_lambda_powertools import Logger

logger = Logger(service="bouncer")


def emit_metric(namespace: str, metric_name: str, value: float, unit: str = 'Count', dimensions: dict = None):
    """Emit CloudWatch EMF metric via stdout.

    dimensions can be:
    - dict: {'Status': 'success', 'Path': 'auto_approve'} (legacy format)
    - list of dicts: [{'Name': 'Decision', 'Value': 'auto_approve'}] (CloudWatch EMF format)
    """
    # Normalize dimensions to dict format for EMF
    if isinstance(dimensions, list):
        # Convert [{'Name': 'k', 'Value': 'v'}, ...] → {'k': 'v', ...}
        dim_dict = {d['Name']: d['Value'] for d in dimensions if 'Name' in d and 'Value' in d}
    else:
        dim_dict = dimensions or {}

    emf = {
        '_aws': {
            'Timestamp': int(time.time() * 1000),
            'CloudWatchMetrics': [{
                'Namespace': namespace,
                'Dimensions': [list(dim_dict.keys())] if dim_dict else [[]],
                'Metrics': [{'Name': metric_name, 'Unit': unit}]
            }]
        },
        metric_name: value
    }
    if dim_dict:
        emf.update(dim_dict)
    # EMF requires raw JSON on stdout for CloudWatch to parse; intentional print()
    print(json.dumps(emf))
