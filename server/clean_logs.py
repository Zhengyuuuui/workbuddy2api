#!/usr/bin/env python3
"""清理 codebuddy2api 的日志目录。

logs/ 下有三类文件，增长行为不同：

    proxy.log            滚动日志（按天分片，保留 30 天）
    proxy.log.YYYY-MM-DD 历史分片，单日可达数百 MB
    *.jsonl              --log-file 请求日志，**无任何轮转**，无限增长
    *-start.log          stdout 重定向，通常很小

策略：
    1. 删除修改时间早于 --retain-days 的 proxy.log.* 分片
    2. 删除修改时间早于 --retain-days 且未被运行中进程占用的 *.jsonl
    3. 对仍在写入但超过 --max-size-mb 的活动 *.jsonl，gzip 归档后截断

安全约束：
    - 默认 dry-run，只打印计划，必须加 --apply 才真正执行
    - 任何被运行中的 codebuddy_proxy.py 打开的文件一律不删

用法：
    uv run server/clean_logs.py                  # 预览（默认）
    uv run server/clean_logs.py --apply          # 执行
    uv run server/clean_logs.py --retain-days 14 # 保留两周
    uv run server/clean_logs.py --no-rotate      # 只删旧文件，不动大文件

定时（每天凌晨 4 点）：
    0 4 * * * cd /path/to/workbuddy2api && uv run server/clean_logs.py --apply >> logs/clean.log 2>&1
"""

from __future__ import annotations

import argparse
import gzip
import os
import pathlib
import shutil
import subprocess
import sys
import time

DEFAULT_LOG_DIR = pathlib.Path("logs")
DEFAULT_RETAIN_DAYS = 7
DEFAULT_MAX_SIZE_MB = 100

# 这些后缀的文件是滚动日志的历史分片，可按保留期删除
ROTATED_SUFFIX_RE = r"^proxy\.log\.\d{4}-\d{2}-\d{2}$"
# stdout 重定向文件，体积小，不处理
IGNORED_NAMES = {"clean.log"}


def open_log_files(log_dir: pathlib.Path) -> set[pathlib.Path]:
    """返回正被运行中的 codebuddy_proxy.py 占用的日志文件绝对路径集合。

    注意：proxy 对 jsonl 是「每次写入时 open/append/close」，lsof 大概率抓不到，
    所以这里只能作为辅助判据；主要判据见 is_recently_written()。
    """
    active: set[pathlib.Path] = set()
    try:
        pids = subprocess.run(
            ["pgrep", "-f", "codebuddy_proxy.py"],
            capture_output=True, text=True, check=False,
        ).stdout.split()
    except FileNotFoundError:
        pids = []

    for pid in pids:
        try:
            out = subprocess.run(
                ["lsof", "-p", pid, "-Fn"],
                capture_output=True, text=True, check=False,
            ).stdout
        except FileNotFoundError:
            return active  # lsof 不可用时放弃探测，交给保留期兜底
        for line in out.splitlines():
            if line.startswith("n") and str(log_dir) in line:
                active.add(pathlib.Path(line[1:]).resolve())
    return active


def is_recently_written(path: pathlib.Path, now: float, window_minutes: float) -> bool:
    """文件是否在最近 window_minutes 内被写过（proxy 活动日志的判据）。"""
    return (now - path.stat().st_mtime) < window_minutes * 60


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def age_days(path: pathlib.Path, now: float) -> float:
    return (now - path.stat().st_mtime) / 86400


def plan(
    log_dir: pathlib.Path,
    retain_days: float,
    max_size_mb: float,
    rotate: bool,
    active_window_minutes: float,
) -> tuple[list[tuple[pathlib.Path, str]], list[tuple[pathlib.Path, str]]]:
    """返回 (待删除, 待归档截断) 两个计划列表。"""
    if not log_dir.is_dir():
        return [], []

    now = time.time()
    held_open = open_log_files(log_dir)
    to_delete: list[tuple[pathlib.Path, str]] = []
    to_rotate: list[tuple[pathlib.Path, str]] = []

    import re
    rotated_re = re.compile(ROTATED_SUFFIX_RE)

    for path in sorted(log_dir.iterdir()):
        if not path.is_file() or path.name in IGNORED_NAMES:
            continue
        resolved = path.resolve()
        size = path.stat().st_size
        age = age_days(path, now)
        # 活动文件：被进程占用，或最近还在写（proxy 每次写都开关文件，必须靠 mtime）
        active = resolved in held_open or is_recently_written(
            path, now, active_window_minutes
        )

        # 正在被写入的文件：只考虑体积过大时归档，绝不删除
        if active:
            if rotate and size > max_size_mb * 1024 * 1024:
                to_rotate.append((path, f"活动文件 {human(size)} > {max_size_mb:.0f}MB，归档后截断"))
            continue

        if path.suffix == ".jsonl" and age > retain_days:
            to_delete.append((path, f"过期 {age:.1f} 天，{human(size)}"))
        elif rotated_re.match(path.name) and age > retain_days:
            to_delete.append((path, f"过期 {age:.1f} 天，{human(size)}"))

    return to_delete, to_rotate


def apply_plan(
    log_dir: pathlib.Path,
    to_delete: list[tuple[pathlib.Path, str]],
    to_rotate: list[tuple[pathlib.Path, str]],
) -> tuple[int, int]:
    deleted_bytes = 0
    for path, _ in to_delete:
        try:
            deleted_bytes += path.stat().st_size
            path.unlink()
            print(f"  deleted  {path}")
        except OSError as exc:
            print(f"  FAILED   {path}: {exc}", file=sys.stderr)

    rotated_bytes = 0
    for path, _ in to_rotate:
        archive = path.with_suffix(path.suffix + f".{time.strftime('%Y%m%d-%H%M%S')}.gz")
        try:
            with open(path, "rb") as src, gzip.open(archive, "wb") as dst:
                shutil.copyfileobj(src, dst)
            rotated_bytes += path.stat().st_size
            # 截断（保留 inode，proxy 下次 append 仍写同一文件）
            with open(path, "w", encoding="utf-8"):
                pass
            print(f"  rotated  {path} -> {archive.name} ({human(rotated_bytes)})")
        except OSError as exc:
            print(f"  FAILED   {path}: {exc}", file=sys.stderr)
            if archive.exists():
                archive.unlink(missing_ok=True)

    return deleted_bytes, rotated_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 codebuddy2api 日志目录")
    parser.add_argument("--log-dir", type=pathlib.Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--retain-days", type=float, default=DEFAULT_RETAIN_DAYS,
                        help=f"保留最近 N 天的日志（默认 {DEFAULT_RETAIN_DAYS}）")
    parser.add_argument("--max-size-mb", type=float, default=DEFAULT_MAX_SIZE_MB,
                        help=f"活动 jsonl 超过该大小则归档截断（默认 {DEFAULT_MAX_SIZE_MB:.0f}，0 关闭）")
    parser.add_argument("--no-rotate", action="store_true",
                        help="不归档超大的活动文件，只删过期文件")
    parser.add_argument("--active-window-minutes", type=float, default=60,
                        help="最近 N 分钟内写过视为活动文件（默认 60）")
    parser.add_argument("--apply", action="store_true",
                        help="真正执行；默认只预览")
    args = parser.parse_args()

    rotate = not args.no_rotate and args.max_size_mb > 0
    to_delete, to_rotate = plan(
        args.log_dir, args.retain_days, args.max_size_mb, rotate,
        args.active_window_minutes,
    )

    if not to_delete and not to_rotate:
        print(f"{args.log_dir}/ 无需清理（保留 {args.retain_days:.0f} 天"
              f"{f'，活动文件上限 {args.max_size_mb:.0f}MB' if rotate else ''}）")
        return 0

    mode = "执行" if args.apply else "预览（加 --apply 执行）"
    print(f"{args.log_dir}/ 清理计划 [{mode}]")

    if to_delete:
        print(f"\n删除过期文件（>{args.retain_days:.0f} 天）:")
        reclaim = sum(p.stat().st_size for p, _ in to_delete)
        for path, reason in to_delete:
            print(f"  {path.name:34} {reason}")
        print(f"  小计: {len(to_delete)} 个文件，可释放 {human(reclaim)}")

    if to_rotate:
        print(f"\n归档活动大文件（>{args.max_size_mb:.0f}MB）:")
        for path, reason in to_rotate:
            print(f"  {path.name:34} {reason}")

    if not args.apply:
        print("\n未做任何修改。确认后加 --apply 执行。")
        return 0

    print()
    deleted_bytes, rotated_bytes = apply_plan(args.log_dir, to_delete, to_rotate)
    print(f"\n完成：释放 {human(deleted_bytes)}，归档压缩 {human(rotated_bytes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
