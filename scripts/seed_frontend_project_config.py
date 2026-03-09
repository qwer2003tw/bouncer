#!/usr/bin/env python3
"""
Seed script: write ztp-files frontend config into bouncer-projects DDB table.

Run AFTER deploying the new code, or before if the DDB record is needed first.

Usage (via bouncer_execute with grant session):
    python3 scripts/seed_frontend_project_config.py

Or via AWS CLI:
    aws dynamodb update-item \
        --table-name bouncer-projects \
        --key '{"project_id": {"S": "ztp-files"}}' \
        --update-expression "SET frontend_bucket = :b, frontend_distribution_id = :d, frontend_region = :r, frontend_deploy_role_arn = :a" \
        --expression-attribute-values '{
            ":b": {"S": "ztp-files-dev-frontendbucket-nvvimv31xp3v"},
            ":d": {"S": "E176PW0SA5JF29"},
            ":r": {"S": "us-east-1"},
            ":a": {"S": "arn:aws:iam::190825685292:role/ztp-files-dev-frontend-deploy-role"}
        }' \
        --region us-east-1
"""
import os
import boto3

REGION = os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
TABLE_NAME = os.environ.get('PROJECTS_TABLE', 'bouncer-projects')

FRONTEND_CONFIGS = {
    'ztp-files': {
        'frontend_bucket': 'ztp-files-dev-frontendbucket-nvvimv31xp3v',
        'frontend_distribution_id': 'E176PW0SA5JF29',
        'frontend_region': 'us-east-1',
        'frontend_deploy_role_arn': 'arn:aws:iam::190825685292:role/ztp-files-dev-frontend-deploy-role',
    },
}


def seed(dry_run: bool = False):
    dynamodb = boto3.resource('dynamodb', region_name=REGION)
    table = dynamodb.Table(TABLE_NAME)

    for project_id, config in FRONTEND_CONFIGS.items():
        update_expr = 'SET ' + ', '.join(
            f'#{k} = :{k}' for k in config
        )
        expr_attr_names = {f'#{k}': k for k in config}
        expr_attr_values = {f':{k}': v for k, v in config.items()}

        print(f"[seed] {'(DRY RUN) ' if dry_run else ''}Updating {project_id}:")
        for k, v in config.items():
            print(f"  {k} = {v}")

        if not dry_run:
            table.update_item(
                Key={'project_id': project_id},
                UpdateExpression=update_expr,
                ExpressionAttributeNames=expr_attr_names,
                ExpressionAttributeValues=expr_attr_values,
            )
            print("  -> OK")

    print("[seed] Done.")


if __name__ == '__main__':
    import sys
    dry_run = '--dry-run' in sys.argv
    seed(dry_run=dry_run)
