#!/bin/zsh
# 原地刷新 16-飞书HTML 根层的置顶目录 00-HTML资料库管理器.html（先写临时文件再原子替换，旧文件不会残留）。
# 置顶目录只收录 16-飞书HTML 本身（体积小、手机可开）；全库检索请用 启动本机管理器.command（本机服务）。
# 默认只刷新、不打开浏览器：每次归档 HTML 后飞书 bot 和本机 Agent 都会运行本脚本，自动打开会反复弹出管理器。要看结果时加 --open。
set -eu
case "$#:${1:-}" in
  0:) open_after=0 ;;
  1:--open) open_after=1 ;;
  *) print -u2 '用法：更新静态目录.command [--open]（--open：刷新后在浏览器打开置顶目录）'; exit 2 ;;
esac
cd "${0:A:h}"
library="$HOME/Desktop/03-内容创作/16-飞书HTML"
catalog_target="$library/00-HTML资料库管理器.html"
python3 manager.py export --root "$library" --state-dir "$HOME/.local/share/html-manager/pinned-16" --out "$catalog_target" --force
print "已原地刷新置顶目录：$catalog_target"
if (( open_after )); then
  open "$catalog_target"
fi
