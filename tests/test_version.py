"""版本管理测试：Release 通道的更新检查 + DDB 缓存 + 健康检查。

自动升级只跟随 GitHub Release，所以这里验证的是 /releases/latest 这条路径，
而不是旧的"比对分支最新 commit"。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import json
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest
from httpx import ASGITransport, AsyncClient

from web.app import app


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture(autouse=True)
def _no_build_info():
    """默认清掉 build_info，各测试按需注入，避免相互污染。"""
    sys.modules.pop('common.build_info', None)
    yield
    sys.modules.pop('common.build_info', None)


def fake_build_info(commit_sha='', release_tag='', build_time=''):
    mod = types.ModuleType('common.build_info')
    mod.COMMIT_SHA = commit_sha
    mod.RELEASE_TAG = release_tag
    mod.BUILD_TIME = build_time
    return mod


def release_payload(tag='v20260802', notes='- 修复账单汇总超时', published_at='2026-08-02T03:00:00Z'):
    return json.dumps({
        'tag_name': tag,
        'name': tag,
        'body': notes,
        'published_at': published_at,
        # 故意设成分支名：真实 API 就是这样，代码必须再解析一次 tag 才能拿到 SHA
        'target_commitish': 'main',
    }).encode()


def gh_responses(tag='v20260802', sha='newsha0000000000', notes='- 修复账单汇总超时'):
    """构造 common.release 的两次 HTTP 调用：先 releases/latest，再 commits/{tag}。"""
    r1 = MagicMock()
    r1.read.return_value = release_payload(tag=tag, notes=notes)
    r2 = MagicMock()
    r2.read.return_value = sha.encode()
    m1, m2 = MagicMock(), MagicMock()
    m1.__enter__ = MagicMock(return_value=r1)
    m1.__exit__ = MagicMock(return_value=False)
    m2.__enter__ = MagicMock(return_value=r2)
    m2.__exit__ = MagicMock(return_value=False)
    return [m1, m2]


def _cfn_empty():
    mock_cfn = MagicMock()
    mock_cfn.describe_stacks.return_value = {'Stacks': []}
    return mock_cfn


DEFAULT_CFG = {
    'enabled': True, 'last_check_at': '', 'last_status': '', 'last_error': '',
    'last_known_good_sha': '', 'current_upgrade_id': '',
}


class TestReleaseUpdateCheck:
    """has_update 判定：基于最新 Release 的 commit SHA。"""

    @pytest.mark.anyio
    @patch('web.app.get_auto_upgrade_config', return_value=DEFAULT_CFG)
    @patch('common.release.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item', return_value=None)
    @patch('web.app.put_item')
    async def test_same_release_no_update(self, mock_put, mock_get_item, mock_boto,
                                          mock_urlopen, mock_cfg, client):
        """当前 SHA 与最新 Release 的 SHA 相同 → has_update = False。"""
        mock_boto.return_value = _cfn_empty()
        mock_urlopen.side_effect = gh_responses(tag='v20260802', sha='abc123def456789')

        with patch.dict('sys.modules', {'common.build_info': fake_build_info('abc123def456789', 'v20260802')}), \
             patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        assert resp.status_code == 200
        data = resp.json()
        assert data['commit_sha'] == 'abc123def456789'
        assert data['release_tag'] == 'v20260802'
        assert data['latest_tag'] == 'v20260802'
        assert data['latest_sha'] == 'abc123def456789'
        assert data['has_update'] is False
        assert data['no_release'] is False

    @pytest.mark.anyio
    @patch('web.app.get_auto_upgrade_config', return_value=DEFAULT_CFG)
    @patch('common.release.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item', return_value=None)
    @patch('web.app.put_item')
    async def test_new_release_has_update(self, mock_put, mock_get_item, mock_boto,
                                          mock_urlopen, mock_cfg, client):
        """有更新的 Release → has_update = True，并返回新 tag。"""
        mock_boto.return_value = _cfn_empty()
        mock_urlopen.side_effect = gh_responses(tag='v20260809', sha='newsha999')

        with patch.dict('sys.modules', {'common.build_info': fake_build_info('oldsha111', 'v20260802')}), \
             patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        data = resp.json()
        assert data['release_tag'] == 'v20260802'
        assert data['latest_tag'] == 'v20260809'
        assert data['has_update'] is True

    @pytest.mark.anyio
    @patch('web.app.get_auto_upgrade_config', return_value=DEFAULT_CFG)
    @patch('common.release.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item', return_value=None)
    @patch('web.app.put_item')
    async def test_no_build_info_treated_as_outdated(self, mock_put, mock_get_item, mock_boto,
                                                     mock_urlopen, mock_cfg, client):
        """无 build_info（本地开发/旧部署）→ 视为过期，has_update = True。

        与 Updater 的处理保持一致：本地版本未知时不是"无法判断"，而是
        "默认自己不是最新"，这样旧部署能自愈到最新 Release。
        """
        mock_boto.return_value = _cfn_empty()
        mock_urlopen.side_effect = gh_responses(sha='remote_sha_abc')

        with patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        assert resp.status_code == 200
        data = resp.json()
        assert data['commit_sha'] == ''
        assert data['release_tag'] == ''
        assert data['latest_sha'] == 'remote_sha_abc'
        assert data['has_update'] is True

    @pytest.mark.anyio
    @patch('web.app.get_auto_upgrade_config', return_value=DEFAULT_CFG)
    @patch('common.release.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item', return_value=None)
    @patch('web.app.put_item')
    async def test_no_release_flag(self, mock_put, mock_get_item, mock_boto,
                                   mock_urlopen, mock_cfg, client):
        """仓库没有正式 Release（404）→ no_release = True，且不回退到分支。"""
        mock_boto.return_value = _cfn_empty()
        mock_urlopen.side_effect = HTTPError('u', 404, 'Not Found', {}, None)

        with patch.dict('sys.modules', {'common.build_info': fake_build_info('local_sha', '')}), \
             patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        data = resp.json()
        assert data['no_release'] is True
        assert data['latest_tag'] is None
        assert data['has_update'] is None

    @pytest.mark.anyio
    @patch('web.app.get_auto_upgrade_config', return_value=DEFAULT_CFG)
    @patch('common.release.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item', return_value=None)
    @patch('web.app.put_item')
    async def test_github_unreachable(self, mock_put, mock_get_item, mock_boto,
                                      mock_urlopen, mock_cfg, client):
        """GitHub 不可达且无缓存 → latest 全为 None，has_update = None。"""
        mock_boto.return_value = _cfn_empty()
        mock_urlopen.side_effect = URLError('timeout')

        with patch.dict('sys.modules', {'common.build_info': fake_build_info('local_sha', 'v1')}), \
             patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        data = resp.json()
        assert data['latest_sha'] is None
        assert data['has_update'] is None
        assert data['no_release'] is False


class TestReleaseCache:
    """_get_latest_release_cached 的缓存行为。"""

    @pytest.mark.anyio
    @patch('web.app.get_auto_upgrade_config', return_value=DEFAULT_CFG)
    @patch('common.release.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item')
    @patch('web.app.put_item')
    async def test_cache_hit_skips_github(self, mock_put, mock_get_item, mock_boto,
                                          mock_urlopen, mock_cfg, client):
        """1h 内命中缓存 → 完全不打 GitHub。"""
        now = datetime.now(timezone.utc)
        mock_get_item.return_value = {
            'PK': 'CONFIG', 'SK': 'version_check',
            'latest_sha': 'cached_sha_aaa', 'latest_tag': 'v20260726',
            'checked_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        mock_boto.return_value = _cfn_empty()

        with patch.dict('sys.modules', {'common.build_info': fake_build_info('cached_sha_aaa', 'v20260726')}), \
             patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        data = resp.json()
        assert data['latest_sha'] == 'cached_sha_aaa'
        assert data['latest_tag'] == 'v20260726'
        assert data['latest_stale'] is False
        assert data['has_update'] is False
        mock_urlopen.assert_not_called()

    @pytest.mark.anyio
    @patch('web.app.get_auto_upgrade_config', return_value=DEFAULT_CFG)
    @patch('common.release.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item')
    @patch('web.app.put_item')
    async def test_cache_expired_refetches(self, mock_put, mock_get_item, mock_boto,
                                           mock_urlopen, mock_cfg, client):
        """缓存过期（>1h）→ 重新拉 GitHub 并写回缓存。"""
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
        mock_get_item.return_value = {
            'PK': 'CONFIG', 'SK': 'version_check',
            'latest_sha': 'old_cached', 'latest_tag': 'v20260726', 'checked_at': old,
        }
        mock_boto.return_value = _cfn_empty()
        mock_urlopen.side_effect = gh_responses(tag='v20260809', sha='fresh_sha')

        with patch.dict('sys.modules', {'common.build_info': fake_build_info('local_sha', 'v20260726')}), \
             patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        data = resp.json()
        assert data['latest_sha'] == 'fresh_sha'
        assert data['latest_tag'] == 'v20260809'
        assert data['latest_stale'] is False
        assert mock_urlopen.call_count == 2  # releases/latest + commits/{tag}
        mock_put.assert_called()

    @pytest.mark.anyio
    @patch('web.app.get_auto_upgrade_config', return_value=DEFAULT_CFG)
    @patch('common.release.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item')
    @patch('web.app.put_item')
    async def test_github_fail_uses_stale_cache(self, mock_put, mock_get_item, mock_boto,
                                                mock_urlopen, mock_cfg, client):
        """GitHub 失败 → 回退过期缓存并标记 stale。"""
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        mock_get_item.return_value = {
            'PK': 'CONFIG', 'SK': 'version_check',
            'latest_sha': 'stale_sha_bbb', 'latest_tag': 'v20260726', 'checked_at': old,
        }
        mock_boto.return_value = _cfn_empty()
        mock_urlopen.side_effect = URLError('rate limited')

        with patch.dict('sys.modules', {'common.build_info': fake_build_info('local_sha', 'v1')}), \
             patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        data = resp.json()
        assert data['latest_sha'] == 'stale_sha_bbb'
        assert data['latest_stale'] is True
        assert data['has_update'] is True


class TestStackInfo:
    """CloudFormation 栈信息读取（白名单、最后更新时间）。"""

    @pytest.mark.anyio
    @patch('web.app.get_auto_upgrade_config', return_value=DEFAULT_CFG)
    @patch('common.release.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item', return_value=None)
    @patch('web.app.put_item')
    async def test_allowed_cidrs_from_stack(self, mock_put, mock_get_item, mock_boto,
                                            mock_urlopen, mock_cfg, client):
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {
            'Stacks': [{
                'LastUpdatedTime': MagicMock(strftime=MagicMock(return_value='2026-08-02T10:00:00Z')),
                'Parameters': [
                    {'ParameterKey': 'AllowedCidrs', 'ParameterValue': '10.0.0.1/32,10.0.1.0/24'},
                    {'ParameterKey': 'SourceRevision', 'ParameterValue': 'abc123'},
                ],
            }]
        }
        mock_boto.return_value = mock_cfn
        mock_urlopen.side_effect = gh_responses(sha='remote_sha_xyz')

        with patch.dict(os.environ, {'STACK_NAME': 'my-stack'}):
            resp = await client.get('/api/version')

        data = resp.json()
        assert data['allowed_cidrs'] == ['10.0.0.1/32', '10.0.1.0/24']
        assert data['last_updated'] == '2026-08-02T10:00:00Z'
        assert data['stack_name'] == 'my-stack'


class TestAutoUpgradeStatus:
    """/api/version 中的 auto_upgrade 段落。"""

    @pytest.mark.anyio
    @patch('web.app.get_auto_upgrade_config')
    @patch('common.release.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item', return_value=None)
    @patch('web.app.put_item')
    async def test_auto_upgrade_block(self, mock_put, mock_get_item, mock_boto,
                                      mock_urlopen, mock_cfg, client):
        mock_cfg.return_value = {
            'enabled': True, 'last_check_at': '2026-08-02T03:00:00Z',
            'last_status': 'SUCCESS', 'last_error': '',
            'last_known_good_sha': 'abc', 'current_upgrade_id': '',
        }
        mock_boto.return_value = _cfn_empty()
        mock_urlopen.side_effect = gh_responses()

        with patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        au = resp.json()['auto_upgrade']
        assert au['enabled'] is True
        assert au['last_status'] == 'SUCCESS'
        assert au['in_progress'] is False
        assert au['next_check_at']  # 下一个周一 03:00 UTC

    @pytest.mark.anyio
    @patch('web.app.get_auto_upgrade_config')
    @patch('common.release.urllib.request.urlopen')
    @patch('web.app.boto3.client')
    @patch('web.app.get_item', return_value=None)
    @patch('web.app.put_item')
    async def test_in_progress_flag(self, mock_put, mock_get_item, mock_boto,
                                    mock_urlopen, mock_cfg, client):
        mock_cfg.return_value = {
            'enabled': True, 'last_check_at': '', 'last_status': 'UPDATING',
            'last_error': '', 'last_known_good_sha': '',
            'current_upgrade_id': '2026-08-02T03:00:00Z',
        }
        mock_boto.return_value = _cfn_empty()
        mock_urlopen.side_effect = gh_responses()

        with patch.dict(os.environ, {'STACK_NAME': 'test'}):
            resp = await client.get('/api/version')

        assert resp.json()['auto_upgrade']['in_progress'] is True


class TestNextCheckTime:
    """_next_check_time：每周一 UTC 03:00。"""

    def test_sunday_returns_next_monday(self):
        from web.app import _next_check_time
        got = _next_check_time(datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))
        assert got == datetime(2026, 8, 3, 3, 0, tzinfo=timezone.utc)

    def test_monday_before_0300_returns_same_day(self):
        from web.app import _next_check_time
        got = _next_check_time(datetime(2026, 8, 3, 1, 0, tzinfo=timezone.utc))
        assert got == datetime(2026, 8, 3, 3, 0, tzinfo=timezone.utc)

    def test_monday_after_0300_returns_next_week(self):
        from web.app import _next_check_time
        got = _next_check_time(datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc))
        assert got == datetime(2026, 8, 10, 3, 0, tzinfo=timezone.utc)

    def test_always_monday(self):
        from web.app import _next_check_time
        for day in range(1, 29):
            got = _next_check_time(datetime(2026, 8, day, 12, 0, tzinfo=timezone.utc))
            assert got.weekday() == 0
            assert got.hour == 3


class TestHealthEndpoint:
    """/api/health：Updater 用它做升级后的应用级验证。"""

    @pytest.mark.anyio
    @patch('web.app.get_item', return_value=None)
    async def test_health_ok(self, mock_get_item, client):
        with patch.dict('sys.modules', {'common.build_info': fake_build_info('sha1', 'v20260802', '2026-08-02T00:00:00Z')}):
            resp = await client.get('/api/health')

        assert resp.status_code == 200
        data = resp.json()
        assert data['ok'] is True
        assert data['checks']['dynamodb'] == 'ok'
        assert data['checks']['modules'] == 'ok'
        assert data['release_tag'] == 'v20260802'

    @pytest.mark.anyio
    @patch('web.app.get_item', side_effect=Exception('table not found'))
    async def test_health_ddb_failure_returns_503(self, mock_get_item, client):
        """DDB 不可读 → 503，Updater 据此触发自动回退。"""
        resp = await client.get('/api/health')
        assert resp.status_code == 503
        data = resp.json()
        assert data['ok'] is False
        assert 'error' in data['checks']['dynamodb']


class TestAutoUpgradeConfigApi:
    """/api/config/auto-upgrade 开关读写。"""

    @pytest.mark.anyio
    @patch('web.app.get_auto_upgrade_config')
    async def test_get_config(self, mock_cfg, client):
        mock_cfg.return_value = {
            'enabled': False, 'last_check_at': '2026-08-02T03:00:00Z',
            'last_status': 'NO_UPDATE', 'last_error': '',
            'last_known_good_sha': '', 'current_upgrade_id': '',
        }
        resp = await client.get('/api/config/auto-upgrade')
        assert resp.status_code == 200
        assert resp.json()['enabled'] is False

    @pytest.mark.anyio
    @patch('web.app.save_auto_upgrade_config')
    async def test_post_enable(self, mock_save, client):
        resp = await client.post('/api/config/auto-upgrade', json={'enabled': True})
        assert resp.status_code == 200
        assert resp.json()['enabled'] is True
        mock_save.assert_called_once_with(enabled='true')

    @pytest.mark.anyio
    @patch('web.app.save_auto_upgrade_config')
    async def test_post_disable(self, mock_save, client):
        resp = await client.post('/api/config/auto-upgrade', json={'enabled': False})
        assert resp.status_code == 200
        mock_save.assert_called_once_with(enabled='false')

    @pytest.mark.anyio
    @patch('web.app.save_auto_upgrade_config')
    async def test_post_rejects_non_bool(self, mock_save, client):
        resp = await client.post('/api/config/auto-upgrade', json={'enabled': 'yes'})
        assert resp.status_code == 400
        mock_save.assert_not_called()


class TestUpgradeNowApi:
    """/api/upgrade/now：Web 只异步触发 Updater，不自己执行升级。"""

    @pytest.mark.anyio
    @patch('web.app.boto3.client')
    @patch('web.app.get_auto_upgrade_config', return_value=DEFAULT_CFG)
    async def test_invokes_updater_async(self, mock_cfg, mock_boto, client):
        mock_lambda = MagicMock()
        mock_boto.return_value = mock_lambda

        with patch.dict(os.environ, {'UPDATER_FUNCTION_NAME': 'stack-updater'}):
            resp = await client.post('/api/upgrade/now')

        assert resp.status_code == 200
        kwargs = mock_lambda.invoke.call_args.kwargs
        assert kwargs['FunctionName'] == 'stack-updater'
        assert kwargs['InvocationType'] == 'Event'
        assert json.loads(kwargs['Payload'])['action'] == 'upgrade_now'

    @pytest.mark.anyio
    @patch('web.app.boto3.client')
    @patch('web.app.get_auto_upgrade_config')
    async def test_rejects_when_in_progress(self, mock_cfg, mock_boto, client):
        """已有升级在跑 → 409，不重复触发。"""
        mock_cfg.return_value = dict(DEFAULT_CFG, current_upgrade_id='2026-08-02T03:00:00Z')

        with patch.dict(os.environ, {'UPDATER_FUNCTION_NAME': 'stack-updater'}):
            resp = await client.post('/api/upgrade/now')

        assert resp.status_code == 409
        mock_boto.return_value.invoke.assert_not_called()

    @pytest.mark.anyio
    @patch('web.app.get_auto_upgrade_config', return_value=DEFAULT_CFG)
    async def test_missing_updater_env_returns_503(self, mock_cfg, client):
        env = {k: v for k, v in os.environ.items() if k != 'UPDATER_FUNCTION_NAME'}
        with patch.dict(os.environ, env, clear=True):
            resp = await client.post('/api/upgrade/now')
        assert resp.status_code == 503


class TestUpgradeHistoryApi:
    """/api/upgrade/history。"""

    @pytest.mark.anyio
    @patch('web.app.get_upgrade_history')
    async def test_history_shape(self, mock_hist, client):
        mock_hist.return_value = [{
            'PK': 'UPGRADE', 'SK': '2026-08-02T03:00:00Z',
            'status': 'SUCCESS', 'from_sha': 'old111', 'from_tag': 'v20260726',
            'to_sha': 'new222', 'to_tag': 'v20260802',
            'changelog': '- 修复超时\n- 优化性能', 'commit_count': 3,
            'is_rollback': 'false', 'started_at': '2026-08-02T03:00:00Z',
            'finished_at': '2026-08-02T03:08:00Z',
        }]
        resp = await client.get('/api/upgrade/history')
        assert resp.status_code == 200
        h = resp.json()['history'][0]
        assert h['status'] == 'SUCCESS'
        assert h['to_tag'] == 'v20260802'
        assert h['commit_count'] == 3
        assert h['is_rollback'] is False
        assert '修复超时' in h['changelog']

    @pytest.mark.anyio
    @patch('web.app.get_upgrade_history', return_value=[])
    async def test_empty_history(self, mock_hist, client):
        resp = await client.get('/api/upgrade/history')
        assert resp.json()['history'] == []

    @pytest.mark.anyio
    @patch('web.app.get_upgrade_history', return_value=[])
    async def test_limit_validation(self, mock_hist, client):
        assert (await client.get('/api/upgrade/history?limit=0')).status_code == 422
        assert (await client.get('/api/upgrade/history?limit=101')).status_code == 422
