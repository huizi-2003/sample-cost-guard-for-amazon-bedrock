"""Unit tests for version management: _compare_versions + GET /api/version endpoint.

Uses pytest with httpx AsyncClient for FastAPI testing.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from unittest.mock import patch, MagicMock
from urllib.error import URLError

from httpx import AsyncClient, ASGITransport
from web.app import app, _compare_versions


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# === _compare_versions 纯函数测试 ===


class TestCompareVersions:
    def test_upgrade_available(self):
        assert _compare_versions('1.0.0', '1.1.0') is True

    def test_same_version(self):
        assert _compare_versions('1.0.0', '1.0.0') is False

    def test_downgrade_not_update(self):
        assert _compare_versions('2.0.0', '1.9.9') is False

    def test_patch_upgrade(self):
        assert _compare_versions('1.0.0', '1.0.1') is True

    def test_major_upgrade(self):
        assert _compare_versions('1.9.9', '2.0.0') is True

    def test_non_semver_fallback_returns_false(self):
        """非语义版本解析失败时保守返回 False（不提示更新）。"""
        assert _compare_versions('abc', 'def') is False
        assert _compare_versions('abc', 'abc') is False
        assert _compare_versions('2.0', '1.9.beta') is False


# === GET /api/version 端点测试 ===


class TestVersionEndpoint:
    @pytest.mark.anyio
    @patch('web.app.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    async def test_version_all_success(self, mock_boto_client, mock_urlopen, client):
        """正常路径：CFn 和 GitHub 都成功。"""
        # Mock CFn
        mock_cfn = MagicMock()
        mock_boto_client.return_value = mock_cfn
        mock_cfn.describe_stacks.return_value = {
            'Stacks': [{
                'LastUpdatedTime': MagicMock(strftime=MagicMock(return_value='2026-07-18T08:00:00Z')),
                'Parameters': [
                    {'ParameterKey': 'AllowedCidrs', 'ParameterValue': '1.2.3.4/32,5.6.7.0/24'},
                    {'ParameterKey': 'Version', 'ParameterValue': '1721300000'},
                ],
            }]
        }

        # Mock GitHub urlopen
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'VERSION = "9.9.9"\n'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch.dict(os.environ, {'STACK_NAME': 'my-stack', 'GITHUB_OWNER': 'test-owner', 'GITHUB_REPO': 'test-repo', 'GITHUB_BRANCH': 'dev'}):
            resp = await client.get('/api/version')

        assert resp.status_code == 200
        data = resp.json()
        assert data['current_version'] == '1.0.0'
        assert data['latest_version'] == '9.9.9'
        assert data['has_update'] is True
        assert data['stack_name'] == 'my-stack'
        assert data['allowed_cidrs'] == ['1.2.3.4/32', '5.6.7.0/24']
        assert data['last_updated'] == '2026-07-18T08:00:00Z'

        # 验证 GitHub URL 使用了环境变量
        call_args = mock_urlopen.call_args
        # urlopen 第一个参数是 Request 对象
        request_obj = call_args[0][0]
        assert 'test-owner' in request_obj.full_url
        assert 'test-repo' in request_obj.full_url
        assert 'dev' in request_obj.full_url

    @pytest.mark.anyio
    @patch('web.app.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    async def test_cfn_denied(self, mock_boto_client, mock_urlopen, client):
        """CFn 权限不足时降级：仍返回 200，stack_name 有默认值，allowed_cidrs 为空。"""
        from botocore.exceptions import ClientError

        mock_cfn = MagicMock()
        mock_boto_client.return_value = mock_cfn
        mock_cfn.describe_stacks.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'not authorized'}},
            'DescribeStacks'
        )

        # GitHub 仍正常
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'VERSION = "1.0.0"\n'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch.dict(os.environ, {'STACK_NAME': 'test-stack'}, clear=False):
            resp = await client.get('/api/version')

        assert resp.status_code == 200
        data = resp.json()
        assert data['stack_name'] == 'test-stack'
        assert data['allowed_cidrs'] == []
        assert data['last_updated'] is None
        assert data['latest_version'] == '1.0.0'
        assert data['has_update'] is False

    @pytest.mark.anyio
    @patch('web.app.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    async def test_github_unreachable(self, mock_boto_client, mock_urlopen, client):
        """GitHub 不可达时降级：latest_version 和 has_update 为 None，仍 200。"""
        # CFn 正常
        mock_cfn = MagicMock()
        mock_boto_client.return_value = mock_cfn
        mock_cfn.describe_stacks.return_value = {
            'Stacks': [{
                'LastUpdatedTime': MagicMock(strftime=MagicMock(return_value='2026-07-19T10:00:00Z')),
                'Parameters': [
                    {'ParameterKey': 'AllowedCidrs', 'ParameterValue': '10.0.0.1/32'},
                ],
            }]
        }

        # GitHub 超时
        mock_urlopen.side_effect = URLError('timeout')

        with patch.dict(os.environ, {'STACK_NAME': 'my-guard'}, clear=False):
            resp = await client.get('/api/version')

        assert resp.status_code == 200
        data = resp.json()
        assert data['latest_version'] is None
        assert data['has_update'] is None
        assert data['stack_name'] == 'my-guard'
        assert data['allowed_cidrs'] == ['10.0.0.1/32']
