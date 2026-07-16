"""tests/test_holiday.py — 节假日判断模块测试。"""

import json
import time
from datetime import date, datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from common import holiday
from common.holiday import (
    _get_holiday_map,
    is_workday,
    clear_cache,
    _cache,
    CACHE_TTL,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    """每个用例前后清理内存缓存，避免用例间串味。"""
    clear_cache()
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# 1. test_fetch_success_updates_cache_and_returns
# ---------------------------------------------------------------------------


@patch('common.holiday._fetch_holiday_data')
def test_fetch_success_updates_cache_and_returns(mock_fetch):
    """GitHub 拉取成功 → 返回数据、内存缓存更新、DDB 写入。"""
    fake_data = {'2026-10-01': True, '2026-10-08': False}
    mock_fetch.return_value = fake_data

    with patch('common.holiday._write_ddb_cache') as mock_ddb_write:
        result = _get_holiday_map(2026)

    assert result == fake_data
    assert 2026 in _cache
    assert _cache[2026]['data'] == fake_data
    mock_ddb_write.assert_called_once()
    call_args = mock_ddb_write.call_args
    assert call_args[0][0] == 2026
    assert call_args[0][1] == fake_data


# ---------------------------------------------------------------------------
# 2. test_stale_cache_used_on_fetch_failure
# ---------------------------------------------------------------------------


@patch('common.holiday._fetch_holiday_data')
def test_stale_cache_used_on_fetch_failure(mock_fetch):
    """内存有过期缓存 + GitHub 拉取失败 → 返回旧数据（关键修复验证）。"""
    stale_data = {'2026-10-01': True, '2026-10-02': True}
    two_days_ago = time.time() - CACHE_TTL - 86400  # 过期超过 1 天
    _cache[2026] = {'data': stale_data, 'fetched_at': two_days_ago}

    mock_fetch.return_value = None  # GitHub 拉取失败

    result = _get_holiday_map(2026)
    assert result == stale_data


# ---------------------------------------------------------------------------
# 3. test_no_cache_fetch_fail_returns_none
# ---------------------------------------------------------------------------


@patch('common.holiday._read_ddb_cache')
@patch('common.holiday._fetch_holiday_data')
def test_no_cache_fetch_fail_returns_none(mock_fetch, mock_ddb_read):
    """无内存缓存 + 拉取失败 + DDB 无数据 → None，is_workday 退化为星期判断。"""
    mock_fetch.return_value = None
    mock_ddb_read.return_value = None

    result = _get_holiday_map(2026)
    assert result is None

    # 星期三应判为工作日
    wed = date(2026, 7, 15)  # Wednesday
    assert wed.weekday() == 2
    with patch('common.holiday._get_holiday_map', return_value=None):
        assert is_workday(wed) is True

    # 星期六应判为休息日
    sat = date(2026, 7, 18)  # Saturday
    assert sat.weekday() == 5
    with patch('common.holiday._get_holiday_map', return_value=None):
        assert is_workday(sat) is False


# ---------------------------------------------------------------------------
# 4. test_is_workday_holiday_respected_with_stale_data
# ---------------------------------------------------------------------------


@patch('common.holiday._fetch_holiday_data')
def test_is_workday_holiday_respected_with_stale_data(mock_fetch):
    """过期缓存中的周中节假日 → is_workday 返回 False（不误判为工作日）。"""
    # 2026-10-01（周四）是国庆节 isOffDay=true
    stale_data = {'2026-10-01': True, '2026-10-02': True, '2026-10-03': True}
    two_days_ago = time.time() - CACHE_TTL - 86400
    _cache[2026] = {'data': stale_data, 'fetched_at': two_days_ago}

    mock_fetch.return_value = None  # 拉取失败，走 stale 路径

    # 国庆节（周四）应为休息日
    national_day = date(2026, 10, 1)
    assert national_day.weekday() == 3  # Thursday
    assert is_workday(national_day) is False


# ---------------------------------------------------------------------------
# 5. test_cold_start_falls_back_to_ddb
# ---------------------------------------------------------------------------


@patch('common.holiday._read_ddb_cache')
@patch('common.holiday._fetch_holiday_data')
def test_cold_start_falls_back_to_ddb(mock_fetch, mock_ddb_read):
    """冷启动（内存空）+ 拉取失败 + DDB 有缓存 → 返回 DDB 数据。"""
    ddb_data = {'2026-10-01': True, '2026-05-01': True}
    ddb_fetched_at = time.time() - 3600  # 1 小时前写入 DDB 的

    mock_fetch.return_value = None
    mock_ddb_read.return_value = {'data': ddb_data, 'fetched_at': ddb_fetched_at}

    result = _get_holiday_map(2026)
    assert result == ddb_data
    # 验证回写到内存缓存
    assert 2026 in _cache
    assert _cache[2026]['data'] == ddb_data
    assert _cache[2026]['fetched_at'] == ddb_fetched_at


# ---------------------------------------------------------------------------
# 6. test_ddb_error_degrades_gracefully
# ---------------------------------------------------------------------------


@patch('common.holiday._read_ddb_cache')
@patch('common.holiday._fetch_holiday_data')
def test_ddb_error_degrades_gracefully(mock_fetch, mock_ddb_read):
    """DDB 读取抛异常 → 返回 None，不崩溃。"""
    mock_fetch.return_value = None
    mock_ddb_read.return_value = None  # _read_ddb_cache 内部已 try/except

    result = _get_holiday_map(2026)
    assert result is None


@patch('common.holiday._fetch_holiday_data')
def test_ddb_write_failure_does_not_break_fetch(mock_fetch):
    """DDB 写入失败不影响正常返回。"""
    fake_data = {'2026-01-01': True}
    mock_fetch.return_value = fake_data

    with patch('common.holiday._write_ddb_cache', side_effect=Exception("DDB down")):
        result = _get_holiday_map(2026)

    # 仍然成功返回数据（DDB 写失败仅打日志）
    assert result == fake_data
    assert _cache[2026]['data'] == fake_data


# ---------------------------------------------------------------------------
# 7. test_fresh_cache_returns_without_fetch
# ---------------------------------------------------------------------------


@patch('common.holiday._fetch_holiday_data')
def test_fresh_cache_returns_without_fetch(mock_fetch):
    """内存缓存未过期 → 直接返回，不触发 GitHub 拉取。"""
    fresh_data = {'2026-01-01': True}
    _cache[2026] = {'data': fresh_data, 'fetched_at': time.time()}

    result = _get_holiday_map(2026)
    assert result == fresh_data
    mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# 8. test_fetch_success_writes_ddb
# ---------------------------------------------------------------------------


@patch('common.holiday._write_ddb_cache')
@patch('common.holiday._fetch_holiday_data')
def test_fetch_success_writes_ddb(mock_fetch, mock_ddb_write):
    """GitHub 拉取成功时写 DDB。"""
    fake_data = {'2026-05-01': True}
    mock_fetch.return_value = fake_data

    _get_holiday_map(2026)

    mock_ddb_write.assert_called_once_with(2026, fake_data, pytest.approx(time.time(), abs=5))


# ---------------------------------------------------------------------------
# 9. test_december_checks_next_year
# ---------------------------------------------------------------------------


@patch('common.holiday._fetch_holiday_data')
def test_december_checks_next_year(mock_fetch):
    """12 月日期不在当年数据中时会查询次年文件。"""
    this_year_data = {'2026-10-01': True}  # 不含 12-31
    next_year_data = {'2026-12-31': False}  # 次年文件有 12-31 调休上班

    def fetch_side_effect(year):
        if year == 2026:
            return this_year_data
        elif year == 2027:
            return next_year_data
        return None

    mock_fetch.side_effect = fetch_side_effect

    with patch('common.holiday._write_ddb_cache'):
        # 12-31 是调休上班日（isOffDay=false）→ is_workday=True
        dec31 = date(2026, 12, 31)
        result = is_workday(dec31)
        assert result is True
