#!/usr/bin/env bash
# @version 1
# Mac 自助修复（MAC_REPAIR_V1）：完整校验整合包的所有受管文件，缺失或损坏的重新下载；
# 按清单规则保留的个人修改不会被覆盖。发布为 _updater/macOS-self-repair.command。
# 保持 LF 换行 + UTF-8 无 BOM。
cd "$(dirname "$0")"
SCRIPT_DIR="$PWD"
INSTANCE_DIR="$SCRIPT_DIR"
if [ "$(basename "$SCRIPT_DIR")" = "_updater" ]; then
  INSTANCE_DIR="$(cd .. && pwd)"
fi

printf '\033]0;整合包自助修复（Mac）\007'
echo "============================================================"
echo "  整合包自助修复：完整校验所有文件，损坏或缺失的重新下载"
echo "============================================================"
echo "实例目录: $INSTANCE_DIR"
echo "请先完全退出 Minecraft。"
echo

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys' >/dev/null 2>&1; then
  echo "[需要 Python 3] 请先双击「更新mod-Mac端.command」，按提示装好 Python 3 后再运行本文件。"
  read -r -p "按回车关闭。" _ || true
  exit 1
fi

UPDATER="$SCRIPT_DIR/player-update-generic.py"
if [ ! -f "$UPDATER" ]; then
  UPDATER="$INSTANCE_DIR/_updater/player-update-generic.py"
fi
PORTABLE_SYNC_INTERACTIVE=1 python3 "$UPDATER" --instance-dir "$INSTANCE_DIR" --verify-all
status=$?
echo
if [ "$status" = "0" ]; then
  echo "[修复] 完成：所有文件都和服务器一致（按规则保留的个人修改除外）。"
else
  echo "[修复] 未完成，退出码 $status。请把实例目录里的「更新诊断包.zip」发给管理员。"
fi
read -r -p "按回车关闭。" _ || true
exit "$status"
