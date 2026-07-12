"""IAM Bedrock 权限扫描。

扫描账号内所有 IAM Users/Roles/Groups，找出拥有"能产生 Bedrock 调用/费用"
权限的身份（排除只读的 List/Get/Describe）。由 web 的 /api/iam-scan 异步触发，
在无 API Gateway 29s 超时限制的 Lambda 自调用上下文中执行。

从 web/app.py 抽出，使 web app 只保留 route handler，扫描逻辑集中在此。
"""
import logging

import boto3
from botocore.config import Config as BotoConfig

from common.config import put_item, query_by_pk, _get_table

_IAM_CONFIG = BotoConfig(retries={'max_attempts': 10, 'mode': 'adaptive'})

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 能产生调用/费用的 Bedrock Action 前缀（排除只读的 List/Get/Describe）
_DANGEROUS_PREFIXES = (
    'bedrock:invoke',
    'bedrock:createmodelinvocation',
    'bedrock:createmodelcustomization',
    'bedrock:createprovisionedmodel',
    'bedrock:applyguardrail',
    'bedrock:retrieve',
    'bedrock:conversestream',
    'bedrock:converse',
)


def _is_dangerous_action(action):
    """判断一个 action 是否能产生 Bedrock 调用/费用。"""
    lower = action.lower()
    if lower == '*' or lower == 'bedrock:*':
        return True
    return any(lower.startswith(p) for p in _DANGEROUS_PREFIXES)


def _extract_bedrock_actions(policy_doc):
    """从策略文档提取能产生 Bedrock 调用的危险 Action。返回 set of action strings。"""
    actions = set()
    if not isinstance(policy_doc, dict):
        return actions
    for stmt in policy_doc.get('Statement', []):
        if stmt.get('Effect') != 'Allow':
            continue
        stmt_actions = stmt.get('Action', [])
        if isinstance(stmt_actions, str):
            stmt_actions = [stmt_actions]
        for action in stmt_actions:
            lower = action.lower()
            # 先匹配 bedrock 相关或全权限
            if not (lower.startswith('bedrock:') or lower == '*'):
                continue
            # 只保留能产生调用/费用的
            if _is_dangerous_action(action):
                actions.add(action)
    return actions


def _check_managed_policy(iam, policy_arn):
    """检查托管策略是否包含 Bedrock 权限。返回 actions set。"""
    try:
        policy = iam.get_policy(PolicyArn=policy_arn)['Policy']
        version_id = policy['DefaultVersionId']
        doc = iam.get_policy_version(PolicyArn=policy_arn, VersionId=version_id)['PolicyVersion']['Document']
        return _extract_bedrock_actions(doc)
    except Exception as e:
        # 读不到策略（无权限/已删除）→ 当作无 Bedrock 权限，但记一笔避免静默漏报
        logger.warning(f"Cannot read managed policy {policy_arn}, treating as no bedrock access: {e}")
        return set()


def _check_group_policies(iam, group_name):
    """检查组的所有策略，返回 (actions_set, policy_sources_list)。"""
    bedrock_actions = set()
    policy_sources = []

    # 组附加的托管策略
    attached = iam.list_attached_group_policies(GroupName=group_name)['AttachedPolicies']
    for p in attached:
        actions = _check_managed_policy(iam, p['PolicyArn'])
        if actions:
            bedrock_actions.update(actions)
            policy_sources.append({'name': p['PolicyName'], 'arn': p['PolicyArn'], 'type': 'managed'})

    # 组内联策略
    inline_names = iam.list_group_policies(GroupName=group_name)['PolicyNames']
    for pname in inline_names:
        doc = iam.get_group_policy(GroupName=group_name, PolicyName=pname)['PolicyDocument']
        actions = _extract_bedrock_actions(doc)
        if actions:
            bedrock_actions.update(actions)
            policy_sources.append({'name': pname, 'type': 'inline'})

    return bedrock_actions, policy_sources


def scan_iam_identities():
    """扫描所有 IAM Users/Roles/Groups，找出有 Bedrock 权限的身份。"""
    iam = boto3.client('iam', config=_IAM_CONFIG)
    results = []

    # --- 扫描 Users ---
    paginator = iam.get_paginator('list_users')
    for page in paginator.paginate():
        for user in page['Users']:
            user_name = user['UserName']
            bedrock_actions = set()
            policy_sources = []

            # 用户附加的托管策略
            attached = iam.list_attached_user_policies(UserName=user_name)['AttachedPolicies']
            for p in attached:
                actions = _check_managed_policy(iam, p['PolicyArn'])
                if actions:
                    bedrock_actions.update(actions)
                    policy_sources.append({'name': p['PolicyName'], 'arn': p['PolicyArn'], 'type': 'managed'})

            # 用户内联策略
            inline_names = iam.list_user_policies(UserName=user_name)['PolicyNames']
            for pname in inline_names:
                doc = iam.get_user_policy(UserName=user_name, PolicyName=pname)['PolicyDocument']
                actions = _extract_bedrock_actions(doc)
                if actions:
                    bedrock_actions.update(actions)
                    policy_sources.append({'name': pname, 'type': 'inline'})

            # 用户所属 Group 的策略
            groups = iam.list_groups_for_user(UserName=user_name)['Groups']
            for g in groups:
                g_actions, g_policies = _check_group_policies(iam, g['GroupName'])
                if g_actions:
                    bedrock_actions.update(g_actions)
                    for gp in g_policies:
                        gp['via_group'] = g['GroupName']
                    policy_sources.extend(g_policies)

            if bedrock_actions:
                results.append({
                    'identity_type': 'User',
                    'name': user_name,
                    'arn': user['Arn'],
                    'actions': sorted(bedrock_actions),
                    'policies': policy_sources,
                    'create_date': user['CreateDate'].isoformat(),
                })

    # --- 扫描 Roles ---
    paginator = iam.get_paginator('list_roles')
    for page in paginator.paginate():
        for role in page['Roles']:
            role_name = role['RoleName']
            # 跳过 AWS Service-Linked Roles
            if role.get('Path', '').startswith('/aws-service-role/'):
                continue

            bedrock_actions = set()
            policy_sources = []

            # 角色附加的托管策略
            attached = iam.list_attached_role_policies(RoleName=role_name)['AttachedPolicies']
            for p in attached:
                actions = _check_managed_policy(iam, p['PolicyArn'])
                if actions:
                    bedrock_actions.update(actions)
                    policy_sources.append({'name': p['PolicyName'], 'arn': p['PolicyArn'], 'type': 'managed'})

            # 角色内联策略
            inline_names = iam.list_role_policies(RoleName=role_name)['PolicyNames']
            for pname in inline_names:
                doc = iam.get_role_policy(RoleName=role_name, PolicyName=pname)['PolicyDocument']
                actions = _extract_bedrock_actions(doc)
                if actions:
                    bedrock_actions.update(actions)
                    policy_sources.append({'name': pname, 'type': 'inline'})

            if bedrock_actions:
                # 提取信任关系（谁能 assume 这个 role）
                trust = role.get('AssumeRolePolicyDocument', {})
                trust_principals = []
                for stmt in trust.get('Statement', []):
                    if stmt.get('Effect') == 'Allow':
                        principal = stmt.get('Principal', {})
                        if isinstance(principal, str):
                            trust_principals.append(principal)
                        else:
                            for k, v in principal.items():
                                if isinstance(v, list):
                                    trust_principals.extend(v)
                                else:
                                    trust_principals.append(v)

                results.append({
                    'identity_type': 'Role',
                    'name': role_name,
                    'arn': role['Arn'],
                    'actions': sorted(bedrock_actions),
                    'policies': policy_sources,
                    'trust_principals': trust_principals,
                    'create_date': role['CreateDate'].isoformat(),
                })

    # --- 扫描 Groups（独立列出有 Bedrock 权限的组）---
    paginator = iam.get_paginator('list_groups')
    for page in paginator.paginate():
        for group in page['Groups']:
            group_name = group['GroupName']
            bedrock_actions, policy_sources = _check_group_policies(iam, group_name)
            if bedrock_actions:
                # 获取组成员
                members = [u['UserName'] for u in iam.get_group(GroupName=group_name)['Users']]
                results.append({
                    'identity_type': 'Group',
                    'name': group_name,
                    'arn': group['Arn'],
                    'actions': sorted(bedrock_actions),
                    'policies': policy_sources,
                    'members': members,
                    'create_date': group['CreateDate'].isoformat(),
                })

    return results


def execute_iam_scan(scan_time):
    """实际执行 IAM 扫描（异步 invoke 时调用，无超时限制）。"""
    try:
        results = scan_iam_identities()

        # 先删除旧的扫描结果
        old_items = query_by_pk('IAM_SCAN')
        table = _get_table()
        for item in old_items:
            if item['SK'] != '_meta':
                table.delete_item(Key={'PK': item['PK'], 'SK': item['SK']})

        # 写入新结果
        user_count = sum(1 for r in results if r['identity_type'] == 'User')
        role_count = sum(1 for r in results if r['identity_type'] == 'Role')
        group_count = sum(1 for r in results if r['identity_type'] == 'Group')

        for r in results:
            sk = f"{r['identity_type'].lower()}/{r['name']}"
            put_item('IAM_SCAN', sk,
                     identity_type=r['identity_type'],
                     name=r['name'],
                     arn=r['arn'],
                     actions=r['actions'],
                     policies=r['policies'],
                     trust_principals=r.get('trust_principals'),
                     members=r.get('members'),
                     create_date=r.get('create_date'))

        # 更新 _meta 为完成
        put_item('IAM_SCAN', '_meta',
                 scan_time=scan_time,
                 status='done',
                 total_identities=str(len(results)),
                 user_count=str(user_count),
                 role_count=str(role_count),
                 group_count=str(group_count))

        return {'statusCode': 200, 'total': len(results)}
    except Exception as e:
        put_item('IAM_SCAN', '_meta',
                 scan_time=scan_time,
                 status='error',
                 error=str(e),
                 total_identities='0',
                 user_count='0', role_count='0', group_count='0')
        return {'statusCode': 500, 'error': str(e)}
