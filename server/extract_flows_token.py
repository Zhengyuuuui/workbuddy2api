#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""从 mitmproxy flows 文件提取 x-device-token 的独立小工具。

mitmproxy 的 flows 是专有格式，纯 Python 无法直接解析；本工具封装系统 mitmdump：

    mitmdump -nr <flows> -s <内置 addon>

内置 addon 从每个请求头提取 x-device-token / x-user-id / path 写入临时 jsonl，
随后本工具按 path 前缀过滤（默认 /v2/chat/completions）、校验 `v3:` 前缀并去重。

用法：
    uv run server/extract_flows_token.py /tmp/wb-intl.mitm --output ~/.workbuddy-device-token.json
    uv run server/extract_flows_token.py /tmp/dt.mitm --output out.json --path-prefix /v2/chat/completions

安全：输出为 0600 JSON（含 _warning 字段）；终端只显示 token 长度与前 8 字符，绝不打印全量。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

DEFAULT_PATH_PREFIX = "/v2/chat/completions"
MIN_TOKEN_PREFIX = "v3:"
WARNING = "per-device secret, never share"


def _is_valid_token(token) -> bool:
    return isinstance(token, str) and token.startswith(MIN_TOKEN_PREFIX) and len(token) > 100


def _write_addon(addon_path: pathlib.Path, jsonl_path: pathlib.Path) -> None:
    """生成 mitmdump -s addon：提取携带 x-device-token 的请求到 jsonl。"""
    addon = f'''import json

OUT = {str(jsonl_path)!r}


def request(flow):
    token = flow.request.headers.get("x-device-token", "")
    if not token:
        return
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps({{
            "token": token,
            "uid": flow.request.headers.get("x-user-id", ""),
            "path": flow.request.path or "",
        }}) + "\\n")
'''
    addon_path.write_text(addon, encoding="utf-8")


def _run_mitmdump(flows_path: pathlib.Path, addon_path: pathlib.Path,
                  jsonl_path: pathlib.Path) -> subprocess.CompletedProcess:
    """真实实现：subprocess 调 mitmdump 回放（addon 会写入 jsonl_path）。

    jsonl_path 同时是测试注入点——单测 monkeypatch 本函数时可直接写该文件。
    """
    return subprocess.run(
        ["mitmdump", "-nr", str(flows_path), "-s", str(addon_path)],
        capture_output=True, text=True, timeout=300,
    )


def _load_entries(jsonl_path: pathlib.Path) -> list[dict]:
    if not jsonl_path.exists():
        return []
    entries: list[dict] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def _filter_entries(entries: list[dict], path_prefix: str) -> list[dict]:
    """path 前缀过滤 + v3: 校验 + 按 token 去重（保留首个）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        token = entry.get("token")
        if not _is_valid_token(token):
            continue
        if not str(entry.get("path", "")).startswith(path_prefix):
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(entry)
    return out


def run_extraction(flows_path: pathlib.Path, path_prefix: str,
                   workdir: pathlib.Path) -> list[dict]:
    addon_path = workdir / "extract_addon.py"
    jsonl_path = workdir / "tokens.jsonl"
    _write_addon(addon_path, jsonl_path)
    proc = _run_mitmdump(flows_path, addon_path, jsonl_path)
    if proc.returncode != 0 and not jsonl_path.exists():
        print(f"mitmdump 回放失败（rc={proc.returncode}）：{(proc.stderr or '').strip()[:300]}",
              file=sys.stderr)
    return _filter_entries(_load_entries(jsonl_path), path_prefix)


def _save_token(token: str, source: str, out: pathlib.Path) -> pathlib.Path:
    payload = {
        "token": token,
        "source": source,
        "found_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "_warning": WARNING,
    }
    out = out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(out, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    finally:
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从 mitmproxy flows 文件提取 x-device-token（封装 mitmdump -nr）")
    parser.add_argument("flows", type=pathlib.Path, help="mitmproxy flows 文件（.mitm）")
    parser.add_argument("--output", type=pathlib.Path, required=True,
                        help="token JSON 输出路径（0600，含 _warning）")
    parser.add_argument("--path-prefix", default=DEFAULT_PATH_PREFIX,
                        help=f"只认该路径前缀（默认 {DEFAULT_PATH_PREFIX}）")
    args = parser.parse_args(argv)

    flows_path = args.flows.expanduser()
    if not flows_path.is_file():
        print(f"flows 文件不存在：{flows_path}", file=sys.stderr)
        return 2

    workdir = pathlib.Path(tempfile.mkdtemp(prefix="wb-flows-token-"))
    try:
        entries = run_extraction(flows_path, args.path_prefix, workdir)
    except FileNotFoundError:
        print("mitmdump 不在 PATH（安装：brew install mitmproxy 或 pipx install mitmproxy）",
              file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("mitmdump 回放超时（flows 过大？）", file=sys.stderr)
        return 2

    if not entries:
        print("未提取到可用 token：", file=sys.stderr)
        print(f"  - 未找到 path 以 {args.path_prefix} 开头且 x-device-token 以 'v3:' 开头的请求",
              file=sys.stderr)
        print("  - 排查：是否真的触发了模型对话？抓包是否命中目标域名/IP？"
              "（见 doc/workbuddy-intl-token-guide.md）", file=sys.stderr)
        return 1

    entry = entries[0]
    token = entry["token"]
    uid = str(entry.get("uid") or "")
    out = _save_token(token, f"mitm:{flows_path}", args.output)

    print("✓ 提取成功：")
    print(f"  token 长度 {len(token)}，前 8 字符 {token[:8]}…（完整值不打印）")
    if uid:
        print(f"  uid 前 8 位 {uid[:8]}…")
    print(f"  path: {entry.get('path')}")
    if len(entries) > 1:
        print(f"  注意：flows 中共有 {len(entries)} 个不同 token，已取第一个")
    print(f"  已保存 → {out}（0600）")
    print("  下一步：uv run server/codebuddy_proxy.py --port 8788 ... "
          f"--device-token {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
