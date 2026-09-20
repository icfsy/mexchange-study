#!/usr/bin/env bash
#
# verify_repo.sh —— 推送前自检
#
# 这个脚本存在的理由：本仓库在准备推送到公开 GitHub 时，踩过两类"看起来对、其实错"的坑：
#
#   1. 提交进去的是**过期版本**（内容与工作区不一致），而且包名还拼错了；
#   2. 我自己的验证脚本匹配了同一个错误拼写，于是验证"全部通过"，把问题掩盖了。
#
# 所以这里的每条检查都刻意**独立于被测对象**：不依赖代码里的任何假设，
# 只做字节比较、语法编译、名称对照和模式扫描。
#
# 用法：
#   yyin/scripts/verify_repo.sh              # 全部检查（含网络）
#   yyin/scripts/verify_repo.sh --offline    # 跳过 remote 连通性检查
#
# 退出码：0 = 全部通过；1 = 有失败项。
#
set -uo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "错误：当前目录不在 git 仓库内" >&2
  exit 2
}
cd "$REPO_ROOT" || exit 2

OFFLINE=0
for arg in "$@"; do
  case "$arg" in
    --offline) OFFLINE=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

PASS=0
FAIL=0
WARN=0

c_ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
c_bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }
c_warn() { printf '  \033[33m!\033[0m %s\n' "$1"; WARN=$((WARN + 1)); }
hdr()    { printf '\n\033[1m%s\033[0m\n' "$1"; }

printf '\033[1m仓库自检\033[0m  %s\n' "$REPO_ROOT"

# ---------------------------------------------------------------------------
hdr "1. 已提交（或已暂存）内容 == 工作区内容"
# 防止把过期版本提交进去。这是本项目真实踩过的坑，所以放在第一位。
# 新增但尚未提交的文件不在 HEAD 里，此时退化为"暂存内容 == 工作区内容"。
mismatch=0
total=0
newly_added=0
while IFS= read -r f; do
  total=$((total + 1))
  if [ ! -f "$f" ]; then
    c_bad "工作区缺少文件：$f"
    mismatch=$((mismatch + 1))
    continue
  fi
  if ! git cat-file -e "HEAD:$f" 2>/dev/null; then
    newly_added=$((newly_added + 1))
    if ! cmp -s <(git show ":$f" 2>/dev/null) "$f"; then
      c_bad "暂存内容与工作区不一致（新增文件）：$f"
      mismatch=$((mismatch + 1))
    fi
    continue
  fi
  if ! cmp -s <(git show "HEAD:$f" 2>/dev/null) "$f"; then
    c_bad "HEAD 与工作区不一致：$f"
    mismatch=$((mismatch + 1))
  fi
done < <(git ls-files)
if [ "$mismatch" -eq 0 ]; then
  if [ "$newly_added" -gt 0 ]; then
    c_ok "全部 $total 个文件一致（其中 $newly_added 个为新增，比对的是暂存内容）"
  else
    c_ok "全部 $total 个已跟踪文件字节一致"
  fi
fi

# ---------------------------------------------------------------------------
hdr "2. Python 语法"
py_files=$(find . -name '*.py' -not -path './.git/*' 2>/dev/null)
if [ -z "$py_files" ]; then
  c_warn "没有找到 .py 文件，跳过"
elif ! command -v python3 >/dev/null 2>&1; then
  c_warn "未安装 python3，跳过"
else
  syntax_err=0
  while IFS= read -r f; do
    # 用 compile() 而不是 py_compile，避免生成 __pycache__ 弄脏工作区
    if ! python3 -c 'import sys; p = sys.argv[1]; compile(open(p, encoding="utf-8").read(), p, "exec")' "$f" 2>/tmp/verify_repo_pyerr; then
      c_bad "语法错误：$f"
      sed 's/^/      /' /tmp/verify_repo_pyerr
      syntax_err=$((syntax_err + 1))
    fi
  done <<< "$py_files"
  rm -f /tmp/verify_repo_pyerr
  if [ "$syntax_err" -eq 0 ]; then
    c_ok "$(printf '%s\n' "$py_files" | wc -l | tr -d ' ') 个 Python 文件语法通过"
  fi
fi

# ---------------------------------------------------------------------------
hdr "3. Python 包名一致性"
# 独立推导"应该叫什么"（取自目录名），再要求所有绝对 import 完全一致。
# 这样任何多字母/少字母的拼写都会被抓出来，而不是靠硬编码某一种拼错。
pkg_dir=$(find . -type d -name 'mexchange' -not -path './.git/*' 2>/dev/null | head -1)
if [ -z "$pkg_dir" ]; then
  c_warn "没找到名为 mexchange 的包目录，跳过"
else
  pkg_name=$(basename "$pkg_dir")
  c_ok "包目录：${pkg_dir#./}（模块名 $pkg_name）"

  imports=$(grep -rhoE '^[[:space:]]*from[[:space:]]+[A-Za-z_][A-Za-z0-9_.]*[[:space:]]+import' \
              --include='*.py' . 2>/dev/null \
            | sed -E 's/^[[:space:]]*from[[:space:]]+([A-Za-z_][A-Za-z0-9_.]*)[[:space:]]+import.*/\1/' \
            | cut -d. -f1 | sort -u)
  # 只看与包有关的（名字里含 exchange），其余是标准库
  bad_names=$(printf '%s\n' "$imports" | grep -i 'exchange' | grep -v "^${pkg_name}$" || true)
  if [ -n "$bad_names" ]; then
    while IFS= read -r n; do
      [ -n "$n" ] && c_bad "import 的模块名不是 '$pkg_name'：$n"
    done <<< "$bad_names"
  else
    c_ok "所有包内绝对 import 均为 '$pkg_name'"
  fi

  # 近似拼写兜底（历史上出现过 meexchange）
  for near in meexchange; do
    if grep -rq --include='*.py' "$near" . 2>/dev/null; then
      c_bad "发现近似拼写 '$near'（应为 '$pkg_name'）"
    fi
  done
fi

# ---------------------------------------------------------------------------
hdr "4. 不该进版本库的东西"
if git ls-files | grep -qE '(__pycache__|\.pyc$|\.pyo$)'; then
  c_bad "Python 缓存文件被跟踪"
else
  c_ok "无 __pycache__ / *.pyc 被跟踪"
fi
if git ls-files | grep -qE '(^|/)node_modules/'; then
  c_warn "node_modules 被跟踪"
fi

# ---------------------------------------------------------------------------
hdr "5. 敏感内容扫描（公开仓库推送前）"
# 真实的 .env 不能进版本库；但 .env.example / .sample / .template 是**故意**提交的模板，不算问题。
env_tracked=$(git ls-files | grep -E '(^|/)\.env' || true)
if [ -z "$env_tracked" ]; then
  c_ok "无 .env 类文件被跟踪"
else
  env_real=$(printf '%s\n' "$env_tracked" | grep -vE '\.(example|sample|template|dist)$' || true)
  env_tpl=$(printf '%s\n' "$env_tracked" | grep -E '\.(example|sample|template|dist)$' || true)
  if [ -n "$env_real" ]; then
    while IFS= read -r f; do
      [ -n "$f" ] && c_bad "非模板 .env 被跟踪：$f"
    done <<< "$env_real"
  fi
  if [ -n "$env_tpl" ]; then
    n=$(printf '%s\n' "$env_tpl" | grep -c . || true)
    c_ok "只有模板文件（$n 个），不是真实凭据"
    # 模板里的敏感字段应当仍是占位符
    suspicious=$(printf '%s\n' "$env_tpl" | while IFS= read -r f; do
      [ -n "$f" ] || continue
      grep -inE '^[A-Z_]*(SECRET|PASSWORD|TOKEN|KEY)[A-Z_]*=' "$f" 2>/dev/null \
        | grep -viE 'change|your|example|placeholder|xxx|<|>|dev-secret' \
        | sed "s|^|$f:|"
    done || true)
    if [ -n "$suspicious" ]; then
      c_warn "模板中的敏感字段可能不是占位符，请人工确认："
      printf '%s\n' "$suspicious" | sed 's/^/      /'
    else
      c_ok "模板中的敏感字段均为占位符"
    fi
  fi
fi
# 模式刻意写得不会匹配到本脚本自身的文本
if git grep -qE 'BEGIN [A-Z ]*PRIVATE[[:space:]]+KEY' -- . >/dev/null 2>&1; then
  c_bad "发现疑似私钥内容"
else
  c_ok "无私钥内容"
fi
if git grep -qE 'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}' -- . >/dev/null 2>&1; then
  c_bad "发现疑似 GitHub token"
else
  c_ok "无 GitHub token"
fi

# ---------------------------------------------------------------------------
hdr "6. LICENSE 与署名"
if [ -f LICENSE ]; then
  if grep -q 'Permission is hereby granted, free of charge' LICENSE; then
    c_ok "LICENSE 含 MIT 授权正文"
  else
    c_bad "LICENSE 不含 MIT 授权正文"
  fi
  # GitHub 的 licensee 只认标准模板：版权段里混入说明文字会变成 NOASSERTION
  stray=$(awk 'seen && /^$/ {exit} /^Copyright/ {seen=1; next} seen && NF {print}' LICENSE | wc -l | tr -d ' ')
  if [ "$stray" -gt 0 ]; then
    c_warn "LICENSE 版权段混入了 $stray 行非 Copyright 文字，GitHub 可能识别为 NOASSERTION（说明文字建议放 NOTICE.md）"
  else
    c_ok "LICENSE 版权段格式干净"
  fi
else
  c_warn "没有 LICENSE 文件"
fi

# ---------------------------------------------------------------------------
hdr "7. 分支与工作区状态"
branch=$(git rev-parse --abbrev-ref HEAD)
printf '  当前分支：%s\n' "$branch"
dirty=$(git status --porcelain | wc -l | tr -d ' ')
if [ "$dirty" -gt 0 ]; then
  c_warn "有 $dirty 项未提交改动"
else
  c_ok "工作区干净"
fi
upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)
if [ -n "$upstream" ]; then
  ahead=$(git rev-list --count "$upstream..HEAD" 2>/dev/null || echo 0)
  printf '  跟踪：%s（领先 %s 个提交）\n' "$upstream" "$ahead"
else
  printf '  跟踪：无上游分支\n'
fi

# ---------------------------------------------------------------------------
hdr "8. remote 连通性"
if [ "$OFFLINE" -eq 1 ]; then
  c_warn "已指定 --offline，跳过"
else
  for r in $(git remote); do
    if git ls-remote --heads "$r" >/dev/null 2>&1; then
      c_ok "remote '$r' 可达"
    else
      c_bad "remote '$r' 不可达（主机密钥未信任？网络问题？）"
    fi
  done
fi

# ---------------------------------------------------------------------------
printf '\n\033[1m结果\033[0m  通过 %d，失败 %d，警告 %d\n' "$PASS" "$FAIL" "$WARN"
if [ "$FAIL" -gt 0 ]; then
  printf '\033[31m存在失败项，先修好再推送。\033[0m\n'
  exit 1
fi
printf '\033[32m可以推送。\033[0m\n'
