#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""CodeBuddy IDE x-device-token 提取脚本（只读，绝不修改任何应用数据）。

================================================================================
人工排查结论（2026-09-21，对 CodeBuddy CN 4.12.0 逆向）
================================================================================

一、x-device-token 赋值链（extensions/genie/out/extension/index.js，webpack 混淆产物）

请求头注入点（3 处，均为每请求实时调用，非启动时缓存）：

    // 聊天请求 customHeaders（约 offset 7000617）
    ...conversationId:Nn.conversationId,
       customHeaders: await this.buildDeviceTokenHeaders()}

    // chat/completions 请求头构造（约 offset 5127113 附近，同样逻辑）
    "XMLHttpRequest";
    const sn = await this.turingShieldService.getDeviceToken();
    return sn.token ? nn["X-Device-Token"] = sn.token
           : sn.error && (nn["X-Device-Token-Error"] = sn.error), ...

    // buildDeviceTokenHeaders（AgentContextBuilderImpl / GenerateTitle 两处定义相同）
    async buildDeviceTokenHeaders() {
        const ir = await this.turingShieldService.getDeviceToken();
        return ir.token ? {"X-Device-Token": ir.token}
             : ir.error ? {"X-Device-Token-Error": ir.error}
             : {};
    }

token 的唯一来源是 TuringShieldServiceImpl（offset ~471498，反混淆后）：

    ar.TuringShieldService = Symbol("TuringShieldService");
    let pn = class TuringShieldServiceImpl {
        constructor() { this.initialized = !1 }
        async getDeviceToken() {
            if (!await this.ensureInitialized() || !this.sdkModule)
                return { error: "sdk_not_initialized" };
            try {
                return { token: (await this.sdkModule.fetchRiskDetect(!0, !1)).token }
            } catch (ir) {
                const ar = this.extractErrorCode(ir);
                return this.logger.warn(`[TuringShieldService] fetchRiskDetect failed: ${ar}`),
                       { error: ar }
            }
        }
        async ensureInitialized() { return !!this.initialized || this.doInitialize() }
        async doInitialize() {
            const ir = await this.productManager.waitConfiguration(!0),
                  ar = this.isGlobal(null == ir ? void 0 : ir.endpoint),
                  rn = ar ? "@tencent/turing-shield-sdk-os" : "@tencent/turing-shield-sdk";
            if (!this.sdkModule) try {
                const ir = tn(ar ? 115122 : 530645);   // webpack 内联 native 包装模块
                this.sdkModule = ir
            } catch (ir) { ... this.initialized = !1; !1 }
            try {
                const tn = ar ? 400101 : 109137,       // channelId：国内 109137 / 海外 400101
                      rn = (null == ir ? void 0 : ir.productName) || "CodeBuddy",
                      nn = (null == ir ? void 0 : ir.productVersion) || "0.0.0",
                      sn = ar ? "https://www.turingfraud.net" : void 0;
                return await this.sdkModule.initialize({
                    channelId: tn, productName: rn, productVersion: nn, serverUrl: sn
                }), this.initialized = !0, ... !0
            } catch (ir) { ... !1 }
        }
        isGlobal(ir) { return !!(null == ir ? void 0 : ir.includes("codebuddy.ai")) }
        extractErrorCode(ir) { ... return ar.replace(/[^\x20-\x7E]/g, "").slice(0, 64) }
    }

二、SDK 链路（JS 包装层 → 原生二进制）

    // webpack 模块 530645 = @tencent/turing-shield-sdk（国内版包装）
    ar.fetchRiskDetect = async function fetchRiskDetect(ir = !0, ar = !1) {
        return hn.syncFetchRiskDetect(ir, ar)
    };
    const pn = dn(tn(16928)), hn = tn(68079)(pn.resolve(__dirname, ".."));   // 加载 native addon

    // webpack 模块 68079：require.addon 绑定，fallback node-gyp-build（模块 654955）
    68079: (ir, ar, tn) => {
        const rn = require;
        "function" == typeof rn.addon ? ir.exports = rn.addon.bind(rn) : ir.exports = tn(654955)
    }

    // initialize 校验参数（同模块）：channelId 必须 number、productName/productVersion 非空、
    // 可选 enableKeyChainStorage（boolean）——SDK 明确支持 Keychain 持久化

实际加载的 native addon（darwin-arm64）：
    /Applications/CodeBuddy CN.app/Contents/Resources/app/extensions/genie/
        out/prebuilds/darwin-arm64/electron.napi.node   （981KB，ObjC/Swift 风格混淆符号）

三、native 二进制 strings 分析（electron.napi.node）

    RiskDetectServer.DeviceTokenV3          ← v3 token 的服务端 API（运行时网络获取/换取）
    RiskDetectServer.CSRiskFeature / CSRiskFeatureEnc / CSRiskQueryPrivate ...
    TuringRiskTokenBASE / TuringMessageTicketBASE / TuringShieldBASE
    fetchCachedDeviceRiskMessage / fetchRiskMessageUsingCacheWithExtractAPIChecking:...
    0.com.tencent.TuringShield.settings.DFC.Data / .DFC.ExpiredDate
    0.com.tencent.TuringShield.settings.HistoryTicket. / .fpmarker. / .riskmessage.v2
    0.com.tencent.TuringShield.settings.packetinfo. / .statistics. / .KeyChainAccessAllowed

    → SDK 持久化的是"设备指纹缓存 + 票据元数据"，不是最终 v3: token 明文。

四、持久化位置实查（只读）

    * Keychain（security dump-keychain 可见，读取 secret 可能触发 GUI 授权弹窗，勿在脚本中读）：
        com.turingshield.identifying.cachedDF.Local.        （NSKeyedArchiver，含 analysisTicket/ticket/outdateDate）
        com.turingshield.identifying.guid.Local / .guid.general
        com.turingshield.identifying.com.tencent.codebuddycn.helper.109137
      实读 cachedDF.Local 值 = bplist00 + NSKeyedArchiver，字段为 ticket/outdateDate 等，无 v3: 明文。

    * NSUserDefaults（~/Library/Preferences/com.tencent.codebuddycn.helper.plist，channel 109137；
      com.tencent.workbuddy.mac.plist，channel 109144）：
        TuringShield..settings.HistoryTicket.    → 88 位 hex（非 v3: 格式）
        TuringShield..settings.RiskTokenSignatures → [{key,checksum,sequence},...] 签名元数据
        TuringShield..settings.DFC.Data / riskmessage.v2 / fpmarker. / packetinfo. → 加密/归档 blob
        __turingshield_user_identifier_tmf_shark:https://tdid.m.qq.com/tmf → tdid 用户标识（加密）
      全部 plist 值中 grep 无 "v3:" 前缀明文。

    * 此前已排除：state.vscdb（117 keys）、~/.codebuddy/、各 leveldb（Local/Session Storage、
      Partitions/genie-webview）二进制 grep 均无果。

五、结论

    x-device-token 由 @tencent/turing-shield-sdk 原生模块在运行时生成/换取：
    本地采集设备特征 + Keychain/plist 中的指纹缓存 → 调用 RiskDetectServer.DeviceTokenV3
    → 返回加密 token（v3: 前缀，465 字符）→ 仅存内存，每个请求通过 fetchRiskDetect 实时取得。
    **无法静态提取，需运行时 hook**：
      - 方案 A（推荐）：mitmproxy 从真实 IDE 会话抓包复制 token（有效期未知，可能随 DFC 过期刷新）
      - 方案 B：改写 extension/index.js 的 TuringShieldServiceImpl.getDeviceToken，在
        return 前 console.log/fs.writeFile 落盘 token（每次 IDE 升级需重打）
      - 方案 C：node 侧以 ELECTRON_RUN_AS_NODE 方式 require 原生 addon 模拟 initialize+fetchRiskDetect
        （依赖 IDE 进程内的设备指纹上下文，独立进程可能拿不到相同 token）
    本脚本的自动发现（环境变量/sqlite/文件扫描）在标准安装上大概率返回 None；
    codebuddy_proxy 启动时会打印 warning 并维持不发送该头的现状。
================================================================================
"""

from __future__ import annotations

import argparse
import json
import mmap
import os
import pathlib
import plistlib
import re
import sqlite3
import subprocess
import sys
import time
from typing import Any

# 官方 token 形态：v3: 前缀 + base64 字符集，实测总长 465 字符
TOKEN_RE = re.compile(rb"v3:[A-Za-z0-9+/=]{100,}")
MIN_TOKEN_PREFIX = "v3:"

_HOME = pathlib.Path.home()

# 目录扫描点：国内 CodeBuddy + 海外 WorkBuddy 数据目录
DEFAULT_SCAN_DIRS = [
    _HOME / "Library" / "Application Support" / "CodeBuddy CN",
    _HOME / ".codebuddy",
    _HOME / "workbuddy" / "app",
    _HOME / ".workbuddy",
    _HOME / "Library" / "Application Support" / "WorkBuddy",
    _HOME / "Library" / "Application Support" / "WorkBuddy AI",
]

# macOS Preferences plist（二进制格式，用 plistlib 解析后递归搜索 v3: 值）。
# 海外 WorkBuddy（com.tencent.workbuddy.mac，channelId 400101）优先；
# 国内 CodeBuddy（com.tencent.codebuddycn*，channelId 109137）作对照。
DEFAULT_PLIST_PATHS = [
    _HOME / "Library" / "Preferences" / "com.tencent.workbuddy.mac.plist",
    _HOME / "Library" / "Preferences" / "com.workbuddy.workbuddy.plist",
    _HOME / "Library" / "Preferences" / "com.workbuddy.workbuddy-ai.plist",
    _HOME / "Library" / "Preferences" / "com.tencent.codebuddycn.helper.plist",
    _HOME / "Library" / "Preferences" / "com.tencent.codebuddycn.plist",
]

# 说明：macOS Keychain 中的 turingshield 条目（com.turingshield.identifying.*）为
# NSKeyedArchiver 归档的票据元数据，且读取 secret 会触发 GUI 授权弹窗；
# 命令行默认无权限、且无 v3: 明文，故不做 Keychain 探测。

SKIP_DIR_NAMES = {"Cache", "CachedData", "CrashReport", "GPUCache"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB


def _is_valid_token(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(MIN_TOKEN_PREFIX) and len(value) > 100


def _make_result(token: str, source: str) -> dict:
    return {
        "token": token,
        "source": source,
        "found_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def coerce_token_value(raw: str | None) -> str | None:
    """解析 token 值：支持 (1) JSON {"token": ...}（extract --output 的文件/环境变量）
    (2) 指向文件的路径（文件内容为 JSON 或裸 token）(3) 裸 token 字符串。"""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    # 直接是 JSON
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("token"):
            return str(data["token"]).strip() or None
        if isinstance(data, str):
            return data.strip() or None
    except json.JSONDecodeError:
        pass
    # 是文件路径
    path = pathlib.Path(raw).expanduser()
    try:
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            return coerce_token_value(text)
    except OSError:
        pass
    return raw


def _scan_bytes_for_token(data: bytes) -> str | None:
    match = TOKEN_RE.search(data)
    if match:
        token = match.group(0).decode("ascii", errors="replace")
        if _is_valid_token(token):
            return token
    return None


def _scan_file_for_token(path: pathlib.Path) -> str | None:
    """mmap 二进制搜索，只读打开；空文件/超大/无权限文件跳过。"""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0 or size > MAX_FILE_SIZE:
        return None
    try:
        with open(path, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                return _scan_bytes_for_token(mm)
            finally:
                mm.close()
    except (OSError, ValueError):
        return None


def _scan_directory(root: pathlib.Path, scanned: list[str]) -> tuple[str | None, str | None]:
    """递归扫描目录，返回 (token, source) 或 (None, None)。"""
    if not root.exists():
        scanned.append(f"{root} (missing)")
        return None, None
    scanned.append(str(root))
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for name in filenames:
            fp = pathlib.Path(dirpath) / name
            try:
                if fp.stat().st_size > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            token = _scan_file_for_token(fp)
            if token:
                return token, str(fp)
    return None, None


def _search_plist_value(value: Any) -> str | None:
    """递归搜索 plist 值中的 v3: token（str 直接匹配，bytes/归档 blob 走二进制正则）。"""
    if isinstance(value, str):
        if _is_valid_token(value):
            return value
        return _scan_bytes_for_token(value.encode("utf-8", "replace"))
    if isinstance(value, (bytes, bytearray)):
        return _scan_bytes_for_token(bytes(value))
    if isinstance(value, dict):
        for item in value.values():
            hit = _search_plist_value(item)
            if hit:
                return hit
    elif isinstance(value, (list, tuple)):
        for item in value:
            hit = _search_plist_value(item)
            if hit:
                return hit
    return None


def _scan_plist_file(path: pathlib.Path, scanned: list[str]) -> tuple[str | None, str | None]:
    """只读解析 macOS Preferences plist（多为二进制格式），搜索 v3: 值。"""
    scanned.append(f"plist:{path}")
    if not path.is_file():
        scanned.append(f"plist:{path} (missing)")
        return None, None
    try:
        with open(path, "rb") as f:
            obj = plistlib.load(f)
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        scanned.append(f"plist:{path} (parse skipped: {exc})")
        return None, None
    token = _search_plist_value(obj)
    if token:
        return token, f"plist:{path}"
    return None, None


def _try_decrypt_safe_storage_hint() -> str:
    """secret:// 值多为 VSCode safeStorage 加密（v10/v11 前缀）。
    纯标准库无法 AES 解密；尝试用 macOS security 读密钥仅作可行性探测（超时/弹窗即放弃）。
    """
    if sys.platform != "darwin":
        return "non-darwin platform, keychain decryption unavailable"
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-w",
             "-s", "CodeBuddy CN Safe Storage", "-a", "CodeBuddy CN"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return ("safeStorage key readable via `security` but AES-128-CBC decrypt requires "
                    "non-stdlib crypto; inject token manually via CODEBUDDY_DEVICE_TOKEN")
        return f"security CLI failed (rc={proc.returncode})"
    except subprocess.TimeoutExpired:
        return "security CLI timed out (keychain access prompt?); skipped"
    except OSError as exc:
        return f"security CLI unavailable: {exc}"


def _scan_sqlite(db_path: pathlib.Path, scanned: list[str]) -> tuple[str | None, str | None]:
    """只读扫描 sqlite ItemTable 的 key/value；secret:// 键的加密值跳过并记录原因。"""
    scanned.append(f"sqlite:{db_path}")
    if not db_path.is_file():
        scanned.append(f"sqlite:{db_path} (missing)")
        return None, None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        scanned.append(f"sqlite:{db_path} (open error: {exc})")
        return None, None
    try:
        try:
            rows = conn.execute("SELECT key, value FROM ItemTable").fetchall()
        except sqlite3.Error as exc:
            scanned.append(f"sqlite:{db_path} (query error: {exc})")
            return None, None
    finally:
        conn.close()

    secret_hint_logged = False
    for key, value in rows:
        key_text = key.decode("utf-8", "replace") if isinstance(key, bytes) else str(key)
        if value is None:
            continue
        if isinstance(value, bytes):
            token = _scan_bytes_for_token(value)
            if token:
                return token, f"sqlite:{db_path}#{key_text}"
            if key_text.startswith("secret://") or value[:3] in (b"v10", b"v11"):
                if not secret_hint_logged:
                    hint = _try_decrypt_safe_storage_hint()
                    scanned.append(f"sqlite:{db_path} secret:// values are safeStorage-encrypted; {hint}")
                    secret_hint_logged = True
            continue
        text = str(value)
        if _is_valid_token(text):
            return text, f"sqlite:{db_path}#{key_text}"
        token = _scan_bytes_for_token(text.encode("utf-8", "replace"))
        if token:
            return token, f"sqlite:{db_path}#{key_text}"
        if key_text.startswith("secret://") and not secret_hint_logged:
            hint = _try_decrypt_safe_storage_hint()
            scanned.append(f"sqlite:{db_path} secret:// values are safeStorage-encrypted; {hint}")
            secret_hint_logged = True
    return None, None


def find_device_token(
    db: pathlib.Path | None = None,
    scan_dirs: list[pathlib.Path] | None = None,
    scanned: list[str] | None = None,
    env: dict | None = None,
    plists: list[pathlib.Path] | None = None,
) -> dict | None:
    """探测 device token，找到第一个即返回 {"token","source","found_at"}，找不到返回 None。

    优先级：环境变量 CODEBUDDY_DEVICE_TOKEN > --db sqlite > Preferences plist > 目录递归扫描。
    绝不抛异常；探测过程追加进 scanned（供 CLI 输出人工排查）。
    """
    scanned = scanned if scanned is not None else []
    env_map = env if env is not None else os.environ

    # a. 环境变量（优先级最高）
    env_val = env_map.get("CODEBUDDY_DEVICE_TOKEN")
    if env_val:
        token = coerce_token_value(env_val)
        if _is_valid_token(token):
            return _make_result(token, "env:CODEBUDDY_DEVICE_TOKEN")
        scanned.append("env:CODEBUDDY_DEVICE_TOKEN (present but invalid format)")

    # b. sqlite 文件
    if db is not None:
        hit = _scan_sqlite(pathlib.Path(db), scanned)
        if hit[0]:
            return _make_result(hit[0], hit[1])

    # c. macOS Preferences plist（海外 WorkBuddy 优先）
    plist_paths = plists if plists is not None else DEFAULT_PLIST_PATHS
    for p in plist_paths:
        hit = _scan_plist_file(pathlib.Path(p).expanduser(), scanned)
        if hit[0]:
            return _make_result(hit[0], hit[1])

    # d. 目录递归扫描（CodeBuddy CN + ~/.codebuddy + WorkBuddy 数据目录）
    dirs = scan_dirs if scan_dirs is not None else DEFAULT_SCAN_DIRS
    for d in dirs:
        hit = _scan_directory(pathlib.Path(d).expanduser(), scanned)
        if hit[0]:
            return _make_result(hit[0], hit[1])

    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract CodeBuddy IDE x-device-token (read-only)")
    parser.add_argument("--db", type=pathlib.Path, default=None,
                        help="CodeBuddy state.vscdb 等 sqlite 文件路径（只读扫描 ItemTable）")
    parser.add_argument("--output", type=pathlib.Path, default=None,
                        help="将结果 JSON 写入该文件（0600 权限），默认输出 stdout")
    args = parser.parse_args(argv)

    scanned: list[str] = []
    found = find_device_token(db=args.db, scanned=scanned)

    if found:
        text = json.dumps(found, ensure_ascii=False, indent=2)
        if args.output:
            out = args.output.expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(out, flags, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text + "\n")
            finally:
                try:
                    os.chmod(out, 0o600)
                except OSError:
                    pass
            print(f"device token written to {out} (source={found['source']})")
        else:
            print(text)
        return 0

    print("device token not found. scanned sources:", file=sys.stderr)
    for item in scanned or ["(nothing scanned)"]:
        print(f"  - {item}", file=sys.stderr)
    print(
        "\n结论：x-device-token 由 @tencent/turing-shield-sdk 运行时生成（RiskDetectServer.DeviceTokenV3），"
        "标准安装下无静态落盘明文。\n"
        "建议：mitmproxy 抓包复制 token 后 export CODEBUDDY_DEVICE_TOKEN=... ，"
        "或使用 --device-token 指向 extract --output 生成的 JSON 文件。\n"
        "详见 server/extract_device_token.py 顶部逆向注释。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
