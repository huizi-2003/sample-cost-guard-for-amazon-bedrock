"""升级控制器测试。

重点覆盖那些"出错就没人能救"的路径：
  - 方向判定：只有 ahead 才升级，behind / diverged / identical 都不能自动执行
  - 无 Release：明确阻断，绝不回退到跟随分支
  - 安全检查：拒绝删除或替换 DynamoDB / S3 等有状态资源
  - 健康检查失败后的自动回退
  - 并发保护：栈已在变更中时不叠加新升级
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import json
from unittest.mock import MagicMock, patch

import pytest
from urllib.error import HTTPError

from common.release import ReleaseNotFound
from updater import handler as up


# ===== 固件 =====

def _ctx(remaining_ms=900_000):
    ctx = MagicMock()
    ctx.get_remaining_time_in_millis.return_value = remaining_ms
    ctx.log_stream_name = 'test-stream'
    return ctx


BASE_CFG = {
    'enabled': True, 'last_check_at': '', 'last_status': '', 'last_error': '',
    'last_known_good_sha': '', 'current_upgrade_id': '',
}

ENV = {
    'STACK_NAME': 'bedrock-cost-guard',
    'GITHUB_OWNER': 'owner',
    'GITHUB_REPO': 'repo',
    'CODE_BUCKET': 'bucket',
    'AWS_REGION': 'us-east-1',
    'STACK_UPDATE_ROLE_ARN': 'arn:aws:iam::1:role/stack-update',
    'WEB_FUNCTION_NAME': 'bedrock-cost-guard-web',
    'AWS_LAMBDA_FUNCTION_NAME': 'bedrock-cost-guard-updater',
}


def _stack(status='UPDATE_COMPLETE', params=None):
    return {
        'StackStatus': status,
        'Parameters': params if params is not None else [
            {'ParameterKey': 'AllowedCidrs', 'ParameterValue': '1.2.3.4/32'},
            {'ParameterKey': 'SourceRevision', 'ParameterValue': 'oldsha'},
        ],
    }


# ===== 变更集安全检查 =====

class TestChangesetSafety:
    """CloudFormation 的自动回滚救不回"成功删掉了 DynamoDB 表"，
    所以这类变更必须在执行前拦住。"""

    def test_allows_lambda_code_update(self):
        changes = [{'ResourceChange': {
            'LogicalResourceId': 'WebFunction', 'ResourceType': 'AWS::Lambda::Function',
            'Action': 'Modify', 'Replacement': 'False'}}]
        safe, reasons = up.check_changeset_safety(changes)
        assert safe is True
        assert reasons == []

    def test_blocks_ddb_removal(self):
        changes = [{'ResourceChange': {
            'LogicalResourceId': 'ConfigTable', 'ResourceType': 'AWS::DynamoDB::Table',
            'Action': 'Remove'}}]
        safe, reasons = up.check_changeset_safety(changes)
        assert safe is False
        assert 'ConfigTable' in reasons[0]

    def test_blocks_ddb_replacement(self):
        changes = [{'ResourceChange': {
            'LogicalResourceId': 'ConfigTable', 'ResourceType': 'AWS::DynamoDB::Table',
            'Action': 'Modify', 'Replacement': 'True'}}]
        safe, reasons = up.check_changeset_safety(changes)
        assert safe is False

    def test_blocks_conditional_replacement(self):
        """Conditional 也拦——无人值守时不能赌它不会真的替换。"""
        changes = [{'ResourceChange': {
            'LogicalResourceId': 'CodeBucket', 'ResourceType': 'AWS::S3::Bucket',
            'Action': 'Modify', 'Replacement': 'Conditional'}}]
        safe, reasons = up.check_changeset_safety(changes)
        assert safe is False

    def test_blocks_by_type_even_if_logical_id_renamed(self):
        """按资源类型兜底：改了 LogicalId 也拦得住。"""
        changes = [{'ResourceChange': {
            'LogicalResourceId': 'SomeNewTableName', 'ResourceType': 'AWS::DynamoDB::Table',
            'Action': 'Remove'}}]
        safe, _ = up.check_changeset_safety(changes)
        assert safe is False

    def test_allows_stateless_replacement(self):
        """无状态资源被替换是正常的（如 API Gateway 重建）。"""
        changes = [{'ResourceChange': {
            'LogicalResourceId': 'WebApiStage', 'ResourceType': 'AWS::ApiGateway::Stage',
            'Action': 'Modify', 'Replacement': 'True'}}]
        safe, _ = up.check_changeset_safety(changes)
        assert safe is True

    def test_reports_all_reasons(self):
        changes = [
            {'ResourceChange': {'LogicalResourceId': 'ConfigTable',
                                'ResourceType': 'AWS::DynamoDB::Table', 'Action': 'Remove'}},
            {'ResourceChange': {'LogicalResourceId': 'CodeBucket',
                                'ResourceType': 'AWS::S3::Bucket', 'Action': 'Remove'}},
        ]
        safe, reasons = up.check_changeset_safety(changes)
        assert safe is False
        assert len(reasons) == 2

    def test_empty_changeset_is_safe(self):
        assert up.check_changeset_safety([]) == (True, [])

    def test_collect_changes_reads_all_pages(self):
        """有状态资源的破坏性变更在第二页时也必须被拦下。"""
        mock_cfn = MagicMock()
        page1 = {
            'Changes': [{'ResourceChange': {
                'LogicalResourceId': 'WebFunction',
                'ResourceType': 'AWS::Lambda::Function',
                'Action': 'Modify', 'Replacement': 'False'}}],
            'NextToken': 'page-2',
        }
        page2 = {
            'Changes': [{'ResourceChange': {
                'LogicalResourceId': 'ConfigTable',
                'ResourceType': 'AWS::DynamoDB::Table',
                'Action': 'Remove'}}],
        }
        mock_cfn.describe_change_set.side_effect = [page1, page2]

        changes = up._collect_changes(mock_cfn, 'stack', 'cs-name')
        safe, reasons = up.check_changeset_safety(changes)

        assert len(changes) == 2
        assert not safe
        assert any('ConfigTable' in r for r in reasons)
        # 第二次调用必须带上 NextToken
        call_kwargs = mock_cfn.describe_change_set.call_args_list[1][1]
        assert call_kwargs['NextToken'] == 'page-2'


# ===== 方向判定 =====

class TestDirectionGating:
    """latest != current 只能说明"不同"。必须区分新版 / 回退 / 分叉。"""

    @patch.dict(os.environ, ENV)
    @patch('updater.handler.record_history')
    @patch('updater.handler.save_config')
    @patch('updater.handler.compare_commits')
    @patch('updater.handler.get_latest_release')
    @patch('updater.handler.get_current_version', return_value=('cursha', 'v20260726'))
    @patch('updater.handler.get_config', return_value=dict(BASE_CFG))
    @patch('updater.handler.boto3.client')
    def test_behind_is_blocked(self, mock_boto, mock_cfg, mock_ver, mock_rel,
                               mock_cmp, mock_save, mock_hist):
        """Release 指向更旧的 commit → 阻断，避免降级。"""
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {'Stacks': [_stack()]}
        mock_boto.return_value = mock_cfn
        mock_rel.return_value = {'sha': 'oldersha', 'tag': 'v20260701', 'notes': '', 'published_at': ''}
        mock_cmp.return_value = {'status': 'behind', 'ahead_by': 0, 'behind_by': 5, 'commits': []}

        result = up.check_and_upgrade({}, _ctx())

        assert result['status'] == up.STATUS_BLOCKED
        assert 'behind' in result['reason']
        mock_cfn.create_change_set.assert_not_called()

    @patch.dict(os.environ, ENV)
    @patch('updater.handler.record_history')
    @patch('updater.handler.save_config')
    @patch('updater.handler.compare_commits')
    @patch('updater.handler.get_latest_release')
    @patch('updater.handler.get_current_version', return_value=('cursha', 'v1'))
    @patch('updater.handler.get_config', return_value=dict(BASE_CFG))
    @patch('updater.handler.boto3.client')
    def test_diverged_is_blocked(self, mock_boto, mock_cfg, mock_ver, mock_rel,
                                 mock_cmp, mock_save, mock_hist):
        """历史被改写或换了分支 → 阻断。"""
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {'Stacks': [_stack()]}
        mock_boto.return_value = mock_cfn
        mock_rel.return_value = {'sha': 'forksha', 'tag': 'v9', 'notes': '', 'published_at': ''}
        mock_cmp.return_value = {'status': 'diverged', 'ahead_by': 2, 'behind_by': 3, 'commits': []}

        result = up.check_and_upgrade({}, _ctx())
        assert result['status'] == up.STATUS_BLOCKED
        mock_cfn.create_change_set.assert_not_called()

    @patch.dict(os.environ, ENV)
    @patch('updater.handler.record_history')
    @patch('updater.handler.save_config')
    @patch('updater.handler.get_latest_release')
    @patch('updater.handler.get_current_version', return_value=('samesha', 'v20260802'))
    @patch('updater.handler.get_config', return_value=dict(BASE_CFG))
    @patch('updater.handler.boto3.client')
    def test_identical_sha_is_no_update(self, mock_boto, mock_cfg, mock_ver,
                                        mock_rel, mock_save, mock_hist):
        """SHA 相同 → NO_UPDATE，连 compare 都不用调。"""
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {'Stacks': [_stack()]}
        mock_boto.return_value = mock_cfn
        mock_rel.return_value = {'sha': 'samesha', 'tag': 'v20260802', 'notes': '', 'published_at': ''}

        result = up.check_and_upgrade({}, _ctx())
        assert result['status'] == up.STATUS_NO_UPDATE
        mock_cfn.create_change_set.assert_not_called()


# ===== 无 Release =====

class TestNoRelease:
    @patch.dict(os.environ, ENV)
    @patch('updater.handler.save_config')
    @patch('updater.handler.get_latest_release', side_effect=ReleaseNotFound('no release'))
    @patch('updater.handler.get_current_version', return_value=('cursha', ''))
    @patch('updater.handler.get_config', return_value=dict(BASE_CFG))
    @patch('updater.handler.boto3.client')
    def test_no_release_blocks_without_branch_fallback(self, mock_boto, mock_cfg, mock_ver,
                                                      mock_rel, mock_save):
        """仓库无 Release → 阻断。绝不能回退到跟随分支，那会废掉整个机制。"""
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {'Stacks': [_stack()]}
        mock_boto.return_value = mock_cfn

        result = up.check_and_upgrade({}, _ctx())
        assert result['status'] == up.STATUS_BLOCKED
        mock_cfn.create_change_set.assert_not_called()

    def test_release_not_found_raised_on_404(self):
        with patch('common.release.urllib.request.urlopen',
                   side_effect=HTTPError('u', 404, 'Not Found', {}, None)):
            from common.release import get_latest_release
            with pytest.raises(ReleaseNotFound):
                get_latest_release('owner', 'repo')


# ===== 开关与并发保护 =====

class TestGating:
    @patch.dict(os.environ, ENV)
    @patch('updater.handler.save_config')
    @patch('updater.handler.get_config')
    def test_disabled_skips_entirely(self, mock_cfg, mock_save):
        """开关关闭 → 只记录检查时间，不打 GitHub、不碰栈。"""
        mock_cfg.return_value = dict(BASE_CFG, enabled=False)
        result = up.check_and_upgrade({}, _ctx())
        assert result['status'] == up.STATUS_SKIPPED
        mock_save.assert_called_once()

    @patch.dict(os.environ, ENV)
    @patch('updater.handler.record_history')
    @patch('updater.handler.save_config')
    @patch('updater.handler.get_latest_release')
    @patch('updater.handler.get_current_version', return_value=('samesha', 'v1'))
    @patch('updater.handler.get_config')
    @patch('updater.handler.boto3.client')
    def test_upgrade_now_bypasses_disabled_switch(self, mock_boto, mock_cfg, mock_ver,
                                                  mock_rel, mock_save, mock_hist):
        """「立即升级」是用户主动意图，即使自动更新关闭也应继续检查。"""
        mock_cfg.return_value = dict(BASE_CFG, enabled=False)
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {'Stacks': [_stack()]}
        mock_boto.return_value = mock_cfn
        mock_rel.return_value = {'sha': 'samesha', 'tag': 'v1', 'notes': '', 'published_at': ''}

        result = up.check_and_upgrade({'action': 'upgrade_now'}, _ctx())
        assert result['status'] == up.STATUS_NO_UPDATE  # 走到了实际检查

    @patch.dict(os.environ, ENV)
    @patch('updater.handler.save_config')
    @patch('updater.handler.get_config', return_value=dict(BASE_CFG))
    @patch('updater.handler.boto3.client')
    def test_busy_stack_skips(self, mock_boto, mock_cfg, mock_save):
        """栈正在变更中 → 跳过，不叠加并发更新。"""
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {'Stacks': [_stack('UPDATE_IN_PROGRESS')]}
        mock_boto.return_value = mock_cfn

        result = up.check_and_upgrade({}, _ctx())
        assert result['status'] == up.STATUS_SKIPPED
        assert 'UPDATE_IN_PROGRESS' in result['reason']

    @patch.dict(os.environ, ENV)
    @patch('updater.handler.record_history')
    @patch('updater.handler.save_config')
    @patch('updater.handler._finish', return_value={'status': 'SUCCESS'})
    @patch('updater.handler._apply_revision', return_value=(True, {'changeset_name': 'cs'}))
    @patch('updater.handler.compare_commits')
    @patch('updater.handler.get_latest_release')
    @patch('updater.handler.get_current_version', return_value=('', ''))
    @patch('updater.handler.get_config', return_value=dict(BASE_CFG))
    @patch('updater.handler.boto3.client')
    def test_unknown_current_version_upgrades_to_latest(
            self, mock_boto, mock_cfg, mock_ver, mock_rel, mock_cmp,
            mock_apply, mock_finish, mock_save, mock_hist):
        """当前版本未知（build_info 缺失）→ 视为过期，直接升到最新 Release。

        旧行为是阻断，但那会让早于本功能的部署永久卡住、无法自愈。
        没有基准 SHA 时做不了方向判定，所以此路径不调 compare。
        """
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {'Stacks': [_stack()]}
        mock_boto.return_value = mock_cfn
        mock_rel.return_value = {'sha': 'newsha', 'tag': 'v20260802',
                                 'notes': '- 首个正式版本', 'published_at': ''}

        result = up.check_and_upgrade({}, _ctx())

        assert result['status'] == 'SUCCESS'
        mock_apply.assert_called_once()
        # 目标是最新 Release 的 SHA
        assert mock_apply.call_args[0][7] == 'newsha'
        # 无基准版本，不做方向判定
        mock_cmp.assert_not_called()
        # changelog 退化为 Release notes
        assert mock_hist.call_args.kwargs['changelog'] == '- 首个正式版本'
        assert mock_hist.call_args.kwargs['commit_count'] == 0


# ===== 参数构造 =====

class TestBuildParameters:
    """目标模板增删参数时都不能让变更集创建失败。"""

    def test_drops_parameters_removed_from_target_template(self):
        """本次改动删掉了 Branch 参数；沿用旧值会导致
        "Parameters: [Branch] do not exist in the template"。"""
        mock_cfn = MagicMock()
        mock_cfn.validate_template.return_value = {'Parameters': [
            {'ParameterKey': 'SourceRevision'},
            {'ParameterKey': 'AllowedCidrs'},
        ]}
        stack = _stack(params=[
            {'ParameterKey': 'AllowedCidrs', 'ParameterValue': '1.2.3.4/32'},
            {'ParameterKey': 'Branch', 'ParameterValue': 'main'},
        ])
        params = up._build_parameters(mock_cfn, stack, 'https://s3/t.yaml', 'newsha')

        keys = {p['ParameterKey'] for p in params}
        assert 'Branch' not in keys
        assert keys == {'SourceRevision', 'AllowedCidrs'}

    def test_source_revision_set_explicitly(self):
        mock_cfn = MagicMock()
        mock_cfn.validate_template.return_value = {'Parameters': [
            {'ParameterKey': 'SourceRevision'}, {'ParameterKey': 'AllowedCidrs'}]}
        params = up._build_parameters(mock_cfn, _stack(), 'https://s3/t.yaml', 'targetsha')

        sr = [p for p in params if p['ParameterKey'] == 'SourceRevision'][0]
        assert sr['ParameterValue'] == 'targetsha'
        assert 'UsePreviousValue' not in sr

    def test_other_params_use_previous_value(self):
        mock_cfn = MagicMock()
        mock_cfn.validate_template.return_value = {'Parameters': [
            {'ParameterKey': 'SourceRevision'}, {'ParameterKey': 'AllowedCidrs'}]}
        params = up._build_parameters(mock_cfn, _stack(), 'https://s3/t.yaml', 'targetsha')

        cidr = [p for p in params if p['ParameterKey'] == 'AllowedCidrs'][0]
        assert cidr['UsePreviousValue'] is True

    def test_validate_failure_falls_back_to_all_existing(self):
        """validate_template 不可用时退化为沿用全部现有参数，而不是直接失败。"""
        from botocore.exceptions import ClientError
        mock_cfn = MagicMock()
        mock_cfn.validate_template.side_effect = ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'no'}}, 'ValidateTemplate')
        params = up._build_parameters(mock_cfn, _stack(), 'https://s3/t.yaml', 'targetsha')
        keys = {p['ParameterKey'] for p in params}
        assert 'SourceRevision' in keys
        assert 'AllowedCidrs' in keys


# ===== 健康检查 =====

class TestHealthCheck:
    def test_healthy_on_200(self):
        mock_lambda = MagicMock()
        payload = MagicMock()
        payload.read.return_value = json.dumps({'statusCode': 200, 'body': '{"ok":true}'}).encode()
        mock_lambda.invoke.return_value = {'Payload': payload}

        ok, detail = up.health_check(mock_lambda, 'web-fn', retries=1)
        assert ok is True
        assert detail == 'ok'

    def test_unhealthy_on_503(self):
        mock_lambda = MagicMock()
        payload = MagicMock()
        payload.read.return_value = json.dumps({'statusCode': 503, 'body': '{"ok":false}'}).encode()
        mock_lambda.invoke.return_value = {'Payload': payload}

        ok, detail = up.health_check(mock_lambda, 'web-fn', retries=1, delay=0)
        assert ok is False
        assert '503' in detail

    def test_unhealthy_on_function_error(self):
        """新代码有导入错误时 Lambda 返回 FunctionError——这正是 CFn 抓不到的情况。"""
        mock_lambda = MagicMock()
        payload = MagicMock()
        payload.read.return_value = b'{"errorType":"ImportModuleError"}'
        mock_lambda.invoke.return_value = {'FunctionError': 'Unhandled', 'Payload': payload}

        ok, detail = up.health_check(mock_lambda, 'web-fn', retries=1, delay=0)
        assert ok is False
        assert 'ImportModuleError' in detail

    def test_retries_then_succeeds(self):
        mock_lambda = MagicMock()
        bad, good = MagicMock(), MagicMock()
        bad.read.return_value = json.dumps({'statusCode': 503}).encode()
        good.read.return_value = json.dumps({'statusCode': 200}).encode()
        mock_lambda.invoke.side_effect = [{'Payload': bad}, {'Payload': good}]

        ok, _ = up.health_check(mock_lambda, 'web-fn', retries=2, delay=0)
        assert ok is True
        assert mock_lambda.invoke.call_count == 2

    def test_health_event_is_valid_apigw_shape(self):
        ev = up._health_event()
        assert ev['httpMethod'] == 'GET'
        assert ev['path'] == '/api/health'
        assert ev['requestContext']['stage'] == 'prod'
        json.dumps(ev)  # 必须可序列化，否则 invoke 会炸


# ===== 自动回退 =====

class TestAutoRollback:
    @patch.dict(os.environ, ENV)
    @patch('updater.handler.notify')
    @patch('updater.handler.record_history')
    @patch('updater.handler.save_config')
    @patch('updater.handler.health_check', return_value=(True, 'ok'))
    def test_success_records_last_known_good(self, mock_health, mock_save,
                                             mock_hist, mock_notify):
        """健康检查通过才把该版本记为回退基准。"""
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {'Stacks': [_stack('UPDATE_COMPLETE')]}
        ctx_info = {'target_sha': 'newsha', 'target_tag': 'v2', 'from_sha': 'oldsha',
                    'owner': 'o', 'repo': 'r', 'bucket': 'b', 'region': 'us-east-1',
                    'role_arn': 'arn', 'is_rollback': False, 'hop': 0}

        result = up._finish(mock_cfn, MagicMock(), 'stack', 'id1', ctx_info, _ctx())

        assert result['status'] == up.STATUS_SUCCESS
        assert mock_save.call_args.kwargs['last_known_good_sha'] == 'newsha'
        mock_notify.assert_not_called()  # 成功不打扰用户

    @patch.dict(os.environ, ENV)
    @patch('updater.handler.notify')
    @patch('updater.handler.record_history')
    @patch('updater.handler.save_config')
    @patch('updater.handler.health_check', return_value=(False, '/api/health 返回 503'))
    def test_cfn_failure_notifies_and_does_not_promote(self, mock_health, mock_save,
                                                      mock_hist, mock_notify):
        """CloudFormation 层面失败（已自动回滚）→ 记 FAILED 并通知。"""
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {
            'Stacks': [_stack('UPDATE_ROLLBACK_COMPLETE')]}
        ctx_info = {'target_sha': 'newsha', 'target_tag': 'v2', 'from_sha': 'oldsha',
                    'owner': 'o', 'repo': 'r', 'bucket': 'b', 'region': 'us-east-1',
                    'role_arn': 'arn', 'is_rollback': False, 'hop': 0}

        result = up._finish(mock_cfn, MagicMock(), 'stack', 'id1', ctx_info, _ctx())

        assert result['status'] == up.STATUS_FAILED
        assert 'last_known_good_sha' not in mock_save.call_args.kwargs
        mock_notify.assert_called_once()

    @patch.dict(os.environ, ENV)
    @patch('updater.handler.notify')
    @patch('updater.handler.record_history')
    @patch('updater.handler.save_config')
    @patch('updater.handler.health_check', return_value=(False, 'irrelevant'))
    def test_cfn_rollback_failed_mentions_manual_recovery(self, mock_health, mock_save,
                                                          mock_hist, mock_notify):
        """UPDATE_ROLLBACK_FAILED → 文案应提示手动 continue-update-rollback。"""
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {
            'Stacks': [_stack('UPDATE_ROLLBACK_FAILED')]}
        ctx_info = {'target_sha': 'newsha', 'target_tag': 'v2', 'from_sha': 'oldsha',
                    'owner': 'o', 'repo': 'r', 'bucket': 'b', 'region': 'us-east-1',
                    'role_arn': 'arn', 'is_rollback': False, 'hop': 0}

        result = up._finish(mock_cfn, MagicMock(), 'stack', 'id1', ctx_info, _ctx())

        assert result['status'] == up.STATUS_FAILED
        assert 'continue-update-rollback' in result['error']
        assert 'continue-update-rollback' in mock_save.call_args.kwargs.get('last_error', '')
        mock_notify.assert_called_once()

    @patch.dict(os.environ, ENV)
    @patch('updater.handler.notify')
    @patch('updater.handler.record_history')
    @patch('updater.handler.save_config')
    @patch('updater.handler.get_config', return_value=dict(BASE_CFG, last_known_good_sha='oldsha'))
    @patch('updater.handler._self_invoke')
    @patch('updater.handler.health_check', return_value=(False, 'ImportError'))
    def test_health_failure_hands_rollback_to_new_hop(self, mock_health, mock_self, mock_cfg,
                                                      mock_save, mock_hist, mock_notify):
        """CFn 报成功但应用是坏的 → 异步触发 rollback，不在收尾预算里就地回退。

        走到这里时只剩 _RESERVE_MS（90 秒）左右，而回退要下载模板、建变更集、
        等变更集就绪（自带 300 秒上限），就地做会被掐在中间态。
        """
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {'Stacks': [_stack('UPDATE_COMPLETE')]}

        ctx_info = {'target_sha': 'badsha', 'target_tag': 'v2', 'from_sha': 'oldsha',
                    'owner': 'o', 'repo': 'r', 'bucket': 'b', 'region': 'us-east-1',
                    'role_arn': 'arn', 'is_rollback': False, 'hop': 0}

        result = up._finish(mock_cfn, MagicMock(), 'stack', 'id1', ctx_info, _ctx())

        assert result['status'] == up.STATUS_UPDATING
        rb_id = result['rollback_id']

        payload = mock_self.call_args[0][0]
        assert payload['action'] == 'rollback'
        assert payload['upgrade_id'] == rb_id
        # 回退目标必须是升级前的版本
        assert payload['ctx']['target_sha'] == 'oldsha'
        assert payload['ctx']['is_rollback'] is True

        # 状态必须在自调用之前落库：自调用本身也可能丢，
        # DDB 里得留下 rb_id 才能靠失效判定恢复
        assert mock_save.call_args.kwargs['current_upgrade_id'] == rb_id
        assert mock_notify.call_count >= 1

    @patch('updater.handler._claim_rollback', return_value=('claimed', {}))
    @patch.dict(os.environ, ENV)
    @patch('updater.handler.notify')
    @patch('updater.handler.record_history')
    @patch('updater.handler.save_config')
    @patch('updater.handler.get_config', return_value=dict(BASE_CFG, last_known_good_sha='oldsha'))
    @patch('updater.handler._apply_revision', return_value=(True, {'changeset_name': 'cs-rb'}))
    @patch('updater.handler.boto3.client')
    @patch('updater.handler.health_check', return_value=(True, 'ok'))
    def test_rollback_action_applies_good_sha(self, mock_health, mock_boto, mock_apply,
                                              mock_cfg, mock_save, mock_hist, mock_notify,
                                              mock_claim):
        """rollback 这一跳把栈更新回 good_sha，健康检查通过后记为 ROLLED_BACK。"""
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {'Stacks': [_stack('UPDATE_COMPLETE')]}
        mock_boto.return_value = mock_cfn

        # ctx 里塞入恶意的基础设施参数：它们必须被环境变量盖掉，
        # 否则异步事件就成了"从哪拉代码、用什么权限改栈"的注入点。
        event = {'action': 'rollback', 'upgrade_id': 'rb1', 'ctx': {
            'target_sha': 'oldsha', 'from_sha': 'badsha', 'is_rollback': True, 'hop': 0,
            'owner': 'attacker', 'repo': 'evil', 'bucket': 'evil-bucket',
            'role_arn': 'arn:aws:iam::999:role/admin',
        }}
        result = up.rollback(event, _ctx())

        assert result['status'] == up.STATUS_ROLLED_BACK
        args = mock_apply.call_args[0]
        # 回退目标必须是升级前的版本
        assert args[7] == 'oldsha'
        # 基础设施参数一律来自环境变量，不采信 event
        assert (args[3], args[4], args[5]) == ('owner', 'repo', 'bucket')
        assert args[8] == ENV['STACK_UPDATE_ROLE_ARN']

    @patch.dict(os.environ, ENV)
    @patch('updater.handler.notify')
    @patch('updater.handler.record_history')
    @patch('updater.handler.save_config')
    @patch('updater.handler.get_config', return_value=dict(BASE_CFG))
    @patch('updater.handler.health_check', return_value=(False, 'boom'))
    def test_no_good_version_reports_failed(self, mock_health, mock_cfg, mock_save,
                                            mock_hist, mock_notify):
        """健康检查失败且没有可回退版本 → FAILED 并明确告警。"""
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {'Stacks': [_stack('UPDATE_COMPLETE')]}
        ctx_info = {'target_sha': 'badsha', 'target_tag': 'v2', 'from_sha': '',
                    'owner': 'o', 'repo': 'r', 'bucket': 'b', 'region': 'us-east-1',
                    'role_arn': 'arn', 'is_rollback': False, 'hop': 0}

        result = up._finish(mock_cfn, MagicMock(), 'stack', 'id1', ctx_info, _ctx())
        assert result['status'] == up.STATUS_FAILED
        mock_notify.assert_called_once()

    @patch.dict(os.environ, ENV)
    @patch('updater.handler.notify')
    @patch('updater.handler.record_history')
    @patch('updater.handler.save_config')
    @patch('updater.handler.health_check', return_value=(False, 'still broken'))
    def test_rollback_health_failure_needs_human(self, mock_health, mock_save,
                                                 mock_hist, mock_notify):
        """回退后仍不健康 → ROLLBACK_FAILED，不再无限循环。"""
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {'Stacks': [_stack('UPDATE_COMPLETE')]}
        ctx_info = {'target_sha': 'oldsha', 'target_tag': '', 'from_sha': 'badsha',
                    'owner': 'o', 'repo': 'r', 'bucket': 'b', 'region': 'us-east-1',
                    'role_arn': 'arn', 'is_rollback': True, 'hop': 0}

        result = up._finish(mock_cfn, MagicMock(), 'stack', 'id1', ctx_info, _ctx())
        assert result['status'] == up.STATUS_ROLLBACK_FAILED


# ===== 超时续跑 =====

class TestWatchHops:
    @patch.dict(os.environ, ENV)
    @patch('updater.handler._self_invoke')
    def test_self_invokes_when_budget_exhausted(self, mock_self):
        """单次 Lambda 装不下整栈更新 → 异步调用自己继续观察。"""
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {'Stacks': [_stack('UPDATE_IN_PROGRESS')]}
        ctx_info = {'target_sha': 'newsha', 'target_tag': 'v2', 'from_sha': 'oldsha',
                    'owner': 'o', 'repo': 'r', 'bucket': 'b', 'region': 'us-east-1',
                    'role_arn': 'arn', 'is_rollback': False, 'hop': 0}

        result = up._finish(mock_cfn, MagicMock(), 'stack', 'id1', ctx_info, _ctx(remaining_ms=1000))

        assert result['status'] == up.STATUS_UPDATING
        assert result['hop'] == 1
        payload = mock_self.call_args[0][0]
        assert payload['action'] == 'watch'
        assert payload['hop'] == 1

    @patch.dict(os.environ, ENV)
    @patch('updater.handler.notify')
    @patch('updater.handler.record_history')
    @patch('updater.handler.save_config')
    @patch('updater.handler._self_invoke')
    def test_gives_up_after_max_hops(self, mock_self, mock_save, mock_hist, mock_notify):
        """超过最大观察跳数 → 停止跟踪并告警，不无限自调用。"""
        mock_cfn = MagicMock()
        mock_cfn.describe_stacks.return_value = {'Stacks': [_stack('UPDATE_IN_PROGRESS')]}
        ctx_info = {'target_sha': 'newsha', 'target_tag': 'v2', 'from_sha': 'oldsha',
                    'owner': 'o', 'repo': 'r', 'bucket': 'b', 'region': 'us-east-1',
                    'role_arn': 'arn', 'is_rollback': False, 'hop': up.MAX_WATCH_HOPS}

        result = up._finish(mock_cfn, MagicMock(), 'stack', 'id1', ctx_info, _ctx(remaining_ms=1000))

        assert result['status'] == up.STATUS_FAILED
        mock_self.assert_not_called()
        mock_notify.assert_called_once()

    @patch.dict(os.environ, ENV)
    def test_ignores_mismatched_lock_without_writes(self):
        """伪造 watch 不得执行收尾、写配置或污染升级历史。"""
        event = {'upgrade_id': 'forged-id', 'ctx': {'target_sha': 'attacker-sha'}}
        with patch.object(up, '_OWNED_LOCK_ID', None), \
             patch('updater.handler.get_config', return_value={'current_upgrade_id': 'real-id'}), \
             patch('updater.handler._finish') as mock_finish, \
             patch('updater.handler._cfn_client') as mock_cfn, \
             patch('updater.handler._lambda_client') as mock_lambda, \
             patch('updater.handler.save_config') as mock_save, \
             patch('updater.handler.record_history') as mock_history:
            result = up.watch(event, _ctx())

            assert result['status'] == up.STATUS_IGNORED
            assert 'forged-id' in result['reason']
            assert up._OWNED_LOCK_ID is None
            mock_finish.assert_not_called()
            mock_cfn.assert_not_called()
            mock_lambda.assert_not_called()
            mock_save.assert_not_called()
            mock_history.assert_not_called()

    @pytest.mark.parametrize('event', [{}, {'upgrade_id': ''}])
    def test_ignores_missing_or_empty_upgrade_id(self, event):
        """缺失 ID 不能再由 watch 自动生成，空 ID 也不能匹配空锁。"""
        with patch.object(up, '_OWNED_LOCK_ID', None), \
             patch('updater.handler.get_config', return_value={'current_upgrade_id': ''}), \
             patch('updater.handler._finish') as mock_finish, \
             patch('updater.handler._cfn_client') as mock_cfn, \
             patch('updater.handler._lambda_client') as mock_lambda:
            result = up.watch(event, _ctx())

            assert result['status'] == up.STATUS_IGNORED
            assert up._OWNED_LOCK_ID is None
            mock_finish.assert_not_called()
            mock_cfn.assert_not_called()
            mock_lambda.assert_not_called()

    @patch.dict(os.environ, ENV)
    def test_matching_lock_continues_finish(self):
        """合法 watch 持有当前锁时继续原有 _finish 链路。"""
        cfn = MagicMock()
        lambda_client = MagicMock()
        expected = {'status': up.STATUS_SUCCESS}
        event = {
            'upgrade_id': 'upgrade-id',
            'hop': 2,
            'ctx': {'target_sha': 'newsha', 'from_sha': 'oldsha'},
        }
        with patch.object(up, '_OWNED_LOCK_ID', None), \
             patch('updater.handler.get_config', return_value={'current_upgrade_id': 'upgrade-id'}), \
             patch('updater.handler._cfn_client', return_value=cfn), \
             patch('updater.handler._lambda_client', return_value=lambda_client), \
             patch('updater.handler._finish', return_value=expected) as mock_finish:
            result = up.watch(event, _ctx())

            assert result == expected
            assert up._OWNED_LOCK_ID == 'upgrade-id'
            args = mock_finish.call_args.args
            assert args[:4] == (cfn, lambda_client, ENV['STACK_NAME'], 'upgrade-id')
            assert args[4]['target_sha'] == 'newsha'
            assert args[4]['hop'] == 2


# ===== 事件路由 =====

class TestHandlerRouting:
    @patch('updater.handler.check_and_upgrade', return_value={'status': 'X'})
    def test_empty_event_is_scheduled_check(self, mock_check):
        up.handler({}, _ctx())
        mock_check.assert_called_once()

    @patch('updater.handler.check_and_upgrade', return_value={'status': 'X'})
    def test_none_event_tolerated(self, mock_check):
        up.handler(None, _ctx())
        mock_check.assert_called_once()

    @patch('updater.handler.watch', return_value={'status': 'X'})
    def test_watch_action(self, mock_watch):
        up.handler({'action': 'watch', 'upgrade_id': 'i', 'hop': 2}, _ctx())
        mock_watch.assert_called_once()

    @patch('updater.handler.check_and_upgrade', return_value={'status': 'X'})
    def test_upgrade_now_action(self, mock_check):
        up.handler({'action': 'upgrade_now'}, _ctx())
        mock_check.assert_called_once()

    def test_unknown_action_ignored(self):
        result = up.handler({'action': 'nonsense'}, _ctx())
        assert result['status'] == 'IGNORED'


# ===== 通知策略 =====

class TestNotifyPolicy:
    @patch('updater.handler.send_webhook_all')
    @patch('updater.handler.get_webhook_config', return_value=[{'url': 'u', 'type': 'feishu'}])
    def test_notify_sends_when_configured(self, mock_cfg, mock_send):
        up.notify('msg')
        mock_send.assert_called_once()

    @patch('updater.handler.send_webhook_all')
    @patch('updater.handler.get_webhook_config', return_value=[])
    def test_notify_noop_without_webhooks(self, mock_cfg, mock_send):
        up.notify('msg')
        mock_send.assert_not_called()

    @patch('updater.handler.get_webhook_config', side_effect=Exception('ddb down'))
    def test_notify_failure_does_not_raise(self, mock_cfg):
        """通知失败不能影响升级主流程。"""
        up.notify('msg')  # 不应抛异常


# ===== common.release =====

class TestReleaseHelpers:
    def test_tag_resolved_to_sha_not_target_commitish(self):
        """target_commitish 常常是分支名，必须再解析一次 tag 拿不可变 SHA。"""
        r1, r2 = MagicMock(), MagicMock()
        r1.read.return_value = json.dumps({
            'tag_name': 'v20260802', 'name': 'v20260802', 'body': 'notes',
            'published_at': '2026-08-02T03:00:00Z', 'target_commitish': 'main',
        }).encode()
        r2.read.return_value = b'realsha1234567890'
        m1, m2 = MagicMock(), MagicMock()
        m1.__enter__ = MagicMock(return_value=r1); m1.__exit__ = MagicMock(return_value=False)
        m2.__enter__ = MagicMock(return_value=r2); m2.__exit__ = MagicMock(return_value=False)

        with patch('common.release.urllib.request.urlopen', side_effect=[m1, m2]):
            from common.release import get_latest_release
            rel = get_latest_release('o', 'r')

        assert rel['sha'] == 'realsha1234567890'
        assert rel['sha'] != 'main'
        assert rel['tag'] == 'v20260802'
        assert rel['notes'] == 'notes'

    def test_compare_extracts_commit_subjects(self):
        resp = MagicMock()
        resp.read.return_value = json.dumps({
            'status': 'ahead', 'ahead_by': 2, 'behind_by': 0,
            'commits': [
                {'commit': {'message': '修复账单超时\n\n详细说明在这里'}},
                {'commit': {'message': '优化 IAM 扫描'}},
            ],
        }).encode()
        m = MagicMock()
        m.__enter__ = MagicMock(return_value=resp); m.__exit__ = MagicMock(return_value=False)

        with patch('common.release.urllib.request.urlopen', return_value=m):
            from common.release import compare_commits
            got = compare_commits('o', 'r', 'base', 'head')

        assert got['status'] == 'ahead'
        assert got['ahead_by'] == 2
        # 只取首行，正文不进 changelog
        assert got['commits'] == ['修复账单超时', '优化 IAM 扫描']
