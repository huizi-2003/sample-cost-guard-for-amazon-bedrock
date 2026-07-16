"""中国法定节假日判断模块。

数据源：NateScarlet/holiday-cn（GitHub 开源项目，自动跟踪国务院公告）
URL 格式：https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json

JSON 数据结构：
  {
    "year": 2026,
    "days": [
      {"name": "元旦", "date": "2026-01-01", "isOffDay": true},   // 放假
      {"name": "春节", "date": "2026-02-14", "isOffDay": false},  // 调休上班
      ...
    ]
  }

判断逻辑（is_workday）：
  1. 如果日期在数据中且 isOffDay=true  → 休息日（不推送）
  2. 如果日期在数据中且 isOffDay=false → 调休上班日（推送）
  3. 如果日期不在数据中 → 看星期：周一~周五=工作日，周六日=休息日

容错（三级 fallback）：
  1. 内存缓存（24h TTL）
  2. GitHub 拉取 → 成功则写回内存 + DDB
  3. 拉取失败 → 过期内存缓存 → DDB 二级缓存 → None（退化为星期判断）

  节假日数据一年内几乎不变，旧数据远比纯星期判断可靠。
"""

import json
import logging
import urllib.request
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# 内存缓存：{year: {'data': {...}, 'fetched_at': timestamp}}
_cache = {}
CACHE_TTL = 86400  # 24 小时

HOLIDAY_URL_TEMPLATE = 'https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json'
REQUEST_TIMEOUT = 10  # 秒


def _fetch_holiday_data(year):
    """从 GitHub 拉取指定年份的节假日数据，返回 {date_str: isOffDay} 字典。

    失败返回 None。
    """
    url = HOLIDAY_URL_TEMPLATE.format(year=year)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'bedrock-cost-guard'})
        resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
        raw = json.loads(resp.read().decode('utf-8'))
        # 转为 {date_str: bool} 方便快速查找
        result = {}
        for day in raw.get('days', []):
            result[day['date']] = day['isOffDay']
        return result
    except Exception as e:
        logger.warning(f"Failed to fetch holiday data for {year}: {e}")
        return None


def _read_ddb_cache(year):
    """从 DDB 读取节假日缓存。失败返回 None（不抛异常）。"""
    try:
        from common.config import get_item
        item = get_item('HOLIDAY', str(year))
        if item and 'data' in item:
            return {
                'data': json.loads(item['data']),
                'fetched_at': float(item.get('fetched_at', 0)),
            }
    except Exception as e:
        logger.warning(f"DDB holiday cache read failed for {year}: {e}")
    return None


def _write_ddb_cache(year, data, fetched_at):
    """将节假日数据写入 DDB。失败仅打日志（不影响主逻辑）。"""
    try:
        from common.config import put_item
        put_item('HOLIDAY', str(year), data=json.dumps(data), fetched_at=str(fetched_at))
    except Exception as e:
        logger.warning(f"DDB holiday cache write failed for {year}: {e}")


def _get_holiday_map(year):
    """获取指定年份的节假日映射（三级缓存 fallback）。"""
    now_ts = datetime.now(timezone.utc).timestamp()

    # 1. 内存缓存命中且未过期 → 直接返回
    if year in _cache:
        entry = _cache[year]
        if now_ts - entry['fetched_at'] < CACHE_TTL:
            return entry['data']

    # 2. GitHub 拉取
    data = _fetch_holiday_data(year)
    if data is not None:
        _cache[year] = {'data': data, 'fetched_at': now_ts}
        try:
            _write_ddb_cache(year, data, now_ts)
        except Exception:
            pass  # _write_ddb_cache 内部已有日志，此处兜底防止未预期异常
        return data

    # 3. 拉取失败 → 过期内存缓存兜底（节假日数据年内基本不变，旧数据优于星期判断）
    if year in _cache:
        logger.warning(f"Holiday data refresh failed for {year}, using stale memory cache")
        return _cache[year]['data']

    # 4. 冷启动无内存 → DDB 二级缓存
    ddb_entry = _read_ddb_cache(year)
    if ddb_entry is not None:
        logger.warning(f"Holiday data refresh failed for {year}, using DDB cache")
        # 写回内存（保留 DDB 中的 fetched_at，保持"过期"状态以便下次重试 GitHub）
        _cache[year] = ddb_entry
        return ddb_entry['data']

    # 5. 全部失败 → None，is_workday 退化为星期判断
    return None


def is_workday(date):
    """判断给定日期是否为中国工作日。

    Args:
        date: datetime.date 对象（应为北京时间日期）

    Returns:
        True  — 工作日（应推送）
        False — 休息日（不推送）

    容错：API 失败时 fallback 到纯星期判断。
    """
    date_str = date.strftime('%Y-%m-%d')
    year = date.year

    # 年末日期可能被下一年文件覆盖（如12月的调休），需同时查当年和次年
    holiday_map = _get_holiday_map(year)

    if holiday_map is not None and date_str in holiday_map:
        # 数据中有这天的记录
        is_off = holiday_map[date_str]
        return not is_off  # isOffDay=true 则不是工作日

    # 检查次年数据（12月日期可能出现在次年文件中）
    if date.month == 12:
        next_year_map = _get_holiday_map(year + 1)
        if next_year_map is not None and date_str in next_year_map:
            is_off = next_year_map[date_str]
            return not is_off

    # 日期不在节假日数据中，fallback 到星期判断
    # weekday(): 0=Monday ... 6=Sunday
    return date.weekday() < 5


def clear_cache():
    """清除内存缓存（测试用）。"""
    _cache.clear()
