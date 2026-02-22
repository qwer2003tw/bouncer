# Bouncer Deployer - Makefile
# Manage deploy scripts and infrastructure

ARTIFACTS_BUCKET := sam-deployer-artifacts-190825685292
DEPLOY_SCRIPT_S3_KEY := deployer-scripts/sam_deploy.py
DEPLOY_SCRIPT_LOCAL := scripts/sam_deploy.py

.PHONY: upload-deploy-script verify-deploy-script

## Upload sam_deploy.py to S3 for CodeBuild to download
upload-deploy-script:
	@echo "Uploading $(DEPLOY_SCRIPT_LOCAL) to s3://$(ARTIFACTS_BUCKET)/$(DEPLOY_SCRIPT_S3_KEY)..."
	aws s3 cp $(DEPLOY_SCRIPT_LOCAL) "s3://$(ARTIFACTS_BUCKET)/$(DEPLOY_SCRIPT_S3_KEY)"
	@echo "✅ Upload complete. Verifying..."
	@aws s3api head-object --bucket $(ARTIFACTS_BUCKET) --key $(DEPLOY_SCRIPT_S3_KEY) --query '{Size: ContentLength, LastModified: LastModified, ETag: ETag}' --output table

## Verify the S3 copy matches the local file
verify-deploy-script:
	@echo "Comparing local vs S3 version..."
	@LOCAL_MD5=$$(md5sum $(DEPLOY_SCRIPT_LOCAL) | cut -d' ' -f1) && \
	aws s3 cp "s3://$(ARTIFACTS_BUCKET)/$(DEPLOY_SCRIPT_S3_KEY)" /tmp/sam_deploy_s3_verify.py --quiet && \
	S3_MD5=$$(md5sum /tmp/sam_deploy_s3_verify.py | cut -d' ' -f1) && \
	if [ "$$LOCAL_MD5" = "$$S3_MD5" ]; then \
		echo "✅ Files match (MD5: $$LOCAL_MD5)"; \
	else \
		echo "❌ Files differ! Local: $$LOCAL_MD5, S3: $$S3_MD5"; \
		echo "Run 'make upload-deploy-script' to sync."; \
		exit 1; \
	fi
