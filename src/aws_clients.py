"""
Bouncer - AWS Client Factory

Centralised helper for creating boto3 clients that optionally assume an IAM role
before connecting.  Replaces the duplicated STS assume-role → S3 client pattern
that existed in mcp_upload.py (x4) and callbacks.py (x2).
"""

import boto3


def get_s3_client(role_arn=None, session_name='bouncer-s3', region=None):
    """Return a boto3 S3 client, optionally assuming *role_arn* first.

    Parameters
    ----------
    role_arn:
        ARN of the IAM role to assume.  When ``None`` the client is created
        using the current Lambda execution role credentials.
    session_name:
        STS session name used for audit trails.  Defaults to ``'bouncer-s3'``.
    region:
        AWS region name for the S3 client (e.g. ``'us-east-1'``).  When
        ``None`` boto3 resolves the region from the environment / config.

    Returns
    -------
    boto3 S3 client
    """
    if role_arn:
        sts = boto3.client('sts')
        creds = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
        )['Credentials']
        kwargs = dict(
            aws_access_key_id=creds['AccessKeyId'],
            aws_secret_access_key=creds['SecretAccessKey'],
            aws_session_token=creds['SessionToken'],
        )
        if region:
            kwargs['region_name'] = region
        return boto3.client('s3', **kwargs)

    return boto3.client('s3', **(dict(region_name=region) if region else {}))


def get_cloudfront_client(role_arn=None, session_name='bouncer-cf', region=None):
    """Return a boto3 CloudFront client, optionally assuming *role_arn* first.

    Parameters
    ----------
    role_arn:
        ARN of the IAM role to assume.  When ``None`` the client is created
        using the current Lambda execution role credentials.
    session_name:
        STS session name used for audit trails.  Defaults to ``'bouncer-cf'``.
    region:
        AWS region name for the CloudFront client.  When ``None`` boto3
        resolves the region from the environment / config.

    Returns
    -------
    boto3 CloudFront client
    """
    if role_arn:
        sts = boto3.client('sts')
        creds = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
        )['Credentials']
        kwargs = dict(
            aws_access_key_id=creds['AccessKeyId'],
            aws_secret_access_key=creds['SecretAccessKey'],
            aws_session_token=creds['SessionToken'],
        )
        if region:
            kwargs['region_name'] = region
        return boto3.client('cloudfront', **kwargs)

    return boto3.client('cloudfront', **(dict(region_name=region) if region else {}))


# ============================================================================
# Generic Client Factory (Sprint 88 #370)
# ============================================================================
# Unified boto3 client creation with caching for all services except S3/CloudFront
# (which require role assumption support via get_s3_client/get_cloudfront_client)

_clients = {}


def get_client(service: str, region: str = None):
    """Get or create a cached boto3 client.

    Args:
        service: AWS service name (e.g., 'sts', 'dynamodb', 'secretsmanager')
        region: AWS region (defaults to DEFAULT_REGION from constants)

    Returns:
        boto3 client for the specified service
    """
    from constants import DEFAULT_REGION
    region = region or DEFAULT_REGION
    key = f"{service}:{region}"
    if key not in _clients:
        _clients[key] = boto3.client(service, region_name=region)
    return _clients[key]


def reset_clients():
    """Reset all cached clients. Use in test teardown."""
    _clients.clear()
