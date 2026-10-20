#!/usr/bin/env bash
# 安装 / 卸载 codebuddy2api 日志定时清理（macOS launchd）
#
# 用法：
#   server/install_clean_cron.sh install     # 安装并立即触发一次验证
#   server/install_clean_cron.sh uninstall   # 卸载
#   server/install_clean_cron.sh status      # 查看状态
#
# 每天 04:00 执行 clean_logs.py --apply（保留 7 天，活动文件上限 100MB）。
# 用 launchd 而非 crontab：macOS 上 crontab 需要完整磁盘访问权限，且机器休眠会漏跑；
# StartCalendarInterval 漏跑后开机能补一次。

set -euo pipefail

LABEL="com.$(whoami).workbuddy2api.cleanlogs"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PROJECT_DIR}/.venv/bin/python3"
SCRIPT="${PROJECT_DIR}/server/clean_logs.py"
CLEAN_LOG="${PROJECT_DIR}/logs/clean.log"

RETAIN_DAYS=7
MAX_SIZE_MB=100
HOUR=4
MINUTE=0

cmd_install() {
  if [[ ! -x "$PYTHON" ]]; then
    echo "错误: 找不到 $PYTHON，请先在项目根目录执行 uv sync" >&2
    exit 1
  fi
  if [[ ! -f "$SCRIPT" ]]; then
    echo "错误: 找不到 $SCRIPT" >&2
    exit 1
  fi

  mkdir -p "$HOME/Library/LaunchAgents" "${PROJECT_DIR}/logs"

  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>${LABEL}</string>

	<key>ProgramArguments</key>
	<array>
		<string>${PYTHON}</string>
		<string>${SCRIPT}</string>
		<string>--apply</string>
		<string>--retain-days</string>
		<string>${RETAIN_DAYS}</string>
		<string>--max-size-mb</string>
		<string>${MAX_SIZE_MB}</string>
	</array>

	<key>WorkingDirectory</key>
	<string>${PROJECT_DIR}</string>

	<key>StartCalendarInterval</key>
	<dict>
		<key>Hour</key>
		<integer>${HOUR}</integer>
		<key>Minute</key>
		<integer>${MINUTE}</integer>
	</dict>

	<key>StandardOutPath</key>
	<string>${CLEAN_LOG}</string>
	<key>StandardErrorPath</key>
	<string>${CLEAN_LOG}</string>

	<key>ProcessType</key>
	<string>Background</string>
</dict>
</plist>
EOF

  plutil -lint "$PLIST" >/dev/null
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"

  echo "已安装 ${LABEL}"
  echo "  plist   : ${PLIST}"
  echo "  计划    : 每天 $(printf '%02d:%02d' "$HOUR" "$MINUTE")"
  echo "  参数    : --apply --retain-days ${RETAIN_DAYS} --max-size-mb ${MAX_SIZE_MB}"
  echo "  日志    : ${CLEAN_LOG}"
  echo ""
  echo "立即触发一次验证..."
  launchctl start "$LABEL"
  sleep 5
  echo "--- ${CLEAN_LOG} ---"
  tail -20 "$CLEAN_LOG" 2>/dev/null || echo "(无输出)"
  echo "--- launchctl list ---"
  launchctl list | grep "$LABEL" || echo "(未找到，安装可能失败)"
}

cmd_uninstall() {
  if [[ -f "$PLIST" ]]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "已卸载并删除 ${PLIST}"
  else
    echo "未安装（${PLIST} 不存在）"
  fi
}

cmd_status() {
  echo "--- launchctl list ---"
  launchctl list | grep "$LABEL" || echo "(未注册)"
  echo ""
  echo "--- plist ---"
  if [[ -f "$PLIST" ]]; then
    plutil -lint "$PLIST"
  else
    echo "(不存在)"
  fi
  echo ""
  echo "--- 最近清理日志 ---"
  tail -10 "$CLEAN_LOG" 2>/dev/null || echo "(无)"
}

case "${1:-}" in
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  status)    cmd_status ;;
  *) echo "用法: $0 {install|uninstall|status}" >&2; exit 1 ;;
esac
