"""升级控制器 Lambda：自动检查 GitHub Release 并通过 CloudFormation 整栈升级。

为什么需要独立的 Lambda 而不是让 Web Lambda 直接升级：
  - 一次升级涉及 Lambda 代码、Web、AgentCore Runtime 和模板资源本身，
    不能只替换函数代码，必须走 CloudFormation 整栈更新。
  - 执行整栈更新需要覆盖全部资源类型的写权限。把这些权限挂在对外提供
    HTTP 服务的 Web Lambda 上风险过大，因此隔离到本函数 + 独立的
    CloudFormation service role。

升级流程：
    EventBridge（每周一）或 Web「立即升级」
        ↓
    读 DDB 开关 → 关闭则只记录检查时间
        ↓
    取最新正式 Release → 锁定不可变 commit SHA
        ↓
    compare 判方向：只有 'ahead' 才继续（区分新版 / 回退 / 分叉）
        ↓
    下载该 SHA 的 template.yaml → 上传 S3
        ↓
    CreateChangeSet（SourceRevision 固定为该 SHA）
        ↓
    安全检查：拒绝删除或替换 DynamoDB / S3 等有状态资源
        ↓
    ExecuteChangeSet → 轮询至终态（CloudFormation 负责基础设施级回滚）
        ↓
    应用级健康检查（调 Web Lambda /api/health）
        ↓
    通过 → 记录 last_known_good_sha
    失败 → 异步触发 action=rollback，在新的时间预算里回退到
           last_known_good_sha 并通知
"""
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from common.config import (
    AUTO_UPGRADE_SK,
    UPGRADE_HISTORY_PK,
    _get_table as _get_config_table,
    get_auto_upgrade_config as get_config,
    get_upgrade_history as get_history,
    get_webhook_config,
    merge_upgrade_record as merge_history,
    record_upgrade as record_history,
    save_auto_upgrade_config as save_config,
)
from common.release import (
    ReleaseNotFound, compare_commits, download_file, get_latest_release,
)
from common.webhook import send_webhook_all

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ===== 状态 =====
STATUS_NO_UPDATE = 'NO_UPDATE'
STATUS_UPDATING = 'UPDATING'
STATUS_SUCCESS = 'SUCCESS'
STATUS_FAILED = 'FAILED'
STATUS_ROLLED_BACK = 'ROLLED_BACK'
STATUS_ROLLBACK_FAILED = 'ROLLBACK_FAILED'
STATUS_BLOCKED = 'BLOCKED'
STATUS_SKIPPED = 'SKIPPED'
STATUS_IGNORED = 'IGNORED'

# 当前 Lambda invocation 持有的升级锁。Lambda 执行环境会复用，因此 handler
# 每次入口必须重置；只有成功写入锁或承接对应 upgrade_id 的续跑调用后才赋值。
_OWNED_LOCK_ID = None

# 有状态资源：CloudFormation 变更集里若要删除或替换它们，一律拒绝自动执行。
# 按 LogicalId 和资源类型双重判定，这样即使将来改了 LogicalId 也拦得住。
PROTECTED_LOGICAL_IDS = frozenset({'ConfigTable', 'CodeBucket'})
PROTECTED_TYPES = frozenset({'AWS::DynamoDB::Table', 'AWS::S3::Bucket'})

# 栈处于这些状态时不允许发起新的升级
_BUSY_SUFFIXES = ('_IN_PROGRESS',)
_TERMINAL_OK = ('CREATE_COMPLETE', 'UPDATE_COMPLETE', 'IMPORT_COMPLETE')

# 自调用续跑的最大跳数。单次 Lambda 最长 15 分钟，而整栈更新（含三次
# pip install + AgentCore Runtime 重建）可能超过这个时间，因此预算耗尽时
# 异步调用自己继续观察。8 跳约 2 小时，足够覆盖最慢的情况。
MAX_WATCH_HOPS = 8

# 留给收尾（健康检查、写 DDB、发通知、一次异步自调用）的时间。
# 回退不在这个预算里——它自己占一整跳，见 _finish 的健康检查失败分支。
_RESERVE_MS = 90_000

# 健康检查单次尝试的最坏耗时：connect(5) + read(20) + 重试间隔(10)。
_HEALTH_ATTEMPT_MS = 35_000

# 健康检查放弃重试后必须留下的收尾时间：写 DDB + 发通知 + 一次异步 invoke。
_FINISH_TAIL_MS = 20_000

# Lambda invoke 用短超时且不重试。健康检查和自调用都跑在收尾预算里，
# 在这里"挂住"比"失败"更危险：失败还能触发回退，挂住会把整个函数拖到
# Lambda 超时被掐死——那时不会抛出可捕获的异常，兜底清理也跑不到。
# max_attempts=1 + mode=standard 显式表示"总共尝试一次"，避开 legacy 模式下
# max_attempts 到底是"重试次数"还是"总次数"的歧义。
_INVOKE_CFG = Config(connect_timeout=5, read_timeout=20,
                     retries={'max_attempts': 1, 'mode': 'standard'})

# CloudFormation / S3 的取舍相反：describe_* 跑在轮询循环里，一次整栈更新要
# 打几十次，需要的是抗限流而不是快速失败。
_STACK_CFG = Config(connect_timeout=10, read_timeout=30,
                    retries={'max_attempts': 10, 'mode': 'adaptive'})


def _cfn_client():
    return boto3.client('cloudformation', config=_STACK_CFG)


def _s3_client():
    return boto3.client('s3', config=_STACK_CFG)


def _lambda_client():
    return boto3.client('lambda', config=_INVOKE_CFG)


def _now():
    return datetime.now(timezone.utc)


def _iso(dt=None):
    return (dt or _now()).strftime('%Y-%m-%dT%H:%M:%SZ')


def _env(name, default=''):
    return os.environ.get(name, default)


# ===== 配置读写 =====
# get_config / save_config / record_history / get_history 均来自 common.config，
# 与 Web Lambda 共用同一份实现，避免两侧对 DDB 结构的理解漂移。


def _get_upgrade_record(upgrade_id):
    """强一致读取一条升级历史，用于判断条件写失败的具体原因。"""
    resp = _get_config_table().get_item(
        Key={'PK': UPGRADE_HISTORY_PK, 'SK': upgrade_id}, ConsistentRead=True)
    return resp.get('Item') or {}


def _claim_rollback(upgrade_id, target_sha):
    """原子校验并认领回退；返回 claimed / duplicate / invalid。"""
    claim_id = uuid.uuid4().hex
    try:
        _get_config_table().update_item(
            Key={'PK': UPGRADE_HISTORY_PK, 'SK': upgrade_id},
            UpdateExpression='SET rollback_claim_id = :claim_id',
            ConditionExpression=(
                'is_rollback = :is_rollback AND #status = :status '
                'AND to_sha = :target_sha AND attribute_not_exists(rollback_claim_id)'
            ),
            ExpressionAttributeNames={'#status': 'status'},
            ExpressionAttributeValues={
                ':claim_id': claim_id,
                ':is_rollback': 'true',
                ':status': STATUS_UPDATING,
                ':target_sha': target_sha,
            },
        )
        return 'claimed', {}
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') != 'ConditionalCheckFailedException':
            raise

    # 条件失败不一定是伪造：也可能是 at-least-once 的另一份投递已认领。
    record = _get_upgrade_record(upgrade_id)
    expected = (
        record.get('is_rollback') == 'true'
        and record.get('status') == STATUS_UPDATING
        and record.get('to_sha') == target_sha
    )
    if expected and record.get('rollback_claim_id'):
        return 'duplicate', record
    return 'invalid', record


def _release_owned_lock(upgrade_id, error):
    """仅当 DDB 当前锁仍属于 upgrade_id 时原子清锁，避免 TOCTOU。"""
    try:
        _get_config_table().update_item(
            Key={'PK': 'CONFIG', 'SK': AUTO_UPGRADE_SK},
            UpdateExpression=(
                'SET current_upgrade_id = :empty, last_status = :failed, '
                'last_error = :error'
            ),
            ConditionExpression='current_upgrade_id = :upgrade_id',
            ExpressionAttributeValues={
                ':empty': '',
                ':failed': STATUS_FAILED,
                ':error': error,
                ':upgrade_id': upgrade_id,
            },
        )
        return True
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
            return False
        raise


# ===== 当前版本 =====

def get_current_version():
    """返回当前部署的 (sha, tag)。

    build_info.py 由 CodeFetcher 在构建时注入，是最可靠的来源——它描述的是
    实际打进 zip 的那份代码，而不是栈参数声明的意图。
    """
    try:
        from common.build_info import COMMIT_SHA, RELEASE_TAG
        return (COMMIT_SHA or ''), (RELEASE_TAG or '')
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 - build_info 是生成文件，容错优先
        logger.warning(f'读取 build_info 失败: {e}')
    return '', ''


# ===== 通知 =====

def notify(message):
    """发送 webhook 通知。

    按设计只在失败 / 回退 / 阻断时调用——升级成功不通知，避免每周一条
    无用打扰；但失败必须有人知道，否则用户会长期停在旧版本而毫无察觉。
    """
    try:
        webhooks = get_webhook_config()
        if webhooks:
            send_webhook_all(message, webhooks)
    except Exception as e:  # noqa: BLE001 - 通知失败不能影响升级主流程
        logger.warning(f'发送升级通知失败: {e}')


# ===== CloudFormation 辅助 =====

def _stack_status(cfn, stack_name):
    resp = cfn.describe_stacks(StackName=stack_name)
    stacks = resp.get('Stacks') or []
    if not stacks:
        raise RuntimeError(f'找不到栈 {stack_name}')
    return stacks[0].get('StackStatus', ''), stacks[0]


def _is_busy(status):
    return any(status.endswith(s) for s in _BUSY_SUFFIXES)


def _template_url(bucket, key, region):
    return f'https://{bucket}.s3.{region}.amazonaws.com/{key}'


def _build_parameters(cfn, stack, template_url, source_revision):
    """构造 change set 参数列表。

    用 validate_template 拿到目标模板真正声明的参数名，再和当前栈的参数取
    交集。这样目标模板新增参数时走它自己的默认值，删除参数时也不会因为
    传了不存在的参数而失败——`Branch` 这种被移除的参数正是这么处理的。
    """
    valid = set()
    try:
        resp = cfn.validate_template(TemplateURL=template_url)
        valid = {p['ParameterKey'] for p in resp.get('Parameters') or []}
    except ClientError as e:
        logger.warning(f'validate_template 失败，回退为沿用全部现有参数: {e}')

    params = []
    if not valid or 'SourceRevision' in valid:
        params.append({'ParameterKey': 'SourceRevision', 'ParameterValue': source_revision})

    for p in stack.get('Parameters') or []:
        key = p['ParameterKey']
        if key == 'SourceRevision':
            continue
        if valid and key not in valid:
            logger.info(f'目标模板已移除参数 {key}，跳过')
            continue
        params.append({'ParameterKey': key, 'UsePreviousValue': True})
    return params


def check_changeset_safety(changes):
    """检查变更集是否会删除或替换有状态资源。

    返回 (safe: bool, reasons: list[str])。

    CloudFormation 的自动回滚只能救回"更新失败"，救不回"成功地删掉了
    DynamoDB 表"。所以这类变更绝不允许无人值守执行。
    """
    reasons = []
    for change in changes:
        rc = change.get('ResourceChange') or {}
        logical_id = rc.get('LogicalResourceId', '?')
        res_type = rc.get('ResourceType', '?')
        action = rc.get('Action', '')
        replacement = rc.get('Replacement', '')

        protected = logical_id in PROTECTED_LOGICAL_IDS or res_type in PROTECTED_TYPES
        if not protected:
            continue
        if action == 'Remove':
            reasons.append(f'{logical_id} ({res_type}) 将被删除')
        elif replacement in ('True', 'Conditional'):
            reasons.append(f'{logical_id} ({res_type}) 将被替换（Replacement={replacement}）')
    return (not reasons), reasons


def _wait_terminal(cfn, stack_name, context, poll=15):
    """轮询栈状态直到终态或时间预算耗尽。

    返回 (status, timed_out)。timed_out=True 表示栈还在更新中，需要由调用方
    自调用续跑（单次 Lambda 15 分钟装不下一次完整整栈更新）。
    """
    while True:
        status, _ = _stack_status(cfn, stack_name)
        if not _is_busy(status):
            return status, False
        remaining = context.get_remaining_time_in_millis() if context else 0
        if remaining < _RESERVE_MS:
            return status, True
        time.sleep(poll)


def _delete_changeset(cfn, stack_name, name):
    try:
        cfn.delete_change_set(StackName=stack_name, ChangeSetName=name)
    except ClientError as e:
        logger.warning(f'删除变更集 {name} 失败: {e}')


def _wait_changeset_ready(cfn, stack_name, cs_name, timeout=300):
    """等变更集创建完成。返回 (status, description)。"""
    deadline = time.time() + timeout
    while True:
        desc = cfn.describe_change_set(StackName=stack_name, ChangeSetName=cs_name)
        status = desc.get('Status', '')
        if status in ('CREATE_COMPLETE', 'FAILED'):
            return status, desc
        if time.time() > deadline:
            return 'TIMEOUT', desc
        time.sleep(5)


def _no_changes(desc):
    reason = (desc.get('StatusReason') or '').lower()
    return "didn't contain changes" in reason or 'no updates are to be performed' in reason


# ===== 健康检查 =====

def _health_event():
    """构造一个最小可用的 API Gateway REST 代理事件，供 mangum 解析。"""
    return {
        'resource': '/{proxy+}',
        'path': '/api/health',
        'httpMethod': 'GET',
        'headers': {'Host': 'localhost', 'X-Forwarded-Proto': 'https'},
        'multiValueHeaders': {},
        'queryStringParameters': None,
        'multiValueQueryStringParameters': None,
        'pathParameters': {'proxy': 'api/health'},
        'stageVariables': None,
        'requestContext': {
            'resourceId': 'health',
            'resourcePath': '/{proxy+}',
            'httpMethod': 'GET',
            'path': '/api/health',
            'stage': 'prod',
            'requestId': 'auto-upgrade-health-check',
            'identity': {'sourceIp': '127.0.0.1'},
            'protocol': 'HTTP/1.1',
        },
        'body': None,
        'isBase64Encoded': False,
    }


def health_check(lambda_client, function_name, context=None, retries=3, delay=10):
    """升级完成后验证 Web Lambda 能正常起来。

    CloudFormation 只保证"基础设施更新成功"。如果新代码有导入错误或运行时
    bug，栈状态依然是 UPDATE_COMPLETE，而用户面对的是一个白屏控制台。
    这一步就是为了把这种情况变成可自动恢复的。

    每次尝试前都检查剩余时间（包括第一次）：新版本 Web Lambda 如果是"挂住"
    而不是"快速报错"，单次 invoke 最坏要 connect+read 秒，三次加重试间隔足以
    超出收尾预算，把 updater 自己拖到超时被掐——那时既没记录也没回退。
    预算不足时返回失败，但 detail 里注明原因，便于事后区分"新版本真的坏了"
    和"没来得及查"。

    返回 (ok: bool, detail: str)
    """
    budget_ms = _HEALTH_ATTEMPT_MS + _FINISH_TAIL_MS
    last_detail = ''
    for attempt in range(1, retries + 1):
        remaining = context.get_remaining_time_in_millis() if context else None
        if remaining is not None and remaining < budget_ms:
            give_up = (f'预算不足放弃重试（剩余 {remaining // 1000}s，'
                       f'第 {attempt}/{retries} 次未执行）')
            logger.warning(f'健康检查{give_up}')
            return False, f'{last_detail}；{give_up}' if last_detail else give_up
        try:
            resp = lambda_client.invoke(
                FunctionName=function_name,
                InvocationType='RequestResponse',
                Payload=json.dumps(_health_event()).encode('utf-8'),
            )
            if resp.get('FunctionError'):
                raw = resp['Payload'].read().decode('utf-8', 'replace')
                last_detail = f'Lambda 执行错误: {raw[:400]}'
            else:
                payload = json.loads(resp['Payload'].read().decode('utf-8', 'replace'))
                code = payload.get('statusCode')
                if code == 200:
                    return True, 'ok'
                last_detail = f'/api/health 返回 statusCode={code}, body={str(payload.get("body"))[:300]}'
        except Exception as e:  # noqa: BLE001 - 任何异常都算健康检查失败
            last_detail = f'调用 Web Lambda 失败: {e}'
        logger.warning(f'健康检查第 {attempt}/{retries} 次失败: {last_detail}')
        if attempt < retries:
            time.sleep(delay)
    return False, last_detail


# ===== 升级执行 =====

def _apply_revision(cfn, s3, stack_name, owner, repo, bucket, region,
                    target_sha, role_arn, reason):
    """把栈更新到指定 commit SHA。

    返回 (ok, info)。info 含 changeset_name / blocked_reasons / error。
    """
    # 下载目标版本自己的 template.yaml —— 模板和代码必须同源，
    # 否则新代码可能依赖当前模板还没有的资源。
    try:
        template_body = download_file(owner, repo, target_sha, 'template.yaml')
    except Exception as e:  # noqa: BLE001
        return False, {'error': f'下载 template.yaml 失败: {e}'}

    key = f'templates/{target_sha}.yaml'
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=template_body)
    except ClientError as e:
        return False, {'error': f'上传模板到 S3 失败: {e}'}

    template_url = _template_url(bucket, key, region)
    _, stack = _stack_status(cfn, stack_name)
    params = _build_parameters(cfn, stack, template_url, target_sha)

    cs_name = f'auto-upgrade-{target_sha[:12]}-{int(time.time())}'
    create_kwargs = {
        'StackName': stack_name,
        'ChangeSetName': cs_name,
        'TemplateURL': template_url,
        'Parameters': params,
        'Capabilities': ['CAPABILITY_NAMED_IAM'],
        'ChangeSetType': 'UPDATE',
        'Description': reason[:1024],
    }
    if role_arn:
        create_kwargs['RoleARN'] = role_arn

    try:
        cfn.create_change_set(**create_kwargs)
    except ClientError as e:
        return False, {'error': f'创建变更集失败: {e}'}

    status, desc = _wait_changeset_ready(cfn, stack_name, cs_name)
    if status != 'CREATE_COMPLETE':
        _delete_changeset(cfn, stack_name, cs_name)
        if _no_changes(desc):
            return False, {'no_changes': True}
        return False, {'error': f'变更集创建失败({status}): {desc.get("StatusReason", "")}'}

    safe, reasons = check_changeset_safety(desc.get('Changes') or [])
    if not safe:
        _delete_changeset(cfn, stack_name, cs_name)
        return False, {'blocked_reasons': reasons}

    try:
        cfn.execute_change_set(StackName=stack_name, ChangeSetName=cs_name)
    except ClientError as e:
        _delete_changeset(cfn, stack_name, cs_name)
        return False, {'error': f'执行变更集失败: {e}'}

    return True, {'changeset_name': cs_name}


def _finish(cfn, lambda_client, stack_name, upgrade_id, ctx_info, context):
    """等栈更新结束 → 健康检查 → 成功记录 / 失败回退。

    ctx_info 需包含 target_sha / target_tag / from_sha / owner / repo /
    bucket / region / role_arn / is_rollback / hop。
    """
    status, timed_out = _wait_terminal(cfn, stack_name, context)

    if timed_out:
        hop = ctx_info.get('hop', 0) + 1
        if hop > MAX_WATCH_HOPS:
            msg = f'栈更新超过最长观察时间（{MAX_WATCH_HOPS} 跳），停止跟踪，当前状态 {status}'
            record_history(upgrade_id, status=STATUS_FAILED, error=msg,
                           finished_at=_iso(), **_hist_base(ctx_info))
            save_config(last_status=STATUS_FAILED, last_error=msg, current_upgrade_id='')
            notify(f'⚠️ Bedrock Cost Guard 自动升级异常\n{msg}')
            return {'status': STATUS_FAILED, 'error': msg}
        _self_invoke({'action': 'watch', 'upgrade_id': upgrade_id,
                      'hop': hop, 'ctx': ctx_info})
        return {'status': STATUS_UPDATING, 'stack_status': status, 'hop': hop}

    is_rollback = ctx_info.get('is_rollback', False)
    target_sha = ctx_info.get('target_sha', '')
    target_tag = ctx_info.get('target_tag', '')

    # --- CloudFormation 层面失败：它已经自动回滚，代码仍是旧版本 ---
    if status not in _TERMINAL_OK:
        rolled_back = status == 'UPDATE_ROLLBACK_COMPLETE'
        if rolled_back:
            msg = f'CloudFormation 更新失败，状态 {status}（已自动回滚到升级前的版本）'
        else:
            msg = (f'CloudFormation 更新失败，状态 {status}（回滚未完成，'
                   f'需要在 CloudFormation 控制台执行 continue-update-rollback）')
        final = STATUS_ROLLBACK_FAILED if is_rollback else STATUS_FAILED
        record_history(upgrade_id, status=final, error=msg, finished_at=_iso(),
                       **_hist_base(ctx_info))
        save_config(last_status=final, last_error=msg, current_upgrade_id='')
        notify(f'⚠️ Bedrock Cost Guard 自动升级失败\n目标版本：{target_tag or target_sha[:7]}\n{msg}')
        return {'status': final, 'error': msg}

    # --- 应用层面健康检查 ---
    web_fn = _env('WEB_FUNCTION_NAME')
    ok, detail = (True, 'skipped')
    if web_fn:
        ok, detail = health_check(lambda_client, web_fn, context)

    if ok:
        final = STATUS_ROLLED_BACK if is_rollback else STATUS_SUCCESS
        record_history(upgrade_id, status=final, finished_at=_iso(), **_hist_base(ctx_info))
        updates = {'last_status': final, 'last_error': '', 'current_upgrade_id': ''}
        # 只有健康检查通过的版本才配得上成为回退基准
        if not is_rollback:
            updates['last_known_good_sha'] = target_sha
        save_config(**updates)
        if is_rollback:
            notify(f'✅ Bedrock Cost Guard 已回退到上一个可用版本\n当前版本：{target_tag or target_sha[:7]}')
        return {'status': final}

    # --- 健康检查失败 ---
    if is_rollback:
        msg = f'回退后健康检查仍失败，需要人工介入：{detail}'
        record_history(upgrade_id, status=STATUS_ROLLBACK_FAILED, error=msg,
                       finished_at=_iso(), **_hist_base(ctx_info))
        save_config(last_status=STATUS_ROLLBACK_FAILED, last_error=msg, current_upgrade_id='')
        notify(f'🚨 Bedrock Cost Guard 回退失败，请人工检查\n{msg}')
        return {'status': STATUS_ROLLBACK_FAILED, 'error': msg}

    good_sha = ctx_info.get('from_sha') or get_config().get('last_known_good_sha')
    msg = f'升级后健康检查失败：{detail}'
    logger.error(msg)

    record_history(upgrade_id, status=STATUS_FAILED, error=msg, finished_at=_iso(),
                   **_hist_base(ctx_info))

    if not good_sha:
        # last_error 是前端唯一会显示的字段，得把"为什么没自动回退"带上
        no_rb = f'{msg}（无可用回退版本，需要人工升级或回退）'
        save_config(last_status=STATUS_FAILED, last_error=no_rb, current_upgrade_id='')
        notify(f'🚨 Bedrock Cost Guard 升级后健康检查失败，且没有可回退的版本\n{msg}')
        return {'status': STATUS_FAILED, 'error': no_rb}

    # --- 回退：独占一跳，不在当前收尾预算里做 ---
    # 走到这里时剩余时间刚过 _RESERVE_MS（约 90 秒），而回退要下载模板、
    # 上传 S3、建变更集、等变更集就绪（_wait_changeset_ready 自带 300 秒上限
    # 且不感知 context），90 秒内大概率跑不完。就地做会被掐在"变更集已创建
    # 甚至已执行、但没人观察"的中间态。所以先把状态落库再异步触发，让回退
    # 在一个全新的 15 分钟预算里跑。
    rb_id = _iso()
    rb_ctx = {
        'target_sha': good_sha, 'target_tag': '', 'from_sha': target_sha,
        'is_rollback': True, 'hop': 0,
    }
    record_history(rb_id, status=STATUS_UPDATING, started_at=rb_id,
                   from_sha=target_sha, to_sha=good_sha, to_tag='', is_rollback='true')
    # 先落状态再自调用：自调用本身也可能丢（invoke 报错、Lambda 被掐）。
    # 留下 rb_id + UPDATING，配合 current_upgrade_id 的失效判定才能恢复。
    save_config(last_status=STATUS_UPDATING, last_error=msg, current_upgrade_id=rb_id)
    notify(f'⚠️ Bedrock Cost Guard 升级后健康检查失败，正在自动回退\n'
           f'失败版本：{target_tag or target_sha[:7]}\n回退到：{good_sha[:7]}\n{detail}')

    try:
        _self_invoke({'action': 'rollback', 'upgrade_id': rb_id, 'ctx': rb_ctx})
    except Exception as e:  # noqa: BLE001 - 触发失败必须让人知道
        err = f'触发自动回退失败: {e}'
        logger.error(err)
        record_history(rb_id, status=STATUS_ROLLBACK_FAILED, error=err,
                       finished_at=_iso(), from_sha=target_sha, to_sha=good_sha,
                       to_tag='', is_rollback='true')
        save_config(last_status=STATUS_ROLLBACK_FAILED, last_error=err, current_upgrade_id='')
        notify(f'🚨 Bedrock Cost Guard 自动回退没能启动，请人工介入\n{err}')
        return {'status': STATUS_ROLLBACK_FAILED, 'error': err}

    return {'status': STATUS_UPDATING, 'rollback_id': rb_id}


def _hist_base(ctx_info):
    return {
        'from_sha': ctx_info.get('from_sha', ''),
        'to_sha': ctx_info.get('target_sha', ''),
        'to_tag': ctx_info.get('target_tag', ''),
        'is_rollback': 'true' if ctx_info.get('is_rollback') else 'false',
    }


def _self_invoke(payload):
    """异步调用自己继续观察栈状态（跨越单次 Lambda 的 15 分钟上限）。"""
    fn = _env('AWS_LAMBDA_FUNCTION_NAME')
    if not fn:
        logger.warning('无 AWS_LAMBDA_FUNCTION_NAME，跳过自调用')
        return
    # 短超时 client：这次 invoke 跑在收尾预算里，挂住同样会把函数拖到超时。
    _lambda_client().invoke(
        FunctionName=fn, InvocationType='Event',
        Payload=json.dumps(payload).encode('utf-8'))


def _ctx_from_env(raw):
    """重建续跑/回退的上下文：基础设施参数一律取环境变量，不信 event。

    watch 和 rollback 都由异步自调用触发，而 Lambda 的调用入口不止自己一个。
    owner / repo / bucket / role_arn 决定了"从哪拉代码、用什么权限改栈"，
    这些必须来自模板注入的环境变量。event 中的版本信息只用于标识已创建的
    升级流程；rollback 不做方向 compare，因此执行前还必须与 DDB 中对应的
    回退历史绑定，不能仅凭事件里的 target_sha 执行。
    """
    return {
        'target_sha': raw.get('target_sha', ''),
        'target_tag': raw.get('target_tag', ''),
        'from_sha': raw.get('from_sha', ''),
        'is_rollback': bool(raw.get('is_rollback')),
        'hop': raw.get('hop', 0),
        'owner': _env('GITHUB_OWNER'),
        'repo': _env('GITHUB_REPO'),
        'bucket': _env('CODE_BUCKET'),
        'region': _env('AWS_REGION', 'us-east-1'),
        'role_arn': _env('STACK_UPDATE_ROLE_ARN'),
    }


# ===== 主流程 =====

def check_and_upgrade(event, context):
    global _OWNED_LOCK_ID

    forced = event.get('action') == 'upgrade_now'
    stack_name = _env('STACK_NAME')
    owner = _env('GITHUB_OWNER')
    repo = _env('GITHUB_REPO')
    bucket = _env('CODE_BUCKET')
    region = _env('AWS_REGION', 'us-east-1')
    role_arn = _env('STACK_UPDATE_ROLE_ARN')

    cfg = get_config()
    now_iso = _iso()

    if not forced and not cfg['enabled']:
        save_config(last_check_at=now_iso, last_status=STATUS_SKIPPED, last_error='')
        return {'status': STATUS_SKIPPED, 'reason': '自动升级已关闭'}

    cfn = _cfn_client()
    lambda_client = _lambda_client()
    s3 = _s3_client()

    # 栈正在变更时不叠加新的升级
    status, stack = _stack_status(cfn, stack_name)
    if _is_busy(status):
        msg = f'栈当前状态 {status}，跳过本次升级'
        save_config(last_check_at=now_iso)
        return {'status': STATUS_SKIPPED, 'reason': msg}

    current_sha, current_tag = get_current_version()

    try:
        rel = get_latest_release(owner, repo)
    except ReleaseNotFound as e:
        save_config(last_check_at=now_iso, last_status=STATUS_BLOCKED, last_error=str(e))
        logger.warning(str(e))
        return {'status': STATUS_BLOCKED, 'reason': str(e)}
    except Exception as e:  # noqa: BLE001 - GitHub 不可达等
        msg = f'获取最新 Release 失败: {e}'
        save_config(last_check_at=now_iso, last_status=STATUS_FAILED, last_error=msg)
        logger.warning(msg)
        return {'status': STATUS_FAILED, 'error': msg}

    target_sha, target_tag = rel['sha'], rel['tag']

    if target_sha == current_sha:
        save_config(last_check_at=now_iso, last_status=STATUS_NO_UPDATE, last_error='',
                    last_known_good_sha=cfg['last_known_good_sha'] or current_sha)
        record_history(now_iso, status=STATUS_NO_UPDATE, from_sha=current_sha,
                       to_sha=target_sha, to_tag=target_tag, is_rollback='false')
        return {'status': STATUS_NO_UPDATE, 'version': target_tag}

    if current_sha:
        # 只有 'ahead' 才升级：把"不同"细分为新版 / 回退 / 分叉
        try:
            cmp_result = compare_commits(owner, repo, current_sha, target_sha)
        except Exception as e:  # noqa: BLE001
            msg = f'比较版本失败: {e}'
            save_config(last_check_at=now_iso, last_status=STATUS_FAILED, last_error=msg)
            return {'status': STATUS_FAILED, 'error': msg}

        if cmp_result['status'] != 'ahead':
            msg = (f'最新 Release {target_tag} 相对当前版本为 "{cmp_result["status"]}"，'
                   f'不是更新的版本，已跳过（避免降级或跨分叉部署）')
            save_config(last_check_at=now_iso, last_status=STATUS_BLOCKED, last_error=msg)
            record_history(now_iso, status=STATUS_BLOCKED, from_sha=current_sha,
                           to_sha=target_sha, to_tag=target_tag, error=msg, is_rollback='false')
            logger.warning(msg)
            return {'status': STATUS_BLOCKED, 'reason': msg}
        changelog = rel['notes'] or '\n'.join(f'- {m}' for m in cmp_result['commits'])
        commit_count = cmp_result['ahead_by']
    else:
        # 当前版本未知（build_info 缺失，例如早于本功能的旧部署）。
        # 此时没有基准 SHA，做不了方向判定，但"停在未知版本永不更新"比
        # "升级到最新正式 Release"风险更大——后者至少是经过 CI 和发版
        # 流程验证的版本，而且升级后还有健康检查兜底。所以视为过期直接升级。
        logger.info(f'当前版本未知，视为过期，升级到最新 Release {target_tag}')
        changelog = rel['notes']
        commit_count = 0
    upgrade_id = now_iso
    ctx_info = {
        'target_sha': target_sha, 'target_tag': target_tag, 'from_sha': current_sha,
        'owner': owner, 'repo': repo, 'bucket': bucket, 'region': region,
        'role_arn': role_arn, 'is_rollback': False, 'hop': 0,
    }

    record_history(upgrade_id, status=STATUS_UPDATING, started_at=now_iso,
                   from_sha=current_sha, from_tag=current_tag, to_sha=target_sha,
                   to_tag=target_tag, changelog=changelog[:8000],
                   commit_count=commit_count, is_rollback='false')
    save_config(last_check_at=now_iso, last_status=STATUS_UPDATING,
                last_error='', current_upgrade_id=upgrade_id,
                last_known_good_sha=cfg['last_known_good_sha'] or current_sha)
    _OWNED_LOCK_ID = upgrade_id

    ok, info = _apply_revision(cfn, s3, stack_name, owner, repo, bucket, region,
                               target_sha, role_arn,
                               f'auto upgrade {current_sha[:12]} -> {target_tag}')
    if not ok:
        if info.get('no_changes'):
            save_config(last_status=STATUS_NO_UPDATE, last_error='', current_upgrade_id='')
            record_history(upgrade_id, status=STATUS_NO_UPDATE, from_sha=current_sha,
                           to_sha=target_sha, to_tag=target_tag, is_rollback='false')
            return {'status': STATUS_NO_UPDATE}

        if info.get('blocked_reasons'):
            reasons = '; '.join(info['blocked_reasons'])
            msg = f'变更集会破坏有状态资源，已拒绝自动执行：{reasons}'
            save_config(last_status=STATUS_BLOCKED, last_error=msg, current_upgrade_id='')
            record_history(upgrade_id, status=STATUS_BLOCKED, error=msg,
                           finished_at=_iso(), **_hist_base(ctx_info))
            notify(f'⚠️ Bedrock Cost Guard 自动升级被安全策略阻断\n'
                   f'目标版本：{target_tag}\n{msg}\n需要人工确认后手动升级。')
            return {'status': STATUS_BLOCKED, 'reason': msg}

        err = info.get('error', '未知错误')
        save_config(last_status=STATUS_FAILED, last_error=err, current_upgrade_id='')
        record_history(upgrade_id, status=STATUS_FAILED, error=err,
                       finished_at=_iso(), **_hist_base(ctx_info))
        notify(f'⚠️ Bedrock Cost Guard 自动升级失败\n目标版本：{target_tag}\n{err}')
        return {'status': STATUS_FAILED, 'error': err}

    return _finish(cfn, lambda_client, stack_name, upgrade_id, ctx_info, context)


def watch(event, context):
    """续跑：继续观察一次已经在执行中的栈更新。"""
    global _OWNED_LOCK_ID

    upgrade_id = event.get('upgrade_id') or ''
    current = get_config().get('current_upgrade_id') or ''
    if not upgrade_id or upgrade_id != current:
        reason = (f'watch 请求与当前升级锁不匹配，忽略'
                  f'（event={upgrade_id!r}, lock={current!r}）')
        logger.warning(reason)
        return {'status': STATUS_IGNORED, 'reason': reason}

    _OWNED_LOCK_ID = upgrade_id
    ctx_info = _ctx_from_env(event.get('ctx') or {})
    ctx_info['hop'] = event.get('hop', 0)
    return _finish(_cfn_client(), _lambda_client(), _env('STACK_NAME'),
                   upgrade_id, ctx_info, context)


def rollback(event, context):
    """执行一次回退，独占一个完整的 Lambda 时间预算。

    由 _finish 的健康检查失败分支异步触发。拆出来是因为回退本身就是一次
    完整的整栈更新（下载模板 + 建变更集 + 等就绪 + 执行 + 观察），塞不进
    上一跳剩下的那点收尾时间。状态在触发前已经落库，这里只管执行。
    """
    global _OWNED_LOCK_ID

    ctx_info = _ctx_from_env(event.get('ctx') or {})
    ctx_info['is_rollback'] = True
    upgrade_id = event.get('upgrade_id') or ''
    stack_name = _env('STACK_NAME')
    good_sha = ctx_info['target_sha']

    # rollback 没有 compare 方向检查，必须把事件与 _finish 预写的回退记录
    # 原子绑定；同一条件写也让 at-least-once 的重复投递只有一个执行者。
    if not upgrade_id or not good_sha:
        reason = '回退请求缺少 upgrade_id 或 target_sha，忽略'
        logger.warning(reason)
        return {'status': STATUS_IGNORED, 'reason': reason}

    claim_result, record = _claim_rollback(upgrade_id, good_sha)
    if claim_result == 'duplicate':
        return {
            'status': STATUS_SKIPPED,
            'reason': '回退请求已由另一 invocation 认领，忽略重复投递',
        }
    if claim_result != 'claimed':
        reason = '回退请求与已落库的升级记录不匹配，忽略'
        logger.warning(
            f'{reason}: upgrade_id={upgrade_id!r}, target_sha={good_sha!r}, '
            f'record_status={record.get("status")!r}, '
            f'record_to_sha={record.get("to_sha")!r}')
        return {'status': STATUS_IGNORED, 'reason': reason}

    # 只有完成 DDB 原子认领的 invocation 才有资格在异常时释放该流程的锁。
    _OWNED_LOCK_ID = upgrade_id

    def _fail(err):
        logger.error(err)
        merge_history(upgrade_id, status=STATUS_ROLLBACK_FAILED, error=err,
                      finished_at=_iso())
        save_config(last_status=STATUS_ROLLBACK_FAILED, last_error=err, current_upgrade_id='')
        notify(f'🚨 Bedrock Cost Guard 自动回退失败，请人工介入\n{err}')
        return {'status': STATUS_ROLLBACK_FAILED, 'error': err}

    cfn = _cfn_client()
    # 原子认领已过滤同一 rb_id 的重复投递；若此处仍 busy，通常是外部
    # CloudFormation 操作恰好抢先。按保守策略不改历史、不清锁，最终由六小时
    # 锁失效机制恢复，避免把可能正在执行的栈操作误报为回退失败。
    status, _ = _stack_status(cfn, stack_name)
    if _is_busy(status):
        return {
            'status': STATUS_SKIPPED,
            'reason': f'栈状态 {status}，暂不执行回退',
        }

    ok, info = _apply_revision(cfn, _s3_client(), stack_name, ctx_info['owner'],
                               ctx_info['repo'], ctx_info['bucket'], ctx_info['region'],
                               good_sha, ctx_info['role_arn'],
                               f'auto rollback to {good_sha[:12]}')
    if not ok:
        if info.get('no_changes'):
            return _fail('回退变更集为空：栈已经是该版本，但健康检查仍失败，'
                         '说明问题不在代码版本上')
        if info.get('blocked_reasons'):
            return _fail(f'回退被安全策略阻断: {"; ".join(info["blocked_reasons"])}')
        return _fail(info.get('error', '未知错误'))

    return _finish(cfn, _lambda_client(), stack_name, upgrade_id, ctx_info, context)


def _release_lock(action, exc):
    """未捕获异常时仅释放当前 invocation 自己持有的升级锁并告警。

    没有这一步，持锁流程异常时 current_upgrade_id 会一直留在 DDB；但不能
    根据"DDB 里现在有什么"释放锁，否则无锁的并发 check 会误杀另一次正常
    升级。Lambda 超时不会进入 except，仍由锁的失效判定兜底。

    锁释放使用 DDB 条件更新，避免“读到自己的锁后、写入前被新锁替换”的
    TOCTOU。清理失败不能盖掉原始异常，那才是根因。
    """
    msg = f'升级流程异常中断（action={action}）: {exc}'
    logger.exception(msg)
    try:
        released = bool(_OWNED_LOCK_ID) and _release_owned_lock(_OWNED_LOCK_ID, msg)
        if released:
            # merge 而非覆盖：record_upgrade 是整条 put，直接写会把 changelog、
            # from_sha 等诊断信息抹掉。
            merge_history(_OWNED_LOCK_ID, status=STATUS_FAILED, error=msg,
                          finished_at=_iso())
        else:
            logger.warning(
                f'异常 invocation 不持有当前升级锁，不修改 DDB: '
                f'owned={_OWNED_LOCK_ID!r}')
    except Exception as cleanup_exc:  # noqa: BLE001 - 原始异常优先
        logger.error(f'异常兜底清理自身失败（原始异常仍会抛出）: {cleanup_exc}')

    # 即使 DDB 清理自身失败也要尽量发出原始异常告警；notify 内部自行容错。
    notify(f'⚠️ Bedrock Cost Guard 自动升级异常中断\n{msg}\n'
           f'请在 CloudFormation 控制台确认栈状态。')


def handler(event, context):
    global _OWNED_LOCK_ID
    _OWNED_LOCK_ID = None  # Lambda 执行环境复用，不能继承上次 invocation 的锁

    event = event or {}
    action = event.get('action') or 'check'
    logger.info(f'updater action={action}')

    try:
        if action == 'watch':
            return watch(event, context)
        if action == 'rollback':
            return rollback(event, context)
        if action in ('check', 'upgrade_now'):
            return check_and_upgrade(event, context)
        return {'status': 'IGNORED', 'reason': f'未知 action: {action}'}
    except Exception as exc:  # noqa: BLE001
        _release_lock(action, exc)
        # 继续抛出：保留 CloudWatch 的 Errors 指标和完整堆栈。
        # 异步重试已在模板里关掉（EventInvokeConfig MaximumRetryAttempts: 0），
        # 否则一次崩溃会重复走三遍兜底、发三条相同的告警。
        raise
