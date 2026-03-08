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
