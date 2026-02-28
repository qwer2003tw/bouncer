"""
Bouncer - CloudWatch Embedded Metric Format (EMF) helper

Emits custom business metrics via stdout in EMF format.
CloudWatch automatically picks these up from Lambda logs.
"""

import json
import logging
import time

logger = logging.getLogger(__name__)


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
    # EMF requires raw JSON on stdout for CloudWatch to parse; intentional print()
    print(json.dumps(emf))
