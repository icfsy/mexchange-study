# yyin/ —— 本仓库中由我新增的内容

上游 [mExchange](https://github.com/afterburn/mExchange)（作者 Kevin Karsopawiro）的原始代码
不在这里；这个目录只放我加的三样东西。完整署名见仓库根目录的 [`NOTICE.md`](../NOTICE.md)。

| 目录 | 内容 |
|---|---|
| [`docs/`](docs) | 架构导读（英文 + 中文）、交易所业务知识笔记、v2 目标架构设计 |
| [`mexchange_py/`](mexchange_py) | Python 业务逻辑参考模型：7 个可执行场景 + 19 个测试 |
| [`scripts/`](scripts) | 推送前自检脚本 |

## docs/

| 文件 | 说明 |
|---|---|
| [`ARCHITECTURE_GUIDE.md`](docs/ARCHITECTURE_GUIDE.md) / [`ARCHITECTURE_GUIDE.zh-CN.md`](docs/ARCHITECTURE_GUIDE.zh-CN.md) | 现有架构与代码的导读（英文 / 中文），含逐条核查过的结论与五条端到端链路 |
| [`EXCHANGE_DOMAIN_NOTES.md`](docs/EXCHANGE_DOMAIN_NOTES.md) | 行业业务知识：撮合与分配算法、订单类型与 TIF、自成交防护、金额精度与记账、可审计与合规、交易前风控、行情与订单流的通道分离、FIX、清算与结算 |
| [`ARCHITECTURE_V2.md`](docs/ARCHITECTURE_V2.md) | 面向生产的目标架构：事件日志、风控网关、FIX 网关、异步结算、行情缺口恢复；含 SQL 草图、时序、故障语义与六阶段迁移路线 |

## meexchange_py/

业务逻辑的 Python 复刻，**刻意去掉** UDP / FlatBuffers / 异步 / 锁等与业务无关的部分。

```bash
cd yyin/mexchange_py
python3 simulator.py --all                            # 跑全部 7 个场景
python3 -m unittest discover -s tests -v              # 19 个测试
```

细节见 [`mexchange_py/README.md`](mexchange_py/README.md)。

## scripts/verify_repo.sh

推送前自检。它会独立复核那些**看起来对但其实错**的问题：

```bash
yyin/scripts/verify_repo.sh              # 全部检查
yyin/scripts/verify_repo.sh --offline    # 跳过 remote 连通性检查
```

检查项：

1. **已提交内容 == 工作区内容**（逐字节）——本项目真实踩过的坑：提交进去的是过期版本
2. Python 语法（用 `compile()`，不生成 `__pycache__`）
3. **包名一致性**——独立从目录名推导"应该叫什么"，再要求所有绝对 import 完全一致
4. 不该进版本库的东西（`__pycache__`、`*.pyc`）
5. **敏感内容扫描**——真实 `.env`、私钥、GitHub token；模板文件单独放行并检查是否仍是占位符
6. `LICENSE` 是否为标准 MIT 文本（版权段混入说明文字会让 GitHub 报 `NOASSERTION`）
7. 分支与工作区状态
8. remote 连通性（`--offline` 可跳过）

退出码 0 = 通过，1 = 有失败项。

### 为什么要有这个脚本

准备把本仓库推到公开 GitHub 时，踩过两个坑：

- 有一次提交进去的是**过期版本**，而且包名被拼成了 `meexchange`（多一个 `e`），推上去会直接
  `ModuleNotFoundError`；
- 更糟的是，我当时的验证脚本**匹配了同一个错误拼写**，于是验证"全部通过"，把问题掩盖了。

教训是：**验证不能与被测对象共享同一个假设**。所以这个脚本只做字节比较、语法编译、
名称对照和模式扫描，不依赖代码里的任何判断。

脚本本身也做过反向测试：故意注入 `meexchange` 拼写后，三项检查同时报错、退出码 1；还原后恢复通过。
