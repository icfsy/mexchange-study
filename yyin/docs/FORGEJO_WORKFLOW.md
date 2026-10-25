# Forgejo 仓库设置：自己的仓库 + 自动跟随上游

目标：**上游的镜像保持只读、自己的仓库可写、`master` 能自动跟上上游**。

## 目标拓扑

```
GitHub  afterburn/mExchange              ← 真正的上游（公开）
            │
            │ Forgejo 自动 pull（只读镜像，保持纯净）
            ▼
Forgejo  upstream/mExchange              ← 只读。不要往这里推东西
            │
            │ 定时 fetch + merge（本仓库的 sync_upstream.sh）
            ▼
Forgejo  yyin/mExchange                  ← 你自己的仓库（可写）
            │  · master   ← 跟随上游
            │  · 其它分支  ← 你的修改
            │
            │ 需要时 push
            ▼
GitHub   icfsy/mexchange-study           ← 公开成果
```

**为什么不能直接推到 `upstream/mExchange`**：它是 Forgejo 的镜像仓库，推送会被直接拒绝：

```
Forgejo: Mirror Repository upstream/mExchange is read-only
```

这是正确行为——镜像如果可写，下次 pull 就会覆盖或产生分叉。

## 关键取舍：`--ff-only` 还是 `--merge`

这是整个方案里唯一需要你决策的地方，而且它取决于你的用法：

| | `--ff-only` | `--merge` |
|---|---|---|
| 前提 | `master` **永远不掺入**你自己的提交 | `master` 可以包含你的合并提交 |
| 同步方式 | 快进，`master` 与上游逐字节一致 | 把上游合并进来，产生 merge commit |
| 会不会冲突 | **永远不会** | 会（但你只新增 `yyin/`，实际很难冲突） |
| 能否无人值守 | **可以** | 冲突时需要人工 |
| 你的工作放哪 | 长期分支；**不要 merge 进 master** | 分支；需要时 merge 进 master |

> **重要**：一旦你把某个分支 merge 进了 `master`，`master` 就再也不能被上游快进了，
> 之后必须用 `--merge`。这两者是互斥的，不存在"既保持逐字节一致、又包含自己的合并"。

按你描述的用法（"需要的时候我会把我的 branch merge 到 master"），你要的是 **`--merge` 模式**。
好处是你的改动都是**新增**（`yyin/` 目录），和上游的修改基本不会碰同一行，冲突概率很低。

如果你更想要"master 永远等于上游"，那就用 `--ff-only`，并且把工作**只留在分支上**、不合并进 master。

## 一次性设置

### 1. 在 Forgejo 上建仓库

打开 https://forgejo.home.yyinlab.dev/repo/create

- Owner 选 **yyin**
- Repository name 填 **mExchange**
- **不要**勾选 "Initialize repository"（保持空仓库，避免推送时冲突）
- 私有或公开都行（推送走 SSH，与可见性无关）

> 如果你的 Forgejo 提供 "Fork" 按钮，也可以直接 fork `upstream/mExchange`。
> 区别只是多了一条 "forked from" 的展示关系；后续步骤完全一样。

### 2. 给本地克隆配置三个 remote

> 先跑 `git remote -v`：如果已经能看到 `origin`（指向 `yyin/mExchange`）和 `upstream`
> （指向 `upstream/mExchange`），说明配置已完成，直接跳到第 3 步。

```bash
cd ~/dev/forgejo/mExchange

# 原来的 origin 其实是只读镜像，改名为 upstream 更准确
git remote rename origin upstream

# 你自己的仓库 = 新的 origin（可写）
git remote add origin ssh://git@forgejo.home.yyinlab.dev:2222/yyin/mExchange.git

# 公开成果仓库保持不变（如果还没有）
git remote add github git@github.com:icfsy/mexchange-study.git

git remote -v
```

预期：

```
origin    git@forgejo.home.yyinlab.dev:yyin/mExchange.git        （可写）
upstream  git@forgejo.home.yyinlab.dev:upstream/mExchange.git    （只读）
github    git@github.com:icfsy/mexchange-study.git               （公开）
```

### 3. 首次推送

```bash
git fetch upstream
git push origin master
git push origin docs/architecture-learning-guide   # 你的工作分支

# 让本地 master 跟踪自己的仓库
git branch --set-upstream-to=origin/master master
```

## 自动同步

### 方案 A：cron（最简单）

```cron
# 每天 06:17 和 18:17 同步一次
17 6,18 * * * cd $HOME/dev/forgejo/mExchange && ./yyin/scripts/sync_upstream.sh --merge >> $HOME/.local/state/sync-mexchange.log 2>&1
```

> 现代 macOS 上 cron 需要"完全磁盘访问权限"才能读你的 SSH 密钥，容易踩坑。
> 在 Mac 上更稳的是方案 B。

### 方案 B：launchd（macOS 推荐）

`~/Library/LaunchAgents/dev.yyin.sync-mexchange.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.yyin.sync-mexchange</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>cd "$HOME/dev/forgejo/mExchange" &amp;&amp; ./yyin/scripts/sync_upstream.sh --merge</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>17</integer></dict>
    <dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>17</integer></dict>
  </array>
  <key>StandardOutPath</key><string>/tmp/sync-mexchange.log</string>
  <key>StandardErrorPath</key><string>/tmp/sync-mexchange.err</string>
</dict>
</plist>
```

```bash
launchctl load -w ~/Library/LaunchAgents/dev.yyin.sync-mexchange.plist
launchctl list | grep mexchange          # 确认已加载
launchctl start dev.yyin.sync-mexchange  # 手动触发一次
tail -f /tmp/sync-mexchange.log
```

`-lc` 是必要的：它让你登录 shell 的环境（PATH、SSH agent）可用，否则可能找不到 `git`/密钥。

### 方案 C：systemd timer（跑在 homelab 主机上）

`~/.config/systemd/user/sync-mexchange.service`：

```ini
[Unit]
Description=Sync mExchange fork with upstream
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/repos/mExchange
ExecStart=%h/repos/mExchange/yyin/scripts/sync_upstream.sh --merge
```

`~/.config/systemd/user/sync-mexchange.timer`：

```ini
[Unit]
Description=Sync mExchange fork twice a day

[Timer]
OnCalendar=*-*-* 06,18:17:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now sync-mexchange.timer
systemctl --user list-timers | grep mexchange
journalctl --user -u sync-mexchange -f
```

### 方案 D：Forgejo Actions（全在服务端）

在仓库里放 `.forgejo/workflows/sync-upstream.yml`：

```yaml
name: Sync with upstream
on:
  schedule:
    - cron: "17 6,18 * * *"
  workflow_dispatch:

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: master
          fetch-depth: 0
      - name: Merge upstream
        run: |
          git config user.name  "forgejo-actions"
          git config user.email "actions@forgejo.local"
          git remote add upstream https://forgejo.home.yyinlab.dev/upstream/mExchange.git
          git fetch upstream master
          git merge --no-edit upstream/master
          git push origin master
```

**三个前提**（这也是它不如方案 A/B/C 直接的原因）：

1. 实例必须启用 Forgejo Actions 并且有可用的 runner；
2. 定时工作流**只在默认分支上生效**，所以这个 yml 必须提交到 `master`——
   而一旦 `master` 多出这个文件，它就**不再与上游一致**，只能用 `--merge` 语义；
3. runner 需要能访问 `upstream/mExchange`（同实例内一般没问题）。

> 结论：除非你本来就打算跑 Actions runner，否则**用方案 A/B/C 更省事**，
> 而且能把 `master` 保持得更干净。

### 手动方案

如果你的 Forgejo 在仓库页面提供 "Sync fork" 按钮，可以手动点一下。但它没有定时能力。

## 日常流程

```bash
# 1. 从自己的 master 开分支
git switch master && git pull --ff-only origin master
git switch -c feat/something

# 2. 改东西、提交
git add -A && git commit -m "..."

# 3. 需要时发布到自己的仓库
git push -u origin feat/something

# 4. 需要合进 master 时
git switch master
git merge --no-ff feat/something
git push origin master
```

同步上游（手动）：

```bash
yyin/scripts/sync_upstream.sh --merge      # master 已含你的合并
yyin/scripts/sync_upstream.sh --ff-only    # master 仍然纯镜像
yyin/scripts/sync_upstream.sh --dry-run    # 只看会发生什么
```

同步脚本的行为（已测试）：

- 工作区不干净 → **直接拒绝**，不做任何操作
- `--ff-only` 但 master 有自己的提交 → 拒绝，并提示改用 `--merge`
- `--merge` 冲突 → **自动 `merge --abort`**，本地和远程都保持原样
- 已经是最新 → 退出码 0（幂等，适合定时任务）
- **退出时切回你原来的分支**（成功或失败都会），所以可以放心在正在用的工作副本里运行，
  不会把你留在 master 上
- 从不使用 `--force`

## 冲突处理

脚本冲突时会中止并报错。人工处理：

```bash
cd ~/dev/forgejo/mExchange
git fetch upstream
git merge upstream/master
# 解决冲突后
git add -A && git commit
git push origin master
```

因为你的改动集中在 `yyin/`，与上游的 `accounts/`、`gateway/`、`matching_engine/` 几乎不重叠，
实际冲突概率很低。

## 验证

```bash
# 1. 本地 master 是否是上游的祖先（--merge 模式下应始终为真）
git merge-base --is-ancestor upstream/master master && echo OK

# 2. 自己的仓库是否拿到了
git ls-remote --heads origin master
git rev-parse master

# 3. 上游镜像是否仍然只读（预期会报 read-only）
git push --dry-run upstream master
```

## 需要我做什么

这份文档里的 Forgejo 侧操作（建仓库、推送）需要**写权限**，我目前没有：

- 你在 https://forgejo.home.yyinlab.dev/repo/create 建好 `yyin/mExchange`（或 fork）后告诉我，我来配置 remote、推送分支并验证；
- 或者给我一个带 `write:repository` 权限的 Forgejo token，我全程自动完成。
