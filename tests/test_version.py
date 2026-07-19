"""Unit tests for version management: SHA-based update check + DDB cache.

Uses pytest with httpx AsyncClient for FastAPI testing.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from unittest.mock import patch, MagicMock
from urllib.error import URLError
from datetime import datetime, timezone, timedelta

from httpx import AsyncClient, ASGITransport
from web.app import app


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# === SHA 判定 4 种 case ===


class TestShaUpdateCheck:
    """测试 has_update 判定逻辑：基于 commit SHA 比对。"""

    @pytest.mark.anyio
    @patch('web.app.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item')
    @patch('web.app.put_item')
    async def test_sha_same_no_update(self, mock_put, mock_get_item, mock_boto_client, mock_urlopen, client):
        """本地 SHA 和远端 SHA 相同 → has_update = False。"""
        mock_get_item.return_value = None  # 无缓存
        mock_cfn = MagicMock()
        mock_boto_client.return_value = mock_cfn
        mock_cfn.describe_stacks.return_value = {'Stacks': []}

        # GitHub 返回相同 SHA
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'abc123def456789'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch.dict(os.environ, {'STACK_NAME': 'test'}), \
             patch('web.app.get_version_info.__module__', 'web.app'), \
             patch('builtins.__import__', wraps=__import__) as mock_import:
            # Mock build_info 模块
            import types
            build_info_mod = types.ModuleType('common.build_info')
            build_info_mod.COMMIT_SHA = 'abc123def456789'
            with patch.dict('sys.modules', {'common.build_info': build_info_mod}):
                resp = await client.get('/api/version')

        assert resp.status_code == 200
        data = resp.json()
        assert data['commit_sha'] == 'abc123def456789'
        assert data['latest_sha'] == 'abc123def456789'
        assert data['has_update'] is False

    @pytest.mark.anyio
    @patch('web.app.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item')
    @patch('web.app.put_item')
    async def test_sha_different_has_update(self, mock_put, mock_get_item, mock_boto_client, mock_urlopen, client):
        """本地 SHA 和远端 SHA 不同 → has_update = True。"""
        mock_get_item.return_value = None
        mock_cfn = MagicMock()
        mock_boto_client.return_value = mock_cfn
        mock_cfn.describe_stacks.return_value = {'Stacks': []}

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'new_commit_sha_999'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        import types
        build_info_mod = types.ModuleType('common.build_info')
        build_info_mod.COMMIT_SHA = 'old_commit_sha_111'
        with patch.dict('sys.modules', {'common.build_info': build_info_mod}), \
             patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        data = resp.json()
        assert data['commit_sha'] == 'old_commit_sha_111'
        assert data['latest_sha'] == 'new_commit_sha_999'
        assert data['has_update'] is True

    @pytest.mark.anyio
    @patch('web.app.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item')
    @patch('web.app.put_item')
    async def test_local_sha_empty_has_update_none(self, mock_put, mock_get_item, mock_boto_client, mock_urlopen, client):
        """本地 SHA 为空（build_info 不存在）→ has_update = None。"""
        mock_get_item.return_value = None
        mock_cfn = MagicMock()
        mock_boto_client.return_value = mock_cfn
        mock_cfn.describe_stacks.return_value = {'Stacks': []}

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'remote_sha_abc'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        # 模拟 ImportError（无 build_info 模块）
        with patch.dict('sys.modules', {'common.build_info': None}), \
             patch.dict(os.environ, {'STACK_NAME': 'test'}):
            # patch.dict with None causes ImportError on from...import
            # 需要用不同方式模拟
            pass

        # 直接删除 sys.modules 中的 build_info 使 import 失败
        import sys as _sys
        _sys.modules.pop('common.build_info', None)
        with patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        data = resp.json()
        assert data['commit_sha'] == ''
        assert data['latest_sha'] == 'remote_sha_abc'
        assert data['has_update'] is None

    @pytest.mark.anyio
    @patch('web.app.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item')
    @patch('web.app.put_item')
    async def test_github_unreachable_has_update_none(self, mock_put, mock_get_item, mock_boto_client, mock_urlopen, client):
        """GitHub 不可达且无缓存 → latest_sha = None, has_update = None。"""
        mock_get_item.return_value = None  # 无缓存
        mock_cfn = MagicMock()
        mock_boto_client.return_value = mock_cfn
        mock_cfn.describe_stacks.return_value = {'Stacks': []}

        mock_urlopen.side_effect = URLError('timeout')

        import types
        build_info_mod = types.ModuleType('common.build_info')
        build_info_mod.COMMIT_SHA = 'local_sha_123'
        with patch.dict('sys.modules', {'common.build_info': build_info_mod}), \
             patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        data = resp.json()
        assert data['commit_sha'] == 'local_sha_123'
        assert data['latest_sha'] is None
        assert data['has_update'] is None


# === DDB 缓存行为 ===


class TestVersionCache:
    """测试 _get_latest_sha_cached 的缓存逻辑。"""

    @pytest.mark.anyio
    @patch('web.app.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item')
    @patch('web.app.put_item')
    async def test_cache_hit_no_github_call(self, mock_put, mock_get_item, mock_boto_client, mock_urlopen, client):
        """1h 内有缓存时不打 GitHub（urlopen 不应被调用）。"""
        now = datetime.now(timezone.utc)
        mock_get_item.return_value = {
            'PK': 'CONFIG', 'SK': 'version_check',
            'latest_sha': 'cached_sha_aaa',
            'checked_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        mock_cfn = MagicMock()
        mock_boto_client.return_value = mock_cfn
        mock_cfn.describe_stacks.return_value = {'Stacks': []}

        import types
        build_info_mod = types.ModuleType('common.build_info')
        build_info_mod.COMMIT_SHA = 'cached_sha_aaa'
        with patch.dict('sys.modules', {'common.build_info': build_info_mod}), \
             patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        data = resp.json()
        assert data['latest_sha'] == 'cached_sha_aaa'
        assert data['has_update'] is False
        # urlopen 不应被调用（缓存命中）
        mock_urlopen.assert_not_called()

    @pytest.mark.anyio
    @patch('web.app.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item')
    @patch('web.app.put_item')
    async def test_cache_expired_calls_github(self, mock_put, mock_get_item, mock_boto_client, mock_urlopen, client):
        """缓存过期（>1h）时重新拉 GitHub。"""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
        mock_get_item.return_value = {
            'PK': 'CONFIG', 'SK': 'version_check',
            'latest_sha': 'old_cached_sha',
            'checked_at': old_time,
        }
        mock_cfn = MagicMock()
        mock_boto_client.return_value = mock_cfn
        mock_cfn.describe_stacks.return_value = {'Stacks': []}

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'fresh_sha_from_github'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        import types
        build_info_mod = types.ModuleType('common.build_info')
        build_info_mod.COMMIT_SHA = 'local_sha'
        with patch.dict('sys.modules', {'common.build_info': build_info_mod}), \
             patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        data = resp.json()
        assert data['latest_sha'] == 'fresh_sha_from_github'
        mock_urlopen.assert_called_once()
        # 缓存应被更新
        mock_put.assert_called()

    @pytest.mark.anyio
    @patch('web.app.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item')
    @patch('web.app.put_item')
    async def test_github_fail_uses_stale_cache(self, mock_put, mock_get_item, mock_boto_client, mock_urlopen, client):
        """GitHub 失败时回退到过期缓存（stale fallback）。"""
        old_time = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        mock_get_item.return_value = {
            'PK': 'CONFIG', 'SK': 'version_check',
            'latest_sha': 'stale_sha_bbb',
            'checked_at': old_time,
        }
        mock_cfn = MagicMock()
        mock_boto_client.return_value = mock_cfn
        mock_cfn.describe_stacks.return_value = {'Stacks': []}

        mock_urlopen.side_effect = URLError('rate limited')

        import types
        build_info_mod = types.ModuleType('common.build_info')
        build_info_mod.COMMIT_SHA = 'local_sha'
        with patch.dict('sys.modules', {'common.build_info': build_info_mod}), \
             patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        data = resp.json()
        # 应回退到 stale 缓存
        assert data['latest_sha'] == 'stale_sha_bbb'
        assert data['has_update'] is True  # local_sha != stale_sha_bbb


# === ImportError 路径 ===


class TestImportErrorPath:
    """测试无 build_info.py 时的降级行为。"""

    @pytest.mark.anyio
    @patch('web.app.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item')
    @patch('web.app.put_item')
    async def test_no_build_info_still_200(self, mock_put, mock_get_item, mock_boto_client, mock_urlopen, client):
        """无 build_info.py 时接口仍返回 200，commit_sha 为空字符串。"""
        mock_get_item.return_value = None
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

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'remote_sha_xyz'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        # 确保 build_info 不可导入
        import sys as _sys
        _sys.modules.pop('common.build_info', None)
        with patch.dict(os.environ, {'STACK_NAME': 'my-stack'}):
            resp = await client.get('/api/version')

        assert resp.status_code == 200
        data = resp.json()
        assert data['commit_sha'] == ''
        assert data['latest_sha'] == 'remote_sha_xyz'
        assert data['has_update'] is None  # 本地 SHA 为空，无法判定
        assert data['allowed_cidrs'] == ['10.0.0.1/32']
        assert data['current_version'] == '1.0.0'
