# mexchange_py —— mExchange 业务逻辑的 Python 参考模型

## 这是什么

把 mExchange（Rust）里的**业务规则**用 Python 重写一遍，让你能**读日志就懂业务**。

```
python3 simulator.py --all
```

跑完你就能看清：一笔订单从下单到成交，钱是怎么动的、顺序是什么、哪一步在冻结资金。

## 这不是什么（重要）

**这是业务逻辑模型，不是 Rust 版的移植，也不追求性能。** 具体地：

| 这里**有** | 这里**没有**（因为与"业务逻辑是什么"无关） |
|---|---|
| 价格-时间优先撮合 | UDP / FlatBuffers / 有没有组播 |
| 成交价取挂单方价格 | `tokio` 异步运行时、`Arc<RwLock<>>` |
| 复式记账 + 系统账户 + 资金守恒 | 零拷贝、批量 fsync、无锁队列 |
| 资金预留 / 释放 / 结算幂等 | 网络分区、进程崩溃、重复投递 |
| TIF（GTC/IOC/FOK）、Post-Only | HTTP / WebSocket 协议细节 |
| 先结算、后发布的顺序 | 高可用、分片、热备 |

**最关键的告诫**：单进程 Python 里，**异步、失败模式、一致性问题全都不会发生**——
而这些恰恰是 Rust 版一半设计的理由。所以：

- 学**业务规则** → 用这个模型（快、可读、可改）
- 学**架构取舍** → 回到 `yyin/docs/`：
  - `ARCHITECTURE_GUIDE.zh-CN.md` —— v1 现状与代码导读
  - `EXCHANGE_DOMAIN_NOTES.md` —— 行业业务知识（撮合算法、TIF、STP、记账、风控、FIX…）
  - `ARCHITECTURE_V2.md` —— v2 目标架构

## 快速开始

无需任何第三方依赖，只要 Python 3.10+（用到了 `X | None` 之类的新语法糖；实际只依赖标准库）。

```bash
cd yyin/mexchange_py

# 跑一个场景
python3 simulator.py scenarios/01_rest_then_fill.json
python3 simulator.py 01_rest_then_fill.json          # 也可以只给文件名

# 跑全部 7 个场景
python3 simulator.py --all

# 跑单元测试
python3 -m unittest discover -s tests -v
```

## 目录结构

```
mexchange_py/
├── simulator.py              # 入口：读场景 JSON，逐步打印发生了什么
├── mexchange/                # 业务内核
│   ├── domain.py             #   Side / OrderType / TimeInForce / Order / Fill
│   ├── book.py               #   撮合引擎（价格-时间优先 FIFO）
│   ├── ledger.py             #   复式记账账本 + 系统账户 + 资金守恒校验
│   ├── accounts.py           #   预留 / 撤单释放 / 结算 / 幂等
│   └── venue.py              #   编排：撮合 → 结算 → 发布（"先结算后发布"在这里看得见）
├── scenarios/*.json          # 7 个可执行场景
└── tests/test_business_rules.py   # 19 个测试 = 可执行的规格说明
```

### 与 Rust 服务的对应关系

| 本模型 | Rust 版 | 说明 |
|---|---|---|
| `domain.py` | `matching_engine` 的领域类型 | 唯一差异：这里的 `Fill` 带买卖双方账户 |
| `book.py` | `matching_engine/src/lib.rs` | `BTreeMap` → `dict` + 排序（注释里解释了有序映射买到了什么） |
| `ledger.py` | `accounts` 的 `ledger` | **这里是复式记账，v1 是单边流水** |
| `accounts.py` | `accounts` 的 `order.rs` / `trade.rs` | **这里预留是显式的，v1 靠负向 lock 流水** |
| `venue.py` | `matching_engine_service` | `process_order_command` 的逐步复刻 |

## 七个场景

| 场景 | 演示的业务规则 |
|---|---|
| `01_rest_then_fill` | 挂单后被吃；**成交价取挂单方价格**（买 100.50 挂单，卖 100.00 来吃，成交 100.50——卖方得到价格改善） |
| `02_partial_fill` | 部分成交后剩余继续挂单（`partially_filled`）；第二笔再次体现挂单方定价 |
| `03_market_order_partial` | 市价单从不挂单；吃不完的剩余被撤销并**释放预留** |
| `04_cancel` | 下单冻结资金、撤单释放——**注意账本余额从没变过，变的只是"可用"** |
| `05_insufficient_funds` | 余额不足在**到达撮合引擎之前**就被拒；订单簿完全没被触碰 |
| `06_tif_and_post_only` | **TIF 是撮合语义差异，不是参数差异**：Post-Only 预判会成交就拒绝；FOK 预演流动性不足就全额拒绝（订单簿不变）；IOC 成交后不挂剩余 |
| `07_self_trade` | **引擎不阻止自成交**：制造虚假成交量、被收两次手续费、资产净变化为 0。v1 的 `Fill` 只有订单 ID 没有账户，**结构上做不到 STP** |

### 怎么读日志

每一行都标了 **actor**，看 actor 的切换就知道请求在服务之间怎么走：

```
[  1] client     place_order bid limit qty=10 price=100.50 tif=gtc  (account=alice)
[  2] accounts   预留 1006.00 EUR → 可用 EUR=8994.00
[  3] engine     撮合完成：0 笔成交，状态=open，挂单 @ 100.50
...
[  6] client     place_order ask limit qty=10 price=100.00 tif=gtc  (account=bob)
[  7] accounts   预留 10.00 KCN → 可用 KCN=0.00000000
[  8] engine     撮合完成：1 笔成交，状态=filled
[  9] accounts   结算成交 10.00000000 KCN @ 100.50 EUR → trade-000003
[ 10] engine     发布 Fill 事件 ← 注意：发生在结算之后
```

**第 9、10 步的顺序就是重点**：v1 刻意先结算再发布，以保证"客户端能看到的成交，一定是已经进账本的成交"。
代价是热路径被一次 HTTP + 数据库事务绑住（见 `ARCHITECTURE_GUIDE.zh-CN.md` 第 3.3 节与 7.2 节）。

时间戳是**虚拟的**（每步 +1），不读真实时钟——所以同一个场景永远得到同样的输出。这就是**确定性**的最直观体现。

## 与 v1 的三处刻意差异

这不是"移植"，有三处刻意做成正确的做法，并在代码注释里标了 v1 的区别：

| | Rust v1 | 本模型 | 为什么 |
|---|---|---|---|
| **记账** | 单边流水，无系统账户 | **复式记账**，`SYS_FEE` / `SYS_MINT` / `SYS_EXTERNAL` | v1 无法自证"钱没多也没少" |
| **手续费** | 算出来存进 `trades` 表，**从不入账** | 真的记入系统手续费账户 | v1 的交易所没有收入科目 |
| **锁资金** | 一条负的 `lock` 流水；`balances.locked` 列**从未被写入** | 显式 `reservation`（可 TTL、可释放） | v1 的语义藏在余额里，且死列会误导人 |

账本每次写入都校验**每个资产的净额为零**，不平衡直接抛异常拒绝写入。
`simulator` 最后打印的 `资金守恒检查: OK (EUR=0.00, KCN=0.00000000)` 就是这条不变量的结果。
**这是 v1 做不到的事情。**

## 测试覆盖

19 个测试，分四组：

- `MatchingRules`（6）——价格优先、时间优先、挂单方定价、市价单不挂单、部分成交、完全成交不入簿
- `TimeInForceRules`（5）——FOK 拒/成、IOC 撤剩余、Post-Only 拒/挂
- `LedgerRules`（4）——不平衡分录被拒、入金平衡、成交守恒且收手续费、**结算幂等**
- `ReservationRules`（3）——余额不足不入簿、撤单全释放、市价单剩余释放
- `KnownGaps`（1）——**自成交不被阻止**（测试通过恰恰说明缺口存在）

> `KnownGaps` 这组是刻意的：把"已知缺口"写成测试，是为了让缺口**可见**、不随时间被遗忘。
> v2 会通过风控层的 STP 关掉它（见 `ARCHITECTURE_V2.md` 4.3 节）。

## 怎么改着玩

几个投入产出比很高的实验：

1. **把 FIFO 换成 Pro-Rata**：在 `book.py` 的 `PriceLevel` 里保留每单原始量，按比例分配。
   用场景 01/02 对比输出——这会直接回答"分配算法如何影响业务"（见 `EXCHANGE_DOMAIN_NOTES.md` 第 1 节）。
2. **加 STP**：在 `venue.py` 下单前比对 `order.account_id` 与对手方，实现 Cancel-Resting / Cancel-Both。
   场景 07 就是它的验收用例。
3. **加价格笼子**：在 `accounts.py` 校验里加"价格偏离参考价 ±X% 就拒单"。场景 05 那类拒绝路径已经现成。
4. **把结算挪到事件之后**：故意把 `venue.py` 里"先结算再发布"倒过来，观察不一致会出现在哪里
   ——这能让你亲手体会 v1 那句 `INCONSISTENCY ... Manual intervention required` 的来历。
5. **加断线重连/序号缺口**：给 `venue.publish_book_update()` 的 `seq` 加上缺口检测，
   模拟丢一个增量后客户端如何失同步、快照如何救回来（见 `ARCHITECTURE_V2.md` 4.5 节）。

## 一个诚实的说明

本模型由我更早的分析产出，**业务逻辑已通过 7 个场景 + 19 个测试验证**。
但如果你在本地运行时遇到问题（尤其是环境相关的），告我一声，我来修。

另外：**不要**试图用这个模型去推断 Rust 版的性能或并发行为——
它把那些维度整个删掉了，而这正是它的用处。
