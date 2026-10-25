#!/usr/bin/env bash
#
# sync_upstream.sh —— 把上游的 master 同步到自己的仓库
#
# 设计目标：安全、可定时、失败时"什么都不做"而不是留下半成品。
#
# 用法：
#   sync_upstream.sh                 # 默认 ff-only：要求自己的 master 与上游完全一致
#   sync_upstream.sh --merge         # 自己的 master 含自己的合并提交时，用 merge
#   sync_upstream.sh --dry-run       # 只做检查，不推送
#   sync_upstream.sh --branch main   # 同步别的分支（默认 master）
#
# 环境变量：
#   UPSTREAM_REMOTE  只读上游（默认 upstream）
#   ORIGIN_REMOTE    自己的可写仓库（默认 origin）
#   BRANCH           要同步的分支（默认 master）
#
# 退出码：
#   0  成功，或本来就是最新
#   1  fetch / 合并 / 推送失败（冲突会先 abort，再退出）
#   2  用法错误，或不在 git 仓库里
#
# 两个模式的区别：
#   --ff-only  只允许快进。这要求"自己的 master 永远不掺入自己的修改"。
#              好处是永远不会冲突、永远与上游逐字节一致，定时任务可以完全无人值守。
#   --merge    允许把上游合并进一个已经含有自己提交的 master。
#              冲突时**不会**自动解决，会回滚并报错，需要人工干预。
#
# 本脚本从不使用 --force。
#
set -uo pipefail

MODE="ff-only"
DRY_RUN=0
BRANCH="${BRANCH:-master}"
UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
ORIGIN_REMOTE="${ORIGIN_REMOTE:-origin}"

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ff-only) MODE="ff-only" ;;
    --merge)   MODE="merge" ;;
    --dry-run) DRY_RUN=1 ;;
    --branch)  shift; [ "$#" -gt 0 ] || { echo "错误：--branch 需要一个参数" >&2; exit 2; }; BRANCH="$1" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "错误：未知参数 $1" >&2; exit 2 ;;
  esac
  shift
done

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "错误：当前目录不在 git 仓库内" >&2
  exit 2
}
cd "$REPO_ROOT" || exit 2

log() { printf '[sync] %s\n' "$1"; }
err() { printf '[sync] 错误：%s\n' "$1" >&2; }

# 同步过程中会 checkout 到目标分支。退出时（成功或失败）切回用户原来的分支，
# 否则定时任务会在用户不知情时把工作副本留在 master 上。
original_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
restore_branch() {
  local now
  now="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
  if [ -n "${original_branch:-}" ] && [ "$original_branch" != "$BRANCH" ] && [ "$now" = "$BRANCH" ]; then
    if git checkout "$original_branch" >/dev/null 2>&1; then
      log "已切回 $original_branch"
    else
      err "无法切回 $original_branch，请手动切换"
    fi
  fi
}
trap restore_branch EXIT

# --- 1. 工作区必须干净：避免把未提交的改动卷进合并 -------------------------
if [ -n "$(git status --porcelain)" ]; then
  err "工作区有未提交的改动，先处理掉再同步（定时任务不应在有改动时运行）"
  git status --short >&2
  exit 1
fi

# --- 2. 拉取上游 -----------------------------------------------------------
log "fetch $UPSTREAM_REMOTE ..."
if ! git fetch --prune "$UPSTREAM_REMOTE"; then
  err "拉取 $UPSTREAM_REMOTE 失败（网络？主机密钥？权限？）"
  exit 1
fi

UPSTREAM_REF="$UPSTREAM_REMOTE/$BRANCH"
if ! git rev-parse --verify "$UPSTREAM_REF" >/dev/null 2>&1; then
  err "找不到上游分支 $UPSTREAM_REF"
  exit 1
fi

# --- 3. 切到目标分支 -------------------------------------------------------
if [ "$original_branch" != "$BRANCH" ]; then
  log "切换分支：$original_branch -> $BRANCH"
  if ! git checkout "$BRANCH"; then
    err "切换到 $BRANCH 失败"
    exit 1
  fi
fi

before="$(git rev-parse HEAD)"
upstream_sha="$(git rev-parse "$UPSTREAM_REF")"
log "上游 $UPSTREAM_REF = $upstream_sha"
log "本地 $BRANCH = $before"

if [ "$before" = "$upstream_sha" ]; then
  log "已经与上游一致，无需同步"
  exit 0
fi

# --- 4. 合并 ---------------------------------------------------------------
if [ "$MODE" = "ff-only" ]; then
  if ! git merge --ff-only "$UPSTREAM_REF"; then
    err "无法快进：$BRANCH 上有你自己的提交。"
    err "如果 $BRANCH 确实收敛了你的工作，请改用 --merge；"
    err "如果希望保持与上游完全一致，请把工作留在独立分支上。"
    exit 1
  fi
else
  if ! git merge --no-edit "$UPSTREAM_REF"; then
    err "合并冲突，已回滚这次合并，$BRANCH 保持原样。请人工处理。"
    git merge --abort 2>/dev/null || true
    exit 1
  fi
fi

after="$(git rev-parse HEAD)"
if [ "$before" = "$after" ]; then
  log "上游没有带来新的提交（已包含在 $BRANCH 中）"
  exit 0
fi

# --- 5. 推送 ---------------------------------------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
  log "[dry-run] 将把 $after 推送到 $ORIGIN_REMOTE/$BRANCH"
  exit 0
fi

log "推送 $BRANCH 到 $ORIGIN_REMOTE ..."
if ! git push "$ORIGIN_REMOTE" "$BRANCH"; then
  err "推送失败。本地已经合并成功，但远程没更新。"
  exit 1
fi

log "完成：$before -> $after，已推送到 $ORIGIN_REMOTE/$BRANCH"
