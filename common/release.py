"""GitHub Release 通道：版本发现、方向判定与文件下载。

自动升级只跟随 GitHub Release，**不跟随分支**。这意味着日常 push 到 main
不会影响任何已部署的栈，只有维护者主动发布 Release 才会触发升级。

`/releases/latest` 端点会自动跳过 draft 和 prerelease，所以：
  - draft   → 已打 tag、CI 已验证，但一个用户都收不到（暂存区）
  - prerelease → 维护者可在自己的栈上先验证，普通用户看不见

所有对外暴露的版本都锁定到不可变的 commit SHA，绝不使用可能移动的
分支引用（如 refs/heads/main.zip）。
"""
import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

GITHUB_API = 'https://api.github.com'
RAW_BASE = 'https://raw.githubusercontent.com'
_UA = 'bedrock-cost-guard'
_TIMEOUT = 10


class ReleaseNotFound(Exception):
    """仓库没有任何正式 release（draft / prerelease 不算）。

    这是一个明确的失败，调用方**不应**回退到跟随分支——那等于绕过整个
    Release 机制，会让"只有主动发版才上线"的保证失效。
    """


def _get(url, accept='application/vnd.github+json', timeout=_TIMEOUT):
    req = urllib.request.Request(url, headers={'User-Agent': _UA, 'Accept': accept})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', 'replace')


def resolve_ref_to_sha(owner, repo, ref, timeout=_TIMEOUT):
    """把 tag / 分支名 / 短 SHA 解析成完整的 commit SHA。

    commits/{ref} 端点同时接受 tag、分支名和 SHA；配合
    Accept: application/vnd.github.sha 直接返回纯 SHA 字符串。
    """
    url = f'{GITHUB_API}/repos/{owner}/{repo}/commits/{ref}'
    return _get(url, accept='application/vnd.github.sha', timeout=timeout).strip()


def get_latest_release(owner, repo, timeout=_TIMEOUT):
    """取最新的正式 release，并锁定到不可变 commit SHA。

    返回 dict: {tag, sha, name, notes, published_at}
    抛 ReleaseNotFound: 仓库还没有正式 release（HTTP 404）
    """
    url = f'{GITHUB_API}/repos/{owner}/{repo}/releases/latest'
    try:
        raw = _get(url, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ReleaseNotFound(
                f'{owner}/{repo} 没有已发布的 Release。自动升级只跟随 Release，'
                f'请先在该仓库创建一个 Release（draft / prerelease 不算）。'
            ) from e
        raise

    data = json.loads(raw)
    tag = (data.get('tag_name') or '').strip()
    if not tag:
        raise ReleaseNotFound(f'{owner}/{repo} 的 latest release 缺少 tag_name')

    # target_commitish 经常是分支名（如 "main"）而非 SHA，不可直接当版本用，
    # 必须再解析一次 tag 才能拿到不可变的 commit SHA。
    sha = resolve_ref_to_sha(owner, repo, tag, timeout=timeout)

    return {
        'tag': tag,
        'sha': sha,
        'name': (data.get('name') or tag).strip(),
        'notes': (data.get('body') or '').strip(),
        'published_at': data.get('published_at') or '',
    }


def compare_commits(owner, repo, base, head, timeout=_TIMEOUT):
    """比较两个 commit，判断 head 相对 base 的方向。

    返回 dict: {status, ahead_by, behind_by, commits}
      status:
        'ahead'     — head 比 base 新（唯一允许自动升级的情况）
        'behind'    — head 比 base 旧（回退，不自动执行）
        'identical' — 相同
        'diverged'  — 分叉（历史被改写或换了分支）
      commits: 每条 commit message 的首行，用作 changelog 兜底

    仅凭 `latest_sha != current_sha` 无法区分"新版本"和"回退/分叉"，
    所以自动升级前必须走这一步。
    """
    url = f'{GITHUB_API}/repos/{owner}/{repo}/compare/{base}...{head}'
    data = json.loads(_get(url, timeout=timeout))
    messages = []
    for c in data.get('commits') or []:
        msg = ((c.get('commit') or {}).get('message') or '').strip()
        if msg:
            messages.append(msg.splitlines()[0])
    return {
        'status': data.get('status') or '',
        'ahead_by': int(data.get('ahead_by') or 0),
        'behind_by': int(data.get('behind_by') or 0),
        'commits': messages,
    }


def download_file(owner, repo, sha, path, timeout=30):
    """下载指定 commit 下的单个文件（用于取该版本的 template.yaml）。

    必须传具体 SHA 而不是分支名，保证拿到的模板与将要部署的代码同源。
    """
    url = f'{RAW_BASE}/{owner}/{repo}/{sha}/{path}'
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()
