"""clean_logs 的清理计划逻辑测试。"""

import pathlib
import time

import pytest

from clean_logs import plan


def _touch(path: pathlib.Path, *, days_ago: float = 0.0, size: int = 0) -> None:
    path.write_bytes(b"x" * size)
    old = time.time() - days_ago * 86400
    import os
    os.utime(path, (old, old))


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return d


def test_deletes_rotated_logs_older_than_retention(log_dir):
    _touch(log_dir / "proxy.log.2026-10-01", days_ago=30, size=1024)
    to_delete, to_rotate = plan(log_dir, retain_days=7, max_size_mb=100,
                                 rotate=True, active_window_minutes=60)
    assert [p.name for p, _ in to_delete] == ["proxy.log.2026-10-01"]
    assert to_rotate == []


def test_keeps_rotated_logs_within_retention(log_dir):
    _touch(log_dir / "proxy.log.2026-10-18", days_ago=2, size=1024)
    to_delete, _ = plan(log_dir, retain_days=7, max_size_mb=100,
                        rotate=True, active_window_minutes=60)
    assert to_delete == []


def test_rotated_name_with_fresh_mtime_is_kept(log_dir):
    """文件名日期不可信（handler suffix 会算错），一律以 mtime 为准。"""
    _touch(log_dir / "proxy.log.2026-10-05", days_ago=0.05, size=1024)
    to_delete, _ = plan(log_dir, retain_days=7, max_size_mb=100,
                        rotate=True, active_window_minutes=60)
    assert to_delete == []


def test_deletes_stale_jsonl_older_than_retention(log_dir):
    _touch(log_dir / "proxy-cn.jsonl", days_ago=30, size=2048)
    to_delete, _ = plan(log_dir, retain_days=7, max_size_mb=100,
                        rotate=True, active_window_minutes=60)
    assert [p.name for p, _ in to_delete] == ["proxy-cn.jsonl"]


def test_keeps_active_jsonl_regardless_of_size(log_dir):
    """活动文件绝不删除，超过体积上限才归档。"""
    _touch(log_dir / "proxy-intl.jsonl", days_ago=0, size=200 * 1024 * 1024)
    to_delete, to_rotate = plan(log_dir, retain_days=7, max_size_mb=100,
                                rotate=True, active_window_minutes=60)
    assert to_delete == []
    assert [p.name for p, _ in to_rotate] == ["proxy-intl.jsonl"]


def test_active_jsonl_under_threshold_is_untouched(log_dir):
    _touch(log_dir / "codebuddy-proxy.jsonl", days_ago=0, size=1024)
    to_delete, to_rotate = plan(log_dir, retain_days=7, max_size_mb=100,
                                rotate=True, active_window_minutes=60)
    assert to_delete == []
    assert to_rotate == []


def test_no_rotate_flag_skips_oversized_active_file(log_dir):
    _touch(log_dir / "proxy-intl.jsonl", days_ago=0, size=200 * 1024 * 1024)
    _, to_rotate = plan(log_dir, retain_days=7, max_size_mb=100,
                        rotate=False, active_window_minutes=60)
    assert to_rotate == []


def test_ignores_own_log_file(log_dir):
    _touch(log_dir / "clean.log", days_ago=30, size=1024)
    to_delete, _ = plan(log_dir, retain_days=7, max_size_mb=100,
                        rotate=True, active_window_minutes=60)
    assert to_delete == []


def test_empty_dir(log_dir):
    to_delete, to_rotate = plan(log_dir, retain_days=7, max_size_mb=100,
                                rotate=True, active_window_minutes=60)
    assert to_delete == []
    assert to_rotate == []


def test_missing_dir(tmp_path):
    to_delete, to_rotate = plan(tmp_path / "nope", retain_days=7, max_size_mb=100,
                                rotate=True, active_window_minutes=60)
    assert to_delete == []
    assert to_rotate == []
