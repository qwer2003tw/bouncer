"""Tests for template_diff_analyzer.py (#123)"""
import json
from unittest.mock import patch, MagicMock
import pytest
import sys
import os

# xdist isolation: keep all template_diff_analyzer tests in the same worker
pytestmark = pytest.mark.xdist_group("template_diff_analyzer")

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from template_diff_analyzer import analyze_template_diff, TemplateDiffResult


class TestTemplateDiffAnalyzer:
    """Template diff analyzer tests"""

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_no_template_changes(self, mock_github_api, mock_get_pat):
        """template.yaml 無變動 → is_safe=True, has_template_changes=False"""
        mock_get_pat.return_value = 'test-pat'

        # Mock commits API
        mock_github_api.side_effect = [
            {
                'sha': 'head123',
                'parents': [{'sha': 'base456'}]
            },
            {
                'files': [
                    {'filename': 'src/lambda.py', 'patch': '+new code'}
                ]
            }
        ]

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is True
        assert result.has_template_changes is False
        assert result.diff_summary == 'template.yaml 無變動 → code-only'
        assert result.error == ''
        assert result.high_risk_findings == []

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_template_changes_no_high_risk(self, mock_github_api, mock_get_pat):
        """template.yaml 有變動但無高風險 → is_safe=True"""
        mock_get_pat.return_value = 'test-pat'

        template_patch = """@@ -10,3 +10,5 @@
 Resources:
   MyFunction:
     Type: AWS::Lambda::Function
+    Properties:
+      MemorySize: 512"""

        mock_github_api.side_effect = [
            {
                'sha': 'head123',
                'parents': [{'sha': 'base456'}]
            },
            {
                'files': [
                    {'filename': 'template.yaml', 'patch': template_patch}
                ]
            }
        ]

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is True
        assert result.has_template_changes is True
        assert 'template.yaml 有變動但無高風險項目 → auto-approve' in result.diff_summary
        assert result.error == ''
        assert result.high_risk_findings == []

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_principal_star_added(self, mock_github_api, mock_get_pat):
        """新增 Principal:* → is_safe=False, findings set"""
        mock_get_pat.return_value = 'test-pat'

        template_patch = """@@ -10,3 +10,7 @@
 Resources:
   MyFunction:
     Type: AWS::Lambda::Function
+  MyFunctionUrl:
+    Properties:
+      AuthType: AWS_IAM
+      Principal: "*" """

        mock_github_api.side_effect = [
            {
                'sha': 'head123',
                'parents': [{'sha': 'base456'}]
            },
            {
                'files': [
                    {'filename': 'template.yaml', 'patch': template_patch}
                ]
            }
        ]

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is True
        assert len(result.high_risk_findings) == 1
        assert 'Principal:*' in result.high_risk_findings[0]
        assert result.error == ''

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_auth_type_none_added(self, mock_github_api, mock_get_pat):
        """新增 AuthType:NONE → is_safe=False"""
        mock_get_pat.return_value = 'test-pat'

        template_patch = """@@ -10,3 +10,5 @@
 Resources:
   MyFunctionUrl:
     Properties:
+      AuthType: NONE
+      Cors: true"""

        mock_github_api.side_effect = [
            {
                'sha': 'head123',
                'parents': [{'sha': 'base456'}]
            },
            {
                'files': [
                    {'filename': 'template.yaml', 'patch': template_patch}
                ]
            }
        ]

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is True
        assert len(result.high_risk_findings) == 1
        assert 'AuthType:NONE' in result.high_risk_findings[0]

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_s3_public_access_disabled(self, mock_github_api, mock_get_pat):
        """S3 public access controls disabled → is_safe=False"""
        mock_get_pat.return_value = 'test-pat'

        template_patch = """@@ -10,3 +10,8 @@
 Resources:
   MyBucket:
     Type: AWS::S3::Bucket
+    Properties:
+      PublicAccessBlockConfiguration:
+        BlockPublicAcls: false
+        BlockPublicPolicy: false
+        RestrictPublicBuckets: false"""

        mock_github_api.side_effect = [
            {
                'sha': 'head123',
                'parents': [{'sha': 'base456'}]
            },
            {
                'files': [
                    {'filename': 'infra/template.yaml', 'patch': template_patch}
                ]
            }
        ]

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is True
        assert len(result.high_risk_findings) == 3  # BlockPublicAcls, BlockPublicPolicy, RestrictPublicBuckets
        assert any('BlockPublicAcls' in f for f in result.high_risk_findings)
        assert any('BlockPublicPolicy' in f for f in result.high_risk_findings)
        assert any('RestrictPublicBuckets' in f for f in result.high_risk_findings)

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_github_api_fails(self, mock_github_api, mock_get_pat):
        """GitHub API 失敗 → is_safe=False, error set"""
        mock_get_pat.return_value = 'test-pat'

        from urllib.error import HTTPError
        mock_github_api.side_effect = HTTPError('url', 404, 'Not Found', {}, None)

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is False
        assert 'GitHub API error: 404' in result.error

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_cannot_parse_git_repo(self, mock_github_api, mock_get_pat):
        """git_repo 格式錯誤 → is_safe=False, error set"""
        mock_get_pat.return_value = 'test-pat'

        result = analyze_template_diff('invalid', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is False
        assert 'Cannot parse git_repo' in result.error

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_first_commit_no_base(self, mock_github_api, mock_get_pat):
        """First commit (no parent) → is_safe=False, error set"""
        mock_get_pat.return_value = 'test-pat'

        # No parents → head_sha == base_sha
        mock_github_api.return_value = {
            'sha': 'head123',
            'parents': []
        }

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is False
        assert 'Cannot determine base commit' in result.error

    @patch('template_diff_analyzer._get_github_pat')
    def test_secretsmanager_fails(self, mock_get_pat):
        """Secrets Manager 失敗 → is_safe=False, error set"""
        mock_get_pat.side_effect = Exception('SecretNotFound')

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is False
        assert 'Analysis failed' in result.error
        assert 'SecretNotFound' in result.error

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_git_repo_with_git_suffix(self, mock_github_api, mock_get_pat):
        """git_repo with .git suffix → correctly parsed"""
        mock_get_pat.return_value = 'test-pat'

        mock_github_api.side_effect = [
            {
                'sha': 'head123',
                'parents': [{'sha': 'base456'}]
            },
            {
                'files': []
            }
        ]

        result = analyze_template_diff('owner/repo.git', 'main', 'test-secret')

        assert result.is_safe is True
        assert mock_github_api.call_count == 2
        # Verify owner/repo was correctly parsed (repo.git suffix removed)
        first_call_url = mock_github_api.call_args_list[0][0][0]
        assert '/repos/owner/repo/' in first_call_url
        assert '/repos/owner/repo.git/' not in first_call_url

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_iam_role_addition(self, mock_github_api, mock_get_pat):
        """新增 IAM Role → is_safe=False (s51-002)"""
        mock_get_pat.return_value = 'test-pat'

        template_patch = """@@ -10,3 +10,8 @@
 Resources:
   MyFunction:
     Type: AWS::Lambda::Function
+  MyRole:
+    Type: AWS::IAM::Role
+    Properties:
+      RoleName: MyLambdaRole"""

        mock_github_api.side_effect = [
            {
                'sha': 'head123',
                'parents': [{'sha': 'base456'}]
            },
            {
                'files': [
                    {'filename': 'template.yaml', 'patch': template_patch}
                ]
            }
        ]

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is True
        assert len(result.high_risk_findings) >= 1
        assert any('IAM 資源變更' in f for f in result.high_risk_findings)

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_iam_assume_role_policy(self, mock_github_api, mock_get_pat):
        """新增 AssumeRolePolicyDocument → is_safe=False (s51-002)"""
        mock_get_pat.return_value = 'test-pat'

        template_patch = """@@ -10,3 +10,5 @@
 Resources:
   MyRole:
     Properties:
+      AssumeRolePolicyDocument:
+        Version: '2012-10-17'"""

        mock_github_api.side_effect = [
            {
                'sha': 'head123',
                'parents': [{'sha': 'base456'}]
            },
            {
                'files': [
                    {'filename': 'template.yaml', 'patch': template_patch}
                ]
            }
        ]

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is True
        assert any('IAM Trust relationship' in f for f in result.high_risk_findings)

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_security_group_open_ipv4(self, mock_github_api, mock_get_pat):
        """Security Group 開放 0.0.0.0/0 → is_safe=False (s51-003)"""
        mock_get_pat.return_value = 'test-pat'

        template_patch = """@@ -10,3 +10,7 @@
 Resources:
   MySecurityGroup:
     Type: AWS::EC2::SecurityGroup
+    Properties:
+      SecurityGroupIngress:
+        - IpProtocol: tcp
+          CidrIp: 0.0.0.0/0"""

        mock_github_api.side_effect = [
            {
                'sha': 'head123',
                'parents': [{'sha': 'base456'}]
            },
            {
                'files': [
                    {'filename': 'template.yaml', 'patch': template_patch}
                ]
            }
        ]

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is True
        assert any('0.0.0.0/0' in f for f in result.high_risk_findings)

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_security_group_open_ipv6(self, mock_github_api, mock_get_pat):
        """Security Group 開放 ::/0 → is_safe=False (s51-003)"""
        mock_get_pat.return_value = 'test-pat'

        template_patch = """@@ -10,3 +10,5 @@
 Resources:
   MySecurityGroup:
     Properties:
+      SecurityGroupIngress:
+        - CidrIpv6: "::/0" """

        mock_github_api.side_effect = [
            {
                'sha': 'head123',
                'parents': [{'sha': 'base456'}]
            },
            {
                'files': [
                    {'filename': 'template.yaml', 'patch': template_patch}
                ]
            }
        ]

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is True
        assert any('::/0' in f for f in result.high_risk_findings)

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_kms_key_addition(self, mock_github_api, mock_get_pat):
        """新增 KMS Key → is_safe=False (s51-005)"""
        mock_get_pat.return_value = 'test-pat'

        template_patch = """@@ -10,3 +10,7 @@
 Resources:
   MyFunction:
     Type: AWS::Lambda::Function
+  MyKey:
+    Type: AWS::KMS::Key
+    Properties:
+      Description: My encryption key"""

        mock_github_api.side_effect = [
            {
                'sha': 'head123',
                'parents': [{'sha': 'base456'}]
            },
            {
                'files': [
                    {'filename': 'template.yaml', 'patch': template_patch}
                ]
            }
        ]

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is True
        assert any('KMS Key' in f for f in result.high_risk_findings)

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_lambda_env_secret_pattern(self, mock_github_api, mock_get_pat):
        """Lambda env 疑似明文 secret → is_safe=False (s51-006)"""
        mock_get_pat.return_value = 'test-pat'

        template_patch = """@@ -10,3 +10,6 @@
 Resources:
   MyFunction:
     Properties:
+      Environment:
+        Variables:
+          API_KEY: sk-1234567890abcdef"""

        mock_github_api.side_effect = [
            {
                'sha': 'head123',
                'parents': [{'sha': 'base456'}]
            },
            {
                'files': [
                    {'filename': 'template.yaml', 'patch': template_patch}
                ]
            }
        ]

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is True
        assert any('疑似明文 secret' in f for f in result.high_risk_findings)

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_ec2_public_ip_enabled(self, mock_github_api, mock_get_pat):
        """EC2 分配公開 IP → is_safe=False (s51-007)"""
        mock_get_pat.return_value = 'test-pat'

        template_patch = """@@ -10,3 +10,6 @@
 Resources:
   MyInstance:
     Type: AWS::EC2::Instance
+    Properties:
+      NetworkInterfaces:
+        - AssociatePublicIpAddress: true"""

        mock_github_api.side_effect = [
            {
                'sha': 'head123',
                'parents': [{'sha': 'base456'}]
            },
            {
                'files': [
                    {'filename': 'template.yaml', 'patch': template_patch}
                ]
            }
        ]

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is True
        assert any('分配公開 IP' in f for f in result.high_risk_findings)

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_subnet_auto_public_ip(self, mock_github_api, mock_get_pat):
        """Subnet 自動分配公開 IP → is_safe=False (s51-007)"""
        mock_get_pat.return_value = 'test-pat'

        template_patch = """@@ -10,3 +10,5 @@
 Resources:
   MySubnet:
     Type: AWS::EC2::Subnet
+    Properties:
+      MapPublicIpOnLaunch: true"""

        mock_github_api.side_effect = [
            {
                'sha': 'head123',
                'parents': [{'sha': 'base456'}]
            },
            {
                'files': [
                    {'filename': 'template.yaml', 'patch': template_patch}
                ]
            }
        ]

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is True
        assert any('自動分配公開 IP' in f for f in result.high_risk_findings)

    @patch('template_diff_analyzer._get_github_pat')
    @patch('template_diff_analyzer._github_api')
    def test_existing_patterns_still_work(self, mock_github_api, mock_get_pat):
        """Verify existing patterns (Principal:*, AuthType:NONE, S3 public) still work after adding new ones"""
        mock_get_pat.return_value = 'test-pat'

        template_patch = """@@ -10,3 +10,10 @@
 Resources:
   MyFunctionUrl:
     Properties:
+      Principal: "*"
+      AuthType: NONE
+  MyBucket:
+    Type: AWS::S3::Bucket
+    Properties:
+      PublicAccessBlockConfiguration:
+        BlockPublicAcls: false"""

        mock_github_api.side_effect = [
            {
                'sha': 'head123',
                'parents': [{'sha': 'base456'}]
            },
            {
                'files': [
                    {'filename': 'template.yaml', 'patch': template_patch}
                ]
            }
        ]

        result = analyze_template_diff('owner/repo', 'main', 'test-secret')

        assert result.is_safe is False
        assert result.has_template_changes is True
        # Should detect all 3 patterns
        assert len(result.high_risk_findings) == 3
        assert any('Principal:*' in f for f in result.high_risk_findings)
        assert any('AuthType:NONE' in f for f in result.high_risk_findings)
        assert any('BlockPublicAcls' in f for f in result.high_risk_findings)
