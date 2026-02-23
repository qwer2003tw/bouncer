"""
Bouncer - CloudWatch Embedded Metric Format (EMF) helper

Emits custom business metrics via stdout in EMF format.
CloudWatch automatically picks these up from Lambda logs.
"""

import json
import time


def emit_metric(namespace: str, metric_name: str, value: float, unit: str = 'Count', dimensions: dict = None):
    """Emit CloudWatch EMF metric via stdout"""
    emf = {
        '_aws': {
            'Timestamp': int(time.time() * 1000),
            'CloudWatchMetrics': [{
                'Namespace': namespace,
                'Dimensions': [list(dimensions.keys())] if dimensions else [[]],
                'Metrics': [{'Name': metric_name, 'Unit': unit}]
            }]
        },
        metric_name: value
    }
    if dimensions:
        emf.update(dimensions)
    print(json.dumps(emf))
