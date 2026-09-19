# mExchange 架构学习指南

> English version: [`ARCHITECTURE_GUIDE.md`](ARCHITECTURE_GUIDE.md)

这是一份面向**没有 Rust 经验**、但想搞懂**系统是怎么搭起来的**读者的仓库导读。

你不需要逐行读 Rust。你需要做到的是：对任何一个组件，都能回答四个问题——
*它拥有什么状态？它跟谁通信？用什么协议？它挂掉时会发生什么？*

---

## 如何使用本指南

按顺序走三遍。不要跳过第一遍——大部分困惑都来自"还没拿到地图就开始读代码"。

| 遍数 | 目标 | 章节 | 时间 |
|------|------|------|------|
| **1. 地图** | 把整个系统装进脑子 | 第一部分、第四部分、第六部分 | ~45 分钟 |
| **2. 追链路** | 跟着一笔订单走完真实代码 | 第五部分（Trace A、B、C） | ~90 分钟 |
| **3. 做判断** | 形成自己的架构观点 | 第三部分、第七部分 | ~2 小时 |

第二部分（Rust 生存手册）当工具书用：先扫一遍，遇到看不懂的符号再回来查。
第八到第十一部分是学习计划、术语表和自测题。

---

# 第一部分 全局认知

## 1.1 这个系统是什么

一个**现货加密货币交易所**：用户充值资产、下买单/卖单，系统负责撮合。当前只配置了一个交易对
`KCN/EUR`（`SYMBOL` 环境变量，默认值见 `matching_engine_service/src/main.rs:113`）。

它是一个**微服务**系统：7 个 Rust 组件 + 1 个 React 前端 + 一个 PostgreSQL 数据库。
每个 Rust 组件是一个独立的构建单元（"crate"），有自己的 `Cargo.toml` 和自己的可执行文件——
大致相当于"每个目录一个 `package.json` + 一个服务"。

**尚未实现**（`README.md` 把它们列在目标架构里）：风控引擎(risk engine)、独立结算服务、管理后台、
Redis、交易对注册中心、事件日志/回放、冗余与故障切换。

## 1.2 60 秒心智模型

用**三个平面**来思考。几乎每个架构问题都能通过"这是哪个平面"来回答。

```
客户端平面               边缘                    热路径（延迟敏感）              记录源
─────────────           ────                    ───────────────────────────     ────────────────

  浏览器  ──HTTP/WS──►  网关 Gateway ──UDP+JSON──► 撮合引擎服务 ──┐
  (React)                :3000      (下单)                          │
                            │                                     │ 进程内调用
                            │ HTTP/JSON                           ▼
                            │                            matching_engine（库）
                            │                            • BTreeMap 订单簿
                            │                            • 价格-时间优先
                            │                                     │
                            │◄──UDP+FlatBuffers───────────────────┘
                            │   （成交、订单簿增量）
                            │
                            │ HTTP/JSON（结算、锁仓、撤单）──────────┐
                            ▼                                     ▼
                       accounts :3001  ◄──── HTTP/JSON（结算）── 账户库 PostgreSQL
                       • 用户、OTP/JWT                              （users、balances、
                       • 余额（available）                            ledger、orders、
                       • 只追加账本                                   trades、ohlcv）
                            ▲
                            │ HTTP/JSON（代理）
                            │
                       market_data :3002 ◄── WS（作为客户端订阅网关）──┘
                       • OHLCV K 线
```

**最关键的一个认知：** 撮合引擎把实时订单簿放在**内存**里，账户服务把**持久化**记录放在
PostgreSQL 里。因此系统里有**两个必须靠一个显式协议保持一致的事实来源**（先结算、后发布事件）。
理解了这个协议，就理解了这套架构——见第五部分 Trace A 和 7.2 节。

## 1.3 组件一览

| 组件 | 类型 | 监听 | 通信对象 | 拥有的状态 |
|---|---|---|---|---|
| `matching_engine/` | **纯库**（无 I/O、无 async） | — | — | 订单簿数据结构 + 撮合算法 |
| `udp_proto/` | **纯库** | — | — | 线格式：分帧、批处理、心跳、FlatBuffers 编解码 |
| `matching_engine_service/` | 服务 | TCP `:8080`（健康检查）、UDP `:9100`（收单） | UDP → 网关 `:9101`；HTTP → accounts | 唯一的可变 `OrderBook`、订单生命周期编排、订单簿发布 |
| `gateway/` | 服务（边缘） | TCP `:3000`（HTTP + WebSocket）、UDP `:9101`（收事件） | UDP → 撮合 `:9100`；HTTP → accounts、market_data | 客户端会话、订阅关系、按客户端分发事件、反向代理、代验证 token |
| `accounts/` | 服务 + 库 | TCP `:3001` | PostgreSQL | 用户、OTP/JWT、余额、账本、订单、成交——**系统记录源** |
| `market_data/` | 服务 | TCP `:3002` | WS → 网关；PostgreSQL | OHLCV K 线聚合 |
| `trading_bot/` | 服务（客户端） | — | WS → 网关（`:3000/ws`） | 做市策略、提供流动性 |
| `frontend/` | 单页应用 | `:80`（容器内）、`:5173`（宿主机） | 网关 HTTP + WS | 交易界面 |
| `e2e-tests/` | 测试框架（TypeScript） | — | 网关、accounts、PostgreSQL | 全流程集成测试 |

## 1.4 两条传输通道——核心设计决策

这是本仓库最有辨识度的选择，`README.md:76-99` 有完整解释。

**冷路径——HTTP/JSON**，用于非延迟敏感的一切：
认证、余额、订单**创建与锁仓**、结算、K 线历史。
易调试、易重试、天然幂等。

**热路径——UDP + FlatBuffers**，用于下单和市场事件：
`gateway → matching_engine_service`（下单）以及反向（成交、订单簿更新）。

在这里选 UDP 是站得住脚的：两个进程在同一机房/同一容器网络内，报文小且彼此独立，而 TCP 的三次握手
和队头阻塞会带来这套设计明确要避免的延迟。

选 FlatBuffers 的理由：零拷贝读取、每条消息不产生额外分配、schema 在编译期被检查
（`udp_proto/src/market_data.fbs`）。

**你因此接受的代价：** UDP 会丢包、会乱序。代码没有实现重传——它实现的是*检测*
（序号、gap 计数器、流状态），把恢复交给对账逻辑。见 `udp_proto/src/receiver.rs:60-108`
（`StreamState`、`GapInfo`、`ReceiverStats`）。

> **架构层面的结论：** 热路径是*有损*的，所以一切不能丢的东西（资金变动）被刻意放在*冷路径*上走
> HTTP。这就是为什么结算是 HTTP 调用而不是一条 UDP 消息。

## 1.5 你会反复看到的五个模式

1. **纯内核 + 命令式外壳。** `matching_engine` 和 `udp_proto` 是没有网络、没有数据库、没有 `async`
   的库。所有 I/O 都在服务层 crate 里。这就是为什么内核可以独立做单元测试和基准测试。
2. **库与状态的分离。** `matching_engine_service` 拥有唯一一个 `Arc<RwLock<OrderBook>>`，
   并且是它**唯一**的写入者。
3. **广播通道做扇出。** 一个生产者（UDP 事件接收器）→ 多个消费者（WebSocket 客户端）。
   见 `gateway/src/state.rs`（`broadcast::Sender<MarketEvent>`）。
4. **服务之间不共享内存，但共享数据库。** `accounts` 和 `market_data` 连的是同一个 PostgreSQL。
   这里没有"每个服务一个库"。
5. **信封 + 带标签的联合体。** 每条线上消息都是 `{type: "...", ...}`（JSON）或一个 FlatBuffers
   `union`——一种消息外形，多种事件类型。

---

# 第二部分 架构师用的 Rust 生存手册

你只需要大约 12 个概念。每一行：*它是什么 → 架构上意味着什么 → 去哪看*。

| Rust 语法 | 通俗解释 | 架构含义 | 仓库中的例子 |
|---|---|---|---|
| `Cargo.toml` | 包清单（相当于 `package.json`） | 声明依赖；`path = "../matching_engine"` 表示**仓库内依赖** | `gateway/Cargo.toml` |
| `main.rs` | 程序入口 | 有 `main.rs` = "这是一个可运行的服务" | 所有服务 |
| `lib.rs` | 库根文件 | 同时有 `lib.rs` 和 `main.rs` = "逻辑可以在不起服务的情况下被测试" | `accounts/src/lib.rs` |
| `mod` / `pub` / `use` | 模块系统 + 可见性 | `pub mod` = 对外 API 表面，也就是组件的边界 | `gateway/src/lib.rs` |
| `struct` | 记录 / 数据类 | 领域对象：`Order`、`Fill`、`OrderBook` | `matching_engine/src/lib.rs:24` |
| `enum` | **带标签的联合体**（和类型），不只是常量枚举 | 这是 Rust 表达"N 种形态之一"的方式——事件、买卖方向、状态、错误都用它 | `gateway/src/events.rs:39` 的 `MarketEvent` |
| `Option<T>` | "可能有值"（没有 `null`） | 强制处理"市价单没有价格"这种情况 | `price: Option<Price>` |
| `Result<T, E>` | "值或错误"（没有异常） | 每个可能失败的调用在签名里就看得见——错误是必须被处理的值 | `fn new(...) -> anyhow::Result<Self>` |
| `async` / `await` + `tokio` | 协作式并发运行时 | 少量线程支撑大量连接；`tokio::spawn` = "并发跑这段" | `axum` 处理器、`gateway/src/server.rs:169` 的 `tokio::spawn` |
| `Arc<T>` | 共享的、引用计数的、不可变指针 | 让多个任务共享同一个对象；要可变就得配合锁 | `Arc<UdpOrderSender>` |
| `RwLock` / `Mutex` | 读写锁 / 互斥锁 | **临界区**。`Arc<RwLock<OrderBook>>` = "一个订单簿，可多读，写唯一" | `matching_engine_service/src/main.rs:23` |
| `serde` + `#[derive(Serialize, Deserialize)]` | 声明式 JSON 编解码 | Rust 结构体与线上 JSON 之间的契约，不需要手写解析 | `gateway/src/events.rs` |
| `trait` / `impl` | 接口 + 实现 | 支撑可替换行为 | `accounts/src/mail/mod.rs` 的 `MailService` |
| `#[cfg(test)]` / `#[test]` | 条件编译 + 测试标记 | 单测与代码同文件；`benches/` 放性能测试 | `udp_proto/src/tests.rs`、`matching_engine/benches/` |

## 2.1 最让非 Rust 读者困惑的两点

**（一）`enum` 不是 C 风格枚举。** Rust 的枚举变体可以携带数据：

```rust
pub enum MarketEvent {
    Fill { price: Decimal, quantity: Decimal, .. },
    OrderBookSnapshot { bids: Vec<PriceLevel>, asks: Vec<PriceLevel> },
    OrderCancelled { order_id: OrderId, filled_quantity: Decimal },
}
```

读法是："一个 `MarketEvent` 恰好是这些形态之一，编译器会强迫每个处理分支考虑所有形态。"
这就是为什么到处都是 `match event { ... }`——它是穷尽式模式匹配，相当于一个编译器不允许你漏掉分支的
`switch`。**你找到的每一个 `match`，都是"系统认为存在哪些情况"的完整清单。** 这些枚举是全仓库最好的
文档。从它们开始读。

**（二）所有权，两段话讲完。** Rust 会追踪每一份值归哪段代码"所有"，所有者消失时自动释放内存——
没有 GC，也不用手动 `free`。一份值不能有两个所有者，所以共享需要显式包装：
`Arc<T>`（共享只读，引用计数）和 `RwLock<T>`/`Mutex<T>`（共享*可变*，带锁保护）。
从架构上看这是礼物：**代码里每一个 `Arc<RwLock<...>>` 都是一个被显式声明的共享可变状态，
也就是一个你可以逐一枚举的并发热点。** 把它们 grep 出来，你就找到了系统的协调点。

跑这两条 grep——这是最快看到架构的方式：

```bash
grep -rn "Arc<RwLock\|Arc<Mutex\|tokio::sync::RwLock" --include=*.rs .
grep -rn "pub enum" --include=*.rs .
```

---

# 第三部分 组件深潜

按下面的顺序读。每一节结尾都有"要注意什么"。

## 3.1 `matching_engine/`——纯领域内核

**职责：** 给定一笔订单，决定它和谁撮合、改动订单簿、返回产生的成交。仅此而已。
没有网络、没有数据库、没有 async、没有时钟、没有日志。

**公开 API**（`matching_engine/src/lib.rs`）：

| 条目 | 含义 |
|---|---|
| `type OrderId = Uuid` | 订单用 UUID 标识，由**上游**（accounts）生成，引擎自己不生成 |
| `type Price = Decimal`、`type Quantity = Decimal` | 精确十进制运算——金额绝不能用浮点（`lib.rs:7-9`） |
| `enum Side { Bid, Ask }` | 买 / 卖 |
| `enum OrderType { Limit, Market }` | 限价 = 有价格约束；市价 = 吃掉现有流动性 |
| `struct Order` | `id, side, order_type, price: Option<Price>, quantity, remaining_quantity` |
| `struct Fill` | `buy_order_id, sell_order_id, price, quantity`——一次**撮合**，还不是结算后的成交 |
| `struct OrderResult` | `order_id, fills: Vec<Fill>, completed_orders: Vec<OrderId>` |
| `struct OrderBook` | 订单簿本体 |
| `add_limit_order(...) -> OrderResult` | 插入 + 撮合 |
| `add_market_order(...) -> OrderResult` | 只撮合，从不挂单 |
| `cancel_order(id) -> bool` | 撤单 |
| `restore_order(order)` | 把订单放回去——**用于回滚** |
| `best_bid / best_ask / spread / quantity_at_price` | 只读视图 |
| `get_bids(n) / get_asks(n)` | 前 N 档聚合价格档位（用于发布行情） |

**内部数据结构**（`lib.rs:104-111`）——整个设计的核心：

```rust
pub struct OrderBook {
    bids: BTreeMap<Price, PriceLevel>,   // 有序 map，以价格为键
    asks: BTreeMap<Price, PriceLevel>,
    orders: HashMap<OrderId, Order>,     // 按 id 查找 O(1)
}
```

* `BTreeMap` = **有序** map。遍历时价格天然有序，所以"最优价"就是
  `keys().next_back()`（买）和 `keys().next()`（卖）（`lib.rs:192-198`）。不需要额外排序。
* `PriceLevel { orders: VecDeque<Order>, total_quantity }`——**每个价位一个 FIFO 队列**
  （`lib.rs:62-67`）。`VecDeque` = 双端队列；新单 `push_back`，消费时 `pop_front`。
  这就是**价格-时间优先**中"时间"那一半：价格是 BTreeMap 的键，时间是队列中的位置。
* `HashMap<OrderId, Order>` = O(1) 撤单/查找，撤单不必扫描整个订单簿。
* `total_quantity` 是每个档位的缓存汇总，所以发布前 N 档是 O(档位数)，而不是 O(订单数)。

**订单生命周期：** `add_limit_order` 构造 `Order`，调用私有的 `match_order`
（从最优价开始遍历*对手方*订单簿，消费 FIFO 队列并产出 `Fill`，同时修改 `remaining_quantity`），
然后如果有剩余就 `add_order_to_book`；如果完全成交，则把订单 id 放进 `completed_orders`
（`lib.rs:122-142`）。

两个有商业含义的细节：

* **成交价取被动方（挂单方）的价格**，绝不是主动方的限价（`match_order` 读取被消费档位的价格）。
  买家出价 105 去撞挂着的 100 卖单，成交价是 100。这是标准的价格改善，也很容易写错。
* **`Fill` 不携带归属信息。** 它只有 `buy_order_id` / `sell_order_id`，所以引擎无法识别自成交——
  这个库中**没有任何自成交防护**。名为 `test_self_matching_prevented` 的测试只提交了不会交叉的订单，
  实际上没有测到自成交。

**要注意什么**

* `restore_order` 的存在正是因为*服务层*可能需要在结算失败时撤销一次撮合。纯内核提供了原语——
  **但没有任何地方调用它。** 全仓库 grep `restore_order` 只在 `lib.rs` 内部有命中，
  也就是说回滚协议被声明了、但没实现。另外即使真的调用它，订单会被重新排到档位的**队尾**，
  丢失原有的时间优先权。
* 市价单从不进入订单簿——未成交的剩余部分被内核直接丢弃，*服务层*负责通知账户
  （`main.rs:316-333`）。所以部分成交的市价单在 `OrderResult` 里不会留下任何关于被丢弃剩余量的痕迹。
* **没有 `OrderStatus` 类型，也没有状态字段。** 状态是隐含的：挂在簿上 = 存在于 `orders` map 中；
  完全成交或已撤销 = 不在其中。`Order` 和 `Fill` 上没有时间戳、没有序号、没有账户 ID。
* **没有任何校验**——没有 tick size、lot size、最小名义金额，也不检查数量是否为负或为零。
  `Decimal` 是有符号的，所以负数数量会被接受。这个库没有错误类型：缺失用 `Option`，
  失败用 `bool`。
* 尽管有"高性能"的说法，这里**不是零分配**：每次下单都会分配 `Vec<Fill>`、`Vec<OrderId>`
  以及一份包含*所有*对手方档位的 `Vec<Price>` 快照，`add_order_to_book` 还会克隆 `Order`。
  每秒 520 万单的说法只出现在 `matching_engine/README.md`，仓库里没有任何产物复现过它。
* 内核完全不知道手续费、用户身份和余额。它只认识订单 ID。**资金完全在它的边界之外。**

## 3.2 `udp_proto/`——协议库

**职责：** 把消息变成字节、再把字节变回消息，并且足以*检测*丢包。

两层结构（`udp_proto/src/protocol.rs`）：

**第一层——分帧。** 24 字节的 `PacketHeader`（version、header length、message count、
`stream_id`、`packet_seq`、`first_msg_seq`），后面跟 N 条消息，每条消息带 4 字节
`MessageHeader`（`msg_type`、`flags`、`msg_len`）。MTU 上限设为 1400 字节
（`MAX_MTU`，`protocol.rs:7`），以留出以太网载荷的余量。

| `MessageType` | 值 | 方向 |
|---|---|---|
| `OrderNew` | `0x01` | 网关 → 撮合 |
| `OrderCancel` | `0x02` | 网关 → 撮合 |
| `OrderReplace` | `0x03` | *已定义，未使用* |
| `MatchEvent` | `0x10` | 撮合 → 网关 |
| `BookSnapshot` / `BookUpdate` | `0x11` / `0x12` | *已定义；订单簿数据目前走 `MatchEvent`* |
| `PositionUpdate` | `0x20` | *已定义，未使用（纯现货）* |
| `Heartbeat` | `0x30` | 双向 |
| `Control` | `0x40` | *已定义，未使用* |

**第二层——载荷。** FlatBuffers，schema 在 `udp_proto/src/market_data.fbs`，生成的 Rust 代码在
`udp_proto/src/generated/market_data_generated.rs`（**生成文件，别读它**，读 `.fbs`）。

```fbs
union EventPayload { Fill, OrderBookSnapshot, OrderBookDelta, OrderCancelled, OrderFilled }
table MarketEvent { payload: EventPayload; }
root_type MarketEvent;
```

注意 `struct Uuid { high: uint64; low: uint64; }`——UUID 以两个整数传输，不是字符串。
另外注意 schema 里的注释（`binary.rs:80-83`）：**线格式用 `f64`**，而引擎内部用精确的 `Decimal`。
这个转换发生在边界上（`matching_engine_service/src/udp_transport.rs:200`）。
行情数据允许有精度损失，账务不允许。

并发模型（`udp_proto/src/sender.rs`、`receiver.rs`）：用一个**专用的操作系统线程**跑阻塞式
socket 循环，通过 `crossbeam-channel` 与 async 代码通信。批处理：消息最多累积
`max_batch_delay`（下单是 100 微秒）或直到 MTU 填满。统计数据用原子变量
（`SenderStats`、`ReceiverStats`），读取时不需要加锁。

**要注意什么**

* `stream_id` 区分两条逻辑流：`ORDER_STREAM_ID = 1`、`EVENT_STREAM_ID = 2`
  （`gateway/src/udp_transport.rs:53-54`）。
* 接收端维护 `StreamState { Initializing, Active, Degraded, Down }` 并统计
  `gaps_detected` / `total_gap_messages`。**丢包是被度量出来的，不是被修复的。**
* 那些"已定义但未使用"的 `MessageType` 变体是作者意图的地图——是理解这套设计想去哪里的廉价线索。

## 3.3 `matching_engine_service/`——热路径编排者

**职责：** 持有唯一的实时 `OrderBook`，通过 UDP 收单、执行撮合、**同步结算**、发布事件。
策略都在这一层。

状态（`main.rs:21-26`）：

```rust
struct AppState {
    orderbook: Arc<RwLock<OrderBook>>,
    event_sender: Arc<UdpEventSender>,
    symbol: String,          // "KCN/EUR" —— 每个进程只有一个交易对
}
```

四个并发活动（`main.rs:150-186`）：

1. UDP 收单任务 → `process_order_command`
2. 订单簿发布任务 → 每 **100 毫秒**读一次订单簿，计算增量或快照，通过 UDP 发出
3. 一个结算 HTTP 客户端（`settlement.rs`）
4. 一个 axum 服务，只在 `:8080` 暴露 `GET /health`

**`process_order_command` 中的关键顺序（`main.rs:223-383`）**——这个函数读两遍；**它就是架构本身**：

```
1. 加写锁拿到订单簿，执行 add_limit_order / add_market_order，然后释放锁
2. 对每一笔成交：POST /internal/settle 到 accounts（HTTP，最多重试 3 次，超时 5 秒）
3. 只对结算成功的成交，通过 UDP 发布 Fill 事件
4. 如果市价单只部分成交：
       POST /internal/cancel 到 accounts，然后发布 OrderCancelled
5. 对每个完全成交的订单发布 OrderFilled
6. 如果任何结算失败：打印 CRITICAL 日志然后继续  ← 实际上并没有执行回滚
```

**要注意什么（这些是有意思的架构发现）**

* **先结算、后发布**是刻意的顺序选择：客户端和下游消费者绝不能看到一笔尚未进账本的成交。
  代码注释在 `main.rs:258` 明确写了这一点。
* 锁在 HTTP 结算调用*之前*就释放了（`main.rs:252`）。这是对的：撮合临界区是微秒级，不是毫秒级。
* **部分失败的处理策略是"记日志然后继续"。** `main.rs:283-295` 打印
  `INCONSISTENCY: Orderbook shows fill but accounts not updated. Manual intervention required.`
  内核里存在 `restore_order` 来支持回滚，但在实时路径上没有任何调用。
  这是系统最大的正确性缺口——也是很好的讨论题。
* **快照/增量发布**（`OrderBookPublisher`，`main.rs:389-548`）：每 `SNAPSHOT_INTERVAL = 10`
  次更新发一次全量快照，其余发增量，最多 `MAX_LEVELS = 10` 档。发布器是与自己上一次的视图做差分——
  所以一个丢失的 UDP 包只会让状态失同步到下一次快照为止（每 10 次 tick ≈ 每秒一次，因为 tick 是 100ms）。
  **这就是丢包恢复机制**，而且是刻意做得很便宜的。
* 发布器会递增 `sequence`，但**网关目前没有用它做 gap 检测**——字段存在，消费者忽略了它。
  值得你亲自验证一下。
* `OrderAccepted` 在事件枚举里存在，但**在发送前被过滤掉了**
  （`udp_transport.rs:141-145`），因为它不在 FlatBuffers union 里。而 `main.rs` 里的顺序意味着
  它本来就只会在成交*之后*才被发出。概念重叠/残留。

## 3.4 `gateway/`——边缘

**职责：** 做唯一的公网入口。终结客户端连接、认证（委托给 accounts）、扇出行情、转发订单。

`GatewayServer`（`server.rs:26-40`）持有五块共享状态，这本身就是网关全部职责的最好总结：

```rust
state: GatewayState,                                          // 广播总线 + 客户端 id
order_sender: Arc<UdpOrderSender>,                            // 热路径出口
channel_manager: Arc<RwLock<ChannelManager>>,                 // 订阅、用户、机器人控制
orderbook_states: Arc<RwLock<HashMap<String, OrderBookState>>>, // 按交易对的订单簿镜像
proxy_state: ProxyState,                                      // 指向 accounts/market_data 的 HTTP 客户端
```

**客户端协议：WebSocket 上的 JSON-RPC 风格**（`websocket.rs:17-24`）：
`{"id": "...", "method": "...", "params": {...}}`，响应为 `{"id":..., "result":...}` 或
`{"id":..., "error":{code, message}}`。错误码有命名空间：标准 JSON-RPC
（`-32601`、`-32602`）加业务码（`10000` 未授权、`10001` 订单被拒、`10002` 撤单失败）。

| 方法 | 认证 | 作用 |
|---|---|---|
| `public/auth` | — | 调 accounts 的 `GET /api/me` 校验 JWT，绑定 `client_id → user_id` |
| `public/subscribe` | — | 订阅 `book.<sym>`、`ticker.<sym>.<interval>`、`lwt.<sym>.<interval>` |
| `private/subscribe` | token | 订阅私有频道 |
| `unsubscribe` | — | 取消订阅 |
| `private/place_order` | 必须 | 在 accounts 创建订单（锁资金）→ 发 UDP 下单 → 立即回 `order_id` |
| `private/cancel_order` | 必须 | 在 accounts 撤单（释放资金）→ 发 UDP 撤单 |
| `private/register_bot` / `private/bot_status` | — | 机器人控制通道 |

**两条彼此不同的扇出路径**——这是重要的设计细节：

* `GatewayServer::start_event_broadcaster`（`server.rs:181-283`）消费 UDP 事件流，
  把它折叠进按交易对的 `OrderBookState`，并把 `book.*`、`ticker.*`、`lwt.*` 通知广播给
  *订阅了这些频道*的客户端。
* 每个连接会自己 spawn 一个任务（`websocket.rs:455-476`）读同一个广播总线，
  把 `OrderFilled` / `OrderCancelled` **只转发给订单所有者**——私有事件不能泄漏给别人。
  归属关系通过 `ChannelManager::get_order_owner` 解析，在 `websocket.rs:778-780` 写入。

**订单簿复制**（`channel_updates.rs`）：网关按交易对维护自己的 `OrderBookState`，
把快照当作替换、把增量当作添加/更新/删除来应用。这是一个 **CQRS 读模型**——
从事件流派生出来的反规范化投影，与撮合引擎内部的权威订单簿彼此独立。

**HTTP 反代**（`proxy.rs`、`server.rs:124-141`）：网关把 `/auth/*`、`/api/me`、
`/api/balances`、`/api/orders`、`/api/faucet/*`、`/api/ohlcv`，以及值得注意的 `/internal/*`
转发给 accounts。这样浏览器只需要一个 origin（CORS 配置了显式 origin 并允许 credentials，
`server.rs:58-121`）。

**要注意什么**

* 网关**不**校验余额和授信；它转发给 accounts，让 accounts 拒绝。网关是适配器，不是策略所有者。
* `validate_token`（`websocket.rs:1014-1030`）每次认证都要走一次 HTTP 往返，而不是本地校验 JWT。
  这是"用单一权威换延迟和耦合"的取舍。
* `ChannelManager`（`websocket.rs:310-427`）用一个结构体同时装下了机器人注册、认证映射、
  订单归属和订阅关系，全部由一把 `RwLock` 保护。这是竞争点，也是拆分候选。
* `/internal/*path` 被公开代理（`server.rs:134`）意味着未鉴权的内部端点——包括
  `POST /internal/mint`——可以从浏览器 origin 直接访问。见 7.2。

## 3.5 `accounts/`——系统记录源

**职责：** 身份、资金、订单、成交。不在这个数据库里的，就等于没发生。
它是唯一持久化任何金融数据的组件。

**组成**（`accounts/src/lib.rs`、`main.rs`）：
`AppState { pool: PgPool, jwt_secret: String, mail: MailService }` 加上模块
`db`、`models`、`routes`、`mail`、`scheduler`。

**路由表面：**

| 前缀 | 鉴权 | 端点 |
|---|---|---|
| `GET /health` | — | 存活检查 |
| `/auth` | — | `POST /request-otp`、`/verify-otp`、`/signup`、`/refresh`、`/logout`，以及当 `ENVIRONMENT=development` 时的 `/dev-login` |
| `/api/me` | Bearer JWT | 当前用户 |
| `/api/balances` | Bearer JWT | `GET /`、`POST /deposit`、`POST /withdraw` |
| `/api/orders` | Bearer JWT | `POST /` 下单、`GET /` 列表、`GET /:id`、`DELETE /:id` 撤单、`GET /:id/fills`、`GET /trades` |
| `/api/faucet` | Bearer JWT | `POST /claim`、`GET /status` |
| `/api/ohlcv` | — | 历史 K 线 |
| `/internal` | **无** | `POST /settle`、`POST /cancel`、`POST /orders`、`DELETE /orders/:id`、`POST /mint` |

认证中间件（`main.rs:180-221`）：取出 `Authorization: Bearer <jwt>`，用
`TokenService::verify_access_token` 校验，加载 `User`，注入到请求 extensions 里。
无密码登录：`POST /auth/request-otp` 邮件发送 6 位验证码；`POST /auth/verify-otp` 用它换取
access token + httpOnly 的 refresh cookie。refresh token 以**哈希**形式存储，
并带 `revoked_at` 支持吊销（`001_create_users.sql`）。

**资金模型——最重要、也最容易被误读的部分。**

`balances` 表有**两个**金额列，`available` 和 `locked`，各自带
`CHECK (... >= 0)` 约束（`003_add_constraints.sql`）。**只有 `available` 会被写入。**
`locked` 被余额 API 读取（`routes/balances.rs:77`），但 `accounts/src/` 里没有任何代码路径更新它——
grep `locked =` 一个结果都没有。它是一个仅靠 CHECK 约束存活下来的残留列。
**不要基于它建立心智模型。**

真正的机制是**账本(ledger)**。"锁定资金"被实现为一条**负的
`entry_type = 'lock'` 账本记录，它减少 `available`**，`ledger.reference_id` 指向订单，
同时 `orders.lock_entry_id` 反向指向该记录。解锁则追加一条正的 `unlock` 记录。所以：

| 事件 | 账本记录 | 对 `available` 的影响 |
|---|---|---|
| 下限价买单 | `lock` −(price × qty) 的计价币 | 减少 |
| 成交（买方） | `unlock` +计价币、`trade` −计价币、`trade` +基础币 | 净效果：计价币 → 基础币 |
| 撤单 | `unlock` +剩余部分 | 增加 |
| 充值 / 提现 | `deposit` / `withdrawal` | ± |

`ledger` 表被声明为余额的**事实来源**，而它是**单边的，不是复式记账**：
每一行是某个用户的一条增量变化，附带一个滚动余额 `balance_after`；
没有科目表，也**没有交易所/系统账户**。两个后果都能在 `trade.rs` 里验证：

* 当一笔成交的一方是机器人（"匿名"）时，那一方被**整体跳过**——于是这笔交易凭空为买方创造了基础币、
  销毁了从买方扣走的计价币。系统里**没有任何资金守恒约束**。
* 手续费被计算出来并存在 `trades` 行上（`FEE_RATE = dec!(0.001)`），但**从未记入账本**。
  `EntryType::Fee` 已定义、也有 `Display` 实现，但 `accounts/src/` 里没有任何代码写入过一条。

**不可变性**确实由数据库强制执行：migration 003 安装了 `prevent_ledger_modification()` 和
`ledger` 表上的 `BEFORE UPDATE OR DELETE` 触发器，会抛出
`'Ledger entries cannot be modified or deleted'`。这是一个很强的不变量——
**但请看下面关于定时清理任务的警告，它可能把这个触发器关掉。**

`ledger.rs` 提供了 `append`、`append_in_tx`、`derive_balance`、`reconcile`——
也就是说余额是*可以从流水推导出来的*，并且可以和 `balances` 这张投影对账。
这两个辅助函数目前没有接到任何地方。

**并发控制：** 每一次账本变更都会先拿
`pg_advisory_xact_lock(compute_lock_key(user_id, asset))`，再读并更新余额
（`ledger.rs:84-240`）；`Trade::settle` 会按用户 id 排序后对所有相关用户加锁，以避免死锁。
这就是系统在不用 `SERIALIZABLE` 隔离级别的情况下防止丢失更新的方式。

**结算**（`internal.rs`、`models/trade.rs`、`models/order.rs`）：
`POST /internal/settle` 接收 `{symbol, buy_order_id, sell_order_id, price, quantity, timestamp}`。
如果某个订单 ID 查不到，那一方就被当作**匿名**（机器人）——这正是 migration `005` 把成交参与方
列改成可空的原因。幂等性靠 `exchange_fill_id`（来自撮合引擎的复合键）实现，背后是一个
**非部分唯一索引**，专门为了让 `ON CONFLICT` 能工作
（`006_exchange_fill_id_index.sql` 解释了早期的部分索引为什么不行）。错误码有语义，
并被撮合引擎的客户端消费：`PARTIAL_SETTLEMENT`、`ORDER_NOT_FOUND`、`ALREADY_SETTLED`
（`matching_engine_service/src/settlement.rs:151-177`）。

`Trade::settle` 在一个事务里执行：begin → 加咨询锁 → 买方侧（`unlock` 计价币、
`trade` −计价币、`trade` +基础币）→ 卖方侧（`unlock` 基础币、`trade` −基础币、`trade` +计价币）
→ 每一侧调用 `Order::add_fill` → `INSERT INTO trades ... ON CONFLICT (exchange_fill_id)
DO UPDATE SET id = trades.id` → commit。成功后路由还会调用 `OHLCV::update_from_trade`，
它的失败只记 `warn!`——K 线被明确当作次要数据。

`Order::add_fill` 是订单状态唯一发生变化的地方，它是一条带 `CASE` 的 SQL `UPDATE`：
`pending → partially_filled | filled`，且 `cancelled` 是粘性的。
**`open`、`rejected`、`expired` 被 CHECK 约束允许，但没有任何代码路径会设置它们。**

`models/order.rs` 提供 `place`、`cancel`、`cancel_internal`、`add_fill`、`get`、`list_for_user`。

**定时任务**（`scheduler.rs`）：周期性清理任务，**默认关闭**，由 `ENABLE_DB_CLEANUP` 控制，
保留 1 天数据。

**要注意什么**

* **Migration 是手写的**，不是 sqlx-migrate：`db::run_migrations` 对一个显式数组使用
  `include_str!`（`db/mod.rs:14-24`）。因此 `007_remove_exchange_order_id.sql` 在磁盘上存在，
  但**不在数组里**——它的改动被合并进了 `004`。文件存在不等于会执行。永远要核对那个数组。
* **结算的幂等性存在竞态。** `get_by_fill_id` 检查发生在 `pool.begin()` **之前**
  （`trade.rs:97-101`），而事务内的保证（`ON CONFLICT ... DO UPDATE SET id = trades.id`）
  在账本写入和 `add_fill` **之后**才执行。一个并发重复请求如果输掉了插入竞争，
  它的账本变更仍然会被提交，而冲突分支只是一次空更新。拿到咨询锁之后也没有重新检查 `fill_id`。
  这是"幂等键到底买到了什么"的绝佳讨论案例。
* **清理任务一旦开启是危险的。** 它执行 `DELETE FROM trades WHERE created_at < $1`
  （而实际列名是 `settled_at`）以及 `DELETE FROM ohlcv WHERE bucket < $1`（实际列名是
  `open_time`）；最糟的是它在删除账本行之前执行
  `ALTER TABLE ledger DISABLE TRIGGER ledger_immutable`，并用 `.ok()` 吞掉错误。
  失败的 `trades` 删除会提前返回，因此触发器可能被**留在关闭状态**，
  悄悄废掉 migration 003 建立的只追加不变量。
  这是本仓库里"一个'无害的后台任务'为什么值得和资金路径同等审查"的最好例子。
* 内部 API **没有任何鉴权**；它信任网络位置和一个 `X-User-Id` 头
  （`internal.rs:216-231, 275-290, 382-397`）。任何能访问 `:3001/internal/orders` 的人
  都能以任意用户身份下单，而 `/internal/mint` 无限制地从零创造余额。
* `accounts` 同时是库（`lib.rs`，供测试使用）和可执行文件（`main.rs`）。
  这个拆分正是 `accounts/tests/*.rs` 能够不启 HTTP 服务就运行的原因。
  那些测试是需要真实 PostgreSQL 的数据库级集成测试；这里没有进程内的 HTTP 处理器测试框架。
* Access token 有效期 15 分钟且无法吊销（没有黑名单）；refresh token 有效期 30 天、
  以**哈希**存储、每次使用轮换、且只通过 httpOnly cookie 传递。
  那个 cookie 上 `secure(false)` 是硬编码的，所以它会通过明文 HTTP 发送——
  本地无所谓，生产环境是错的。
* `Otp::cleanup_expired` 存在但从未被调用，所以 `otps` 表会无限增长。

## 3.6 `market_data/`——派生的读模型

**职责：** 把成交流转成 OHLCV K 线。

`market_data/src/main.rs`：连数据库、spawn `start_websocket_consumer(gateway_ws_url)`
——它是**网关的 WebSocket 客户端**，订阅的和浏览器一样的公共行情——
把成交流喂给 `OhlcvAggregator`（`ohlcv.rs`），并在 `:3002` 暴露
`GET /api/ohlcv?symbol=&interval=&limit=`。

**要注意什么**

* 这个服务靠*消费事件*重建历史，而不是去读撮合引擎，并且它写的是和 `accounts` **同一张 `ohlcv` 表**
  ——`accounts` 也会在 `POST /internal/settle` 里同步更新 K 线
  （`accounts/src/routes/internal.rs:138`）。所以**同一条时间序列有两个独立的写入者**，
  用的是同一个冲突键。这是冗余的，而且引出一个代码没有回答的问题：两者不一致时谁说了算？
* 因为 `accounts` 也会在结算路径上写 K 线，`market_data` 宕机并不会立刻丢历史——
  但两条路径在 `volume`/`trade_count` 的累加上可能分叉，而且两边都没有从 `trades` 表回填的例程。
* 它是共享 PostgreSQL 数据库的第二个写入者——是共享数据库的耦合，不是服务独占的存储。
* 它的 HTTP 接口（`:3002` 上的 `GET /api/ohlcv`）**实际上无法通过网关访问**：
  网关把 `/api/ohlcv` 路由到 accounts，而 `proxy_market_data` 从未挂到任何路由上。

## 3.7 `trading_bot/`——一等公民客户端

**职责：** 制造流动性。从架构上看它是一个**外部客户端**：像用户一样连上网关 WebSocket、
认证、下单（`gateway_client.rs`，760 行——全仓库最大的单文件）。

策略（`strategies/`）：`market_maker`、`aggressive`、`random`、`mean_reversion`、
`random_walk`、`liquidity_provider`。指标在 `indicators.rs`。CLI 用 `clap`：
`--gateway-host`、`--gateway-port`、`--symbol`、`--all` 等（`main.rs`）。

**为什么这是一个好信号：** 因为机器人只是客户端，它持续地检验公开 API，无法伸手进内部。
这也意味着从 accounts 的视角看机器人订单是"匿名"的——这正是成交表参与方列可空的原因。

## 3.8 `frontend/`——单页应用

React 19 + TypeScript + Vite + Tailwind + Zustand（状态）+ react-router-dom。
`src/hooks/useWebSocket.ts` 实现了 JSON-RPC 客户端：带 token 认证、订阅管理、
按 `id` 关联请求与响应、以及频道通知分发（带 `channel_name` + `notification` 且没有 `id` 的消息是推送）。
`src/workers/marketDataWorker.ts` 和 `timerWorker.ts` 把订单簿处理和定时器移出主线程；
`utils/hftOptimizations.ts` 放热路径辅助函数；`components/OrderBook.tsx` +
`VirtualizedList.tsx` 渲染订单簿。

**要注意什么：** 前端消费的是*增量*，所以它必须自己维护订单簿状态，并在丢增量时靠周期性快照恢复
——和网关用的是同一个模式。因此状态对账是一个端到端的关注点，而不是后端的内部细节。

---

# 第四部分 数据模型

数据库：PostgreSQL 16（用的 TimescaleDB 镜像，`docker-compose.yml:5`）。六张表。

```
users ──┬── refresh_tokens        (1:N，哈希存储，可吊销)
        ├── balances              (每个币种 1:N；UNIQUE(user_id, asset))
        │                          available / locked，均 >= 0
        │                          （只有 available 会被写入）
        ├── ledger                (1:N；只追加，由数据库触发器强制)
        ├── orders                (1:N)
        ├── trades (buyer/seller) (1:N，可空——匿名的机器人一方)
        └── faucet_claims         (1:N，限流用)

orders ──┬── trades.buy_order_id  (可空)
         └── trades.sell_order_id (可空)

trades   ── exchange_fill_id      (UNIQUE，非部分索引 → 幂等结算)

ohlcv    ── 独立时间序列，PK (symbol, interval, open_time)
```

| 表 | 用途 | 关键列 | 备注 |
|---|---|---|---|
| `users` | 身份 | `id UUID PK`、`email UNIQUE` | 没有密码列——设计上就是无密码登录 |
| `otps` | 登录验证码 | `email, code, expires_at, used` | 短期有效 |
| `refresh_tokens` | 会话 | `user_id, token_hash UNIQUE, expires_at, revoked_at` | 存哈希，绝不存明文 |
| `balances` | 当前头寸 | `user_id, asset, available, locked` | **只有 `available`**——`locked` 没有任何代码写入（残留列，仅靠 CHECK 存活） |
| `ledger` | **记录流水** | `user_id, asset, amount, balance_after, entry_type, reference_id` | 单边记账、触发器保证不可变；没有系统账户，没有手续费行 |
| `orders` | 订单生命周期 | `side ∈ {bid,ask}`、`order_type ∈ {limit,market}`、`status ∈ {pending, open, partially_filled, filled, cancelled, rejected, expired}`、`filled_quantity`、`lock_entry_id` | `open`/`rejected`/`expired` 声明了但从未设置；`lock_entry_id` 指向账本锁仓记录 |
| `trades` | 已执行成交 | `buy/sell_order_id`、`buyer/seller_id`、`price, quantity`、`buyer_fee, seller_fee`、`exchange_fill_id`、`settled_at` | 真正动钱的记录 |
| `faucet_claims` | 测试网领水 | `user_id, asset, amount, claimed_at` | |
| `ohlcv` | K 线 | PK `(symbol, interval, open_time)`、OHLC + `volume, trade_count` | 由 `market_data` 和 `accounts` 两处写入 |

**金额处理约定：** 数据库里是 `DECIMAL(20, 8)`，Rust 里是 `rust_decimal::Decimal`，
`f64` 只出现在 UDP 线格式的行情数据上。余额绝不用 `float`。精度按币种区分：
`max_decimals_for_asset` 允许 `EUR`/`USD`/`GBP` 用 2 位小数，其他币种 8 位；
`validate_precision` 拒绝超出精度的输入，写入锁仓、计价金额和手续费之前都会先过
`round_to_precision`（`round_dp`）。API 响应中的金额**永远以字符串**返回，绝不作为 JSON 数字。

**schema 声明了但没有强制执行的约束：** 没有任何地方检查某个 symbol 里的币种是否存在
（币种是自由格式的字符串校验，symbol 用 `split('/')` 解析），也没有任何地方检查账本跨用户是否平衡。
这些只是应用层约定。

---

# 第五部分 端到端链路

用调试器或按列出的文件跟着走。这一步是架构从"一张图"变成"一串你能在设计评审里辩护的时序"的地方。

## Trace A —— 下一笔部分成交的限价单

| # | 位置 | 发生了什么 |
|---|---|---|
| 1 | 浏览器 `useWebSocket.ts` | 发送 `{id, method:"private/place_order", params:{symbol, side, order_type, price, quantity}}` |
| 2 | `gateway/src/websocket.rs:718` | 处理器检查 `ChannelManager::get_user_id(client_id)`——没有就返回错误码 `10000` |
| 3 | `websocket.rs:1033` `create_order_in_accounts` | `POST {accounts}/internal/orders`，带 `X-User-Id` 头。**accounts 校验并锁定资金** |
| 4 | `accounts/src/models/order.rs:171` `Order::place` | 插入 `orders` 行（`status=pending`），通过 `LedgerEntry::append_in_tx` 追加一条负的 `lock` 账本记录，并把 `lock_entry_id` 写到订单上。这条记录减少 `balances.available`（在按 `(user, asset)` 的咨询锁保护下），所以余额不足会在这里失败 |
| 5 | `websocket.rs:778` | `ChannelManager::register_order(order_id, user_id)`——记录归属，供之后路由私有事件 |
| 6 | `websocket.rs:800-832` | 构造 `OrderCommand::PlaceOrder` 并调 `UdpOrderSender::send_order_command` → **UDP 分帧里装 JSON 载荷**发到撮合 `:9100`。立即返回 `{result:{order_id}}`——*此时撮合还没发生* |
| 7 | 撮合 `main.rs:205` | `order_rx.recv()` → `process_order_command` |
| 8 | 撮合 `main.rs:245-252` | `orderbook.write().await`、`add_limit_order(...)`、释放锁 |
| 9 | `matching_engine/src/lib.rs:122` | `match_order` 从最优价开始遍历对手方订单簿，档位内 FIFO，产出 `Fill`，成交价为**挂单方的价格** |
| 10 | 撮合 `main.rs:262-296` | 对每笔成交：`POST {accounts}/internal/settle`。遇到 5xx/网络错误按指数退避重试 3 次。`ALREADY_SETTLED` → 视为成功（幂等） |
| 11 | `accounts/src/models/trade.rs:92` `Trade::settle` | 一个事务内：begin → 按排序后的用户顺序加咨询锁 → 对每个存在真实订单的一方，追加 `unlock` 和 `trade` 账本记录（在 `available` 余额之间转移计价币/基础币）→ `Order::add_fill`（`pending → partially_filled/filled`）→ `INSERT INTO trades ... ON CONFLICT (exchange_fill_id)`。订单 ID 查不到就意味着那一方是匿名机器人，会被**整体跳过**。手续费写进 trade 行，但从不写进账本 |
| 12 | 撮合 `main.rs:299-309` | 到这时才通过 UDP 以 FlatBuffers 向网关 `:9101` 发布 `Fill` 事件 |
| 13 | 撮合 `main.rs:316-333` | 如果市价单未完全成交：`POST /internal/cancel`（释放剩余资金）+ 发布 `OrderCancelled` |
| 14 | 撮合 `main.rs:350-356` | 对每个达到 100% 成交的订单发布 `OrderFilled` |
| 15 | 网关 `server.rs:169-176` | UDP 接收器 → `GatewayState::publish_event` → 广播总线 |
| 16 | 网关 `server.rs:186-282` | 广播器把事件折叠进 `OrderBookState`，向订阅者发出 `book.*` / `ticker.*` / `lwt.*` 通知 |
| 17 | 网关 `websocket.rs:455-476` | 每连接的任务把 `OrderFilled`/`OrderCancelled` **只**路由给订单所有者 |
| 18 | 浏览器 | 渲染成交、更新订单簿，并在下次轮询/刷新时更新余额 |
| 19 | 撮合 `main.rs:166-176` | 与此同时，发布器每 100 毫秒独立发出前 10 档 `OrderBookSnapshot`（每第 10 次 tick）或 `OrderBookDelta` |

**值得争论的点：** 客户端在第 6 步就被告知"已受理"，早于第 8 步的撮合——
所以 `order_id` 是一个*意图*句柄，不是确认。成交只有在第 12 步 HTTP 结算之后才发布，
这让热路径的延迟包含了数据库事务。而如果第 11 步永久失败，那一笔成交的发布会被跳过，
但订单簿在第 8 步已经动过了——见 7.2。

## Trace B —— 撤单

1. `websocket.rs:850` `private/cancel_order` → `cancel_order_in_accounts`：
   带 `X-User-Id` 调 `DELETE {accounts}/internal/orders/{id}`。
2. accounts `Order::cancel_internal` → 标记 `cancelled`，为未成交的剩余部分追加一条 `unlock`
   账本记录（把 `balances.available` 加回去）。
3. 网关通过 UDP 发送 `OrderCommand::CancelOrder`。
4. 撮合 `main.rs:365-379` → `orderbook.cancel_order(id)`；如果返回 true（订单确实挂在簿上），
   发布 `OrderCancelled`。

**注意这里的两处记账：** 订单是先在 accounts 被撤销的。如果那条 UDP 撤单丢了，
订单簿会留着一条幽灵订单，而它的资金已经被释放了。网关不会发布任何能纠正它的东西。
这是"有损传输 + 两个写入者"的具体后果。

## Trace C —— 订单簿如何到达浏览器

`撮合发布器（100ms tick）` → UDP FlatBuffers `OrderBookDelta`/`Snapshot` → 网关 UDP 接收器
→ `GatewayState::publish_event` → `start_event_broadcaster` → `OrderBookState::apply_orderbook_update`
→ `ChannelNotification` JSON → `ChannelManager::get_subscribers("book.KCN/EUR")` → 每个客户端的
`broadcast::Sender<String>` → WebSocket 上的 `Message::Text`。

给学习者的一致性检查：`OrderBookState` 是网关的*副本*；撮合引擎的 `OrderBook` 是*权威*；
浏览器里的状态是*副本的副本*。快照会同时重置这三者。丢增量之所以还能承受，只是因为快照会周期性重来。

## Trace D —— 一根 K 线是怎么出现的（有两个写入者）

**路径 1 —— `accounts`，在结算时同步写：** `POST /internal/settle` → `Trade::settle` 成功
→ `OHLCV::update_from_trade`（`internal.rs:138`）在同一个数据库里写入/更新 K 线。
这里失败只记 `warn!`。

**路径 2 —— `market_data`，从行情流异步写：** `Fill` 事件 → 网关把它作为成交通知广播
→ `market_data`（网关的 WS *客户端*）收到 → `OhlcvAggregator` 按周期分桶
→ `INSERT ... ON CONFLICT (symbol, interval, open_time)` 写进同一张 `ohlcv` 表。

两条路径 upsert 同一批行。这是重复劳动，也带来一个值得追问的一致性问题：
两者不一致时谁说了算？`market_data` 宕机而 `accounts` 正常（或反过来）时，那根 K 线会怎样？

对外提供数据还有第三个弯折：网关上的 `GET /api/ohlcv` 被路由到 **`proxy_accounts`**，
而不是 `market_data`（`server.rs:137-141`）。`proxy_market_data` 被 import 了，
`MARKET_DATA_URL` 在启动时也读了，但那个函数**从未挂到任何路由上**——
`market_data` 自己在 `:3002` 暴露了 `GET /api/ohlcv`，网关却不用它。这是失效的接线，验证它是个好练习。

## Trace E —— 登录

`POST /auth/request-otp {email}` → accounts 生成 6 位验证码**但不创建用户**，
写入一行 `otps`（10 分钟过期），发邮件（开发环境用 console provider，生产用 SMTP——
`accounts/src/mail/`）→ `POST /auth/signup`（新邮箱）或 `POST /auth/verify-otp`（已存在邮箱）
原子地消费验证码——`UPDATE otps SET used = TRUE WHERE email = $1 AND code = $2 AND
used = FALSE AND expires_at > NOW() RETURNING *`——然后签发 15 分钟的 **access JWT**
和 30 天的 **refresh token**（只存 SHA-256 哈希），把 refresh token 设为 httpOnly cookie，
access token 放在 JSON body 里返回 → 浏览器把 access token 存在内存
（`stores/authStore.ts`）并调用 `WS public/auth {token}`，让网关为这个连接记住用户。
`signup` 与 `verify-otp` 返回*同一个*笼统错误 `"Invalid email or code"`，
以避免泄漏哪些邮箱已注册。

---

# 第六部分 端口、环境变量与拓扑

```
                    宿主机端口            容器内
frontend            5173    ──────────►  :80 (nginx)
gateway             3000    ──────────►  :3000   TCP  HTTP + WS
                    9101/udp ──────────►  :9101   UDP  收事件  ◄── 来自撮合 :9103
                                       ( :9102 )  UDP  发下单 ──► 撮合 :9100
matching-engine-svc 8080    ──────────►  :8080   TCP  仅健康检查
                    9100/udp ──────────►  :9100   UDP  收下单
                                       ( :9103 )  UDP  发事件 ──► 网关 :9101
accounts            3001    ──────────►  :3001   TCP  HTTP
market-data         3002    ──────────►  :3002   TCP  HTTP
postgres            5433    ──────────►  :5432
```

决定接线的环境变量：

| 变量 | 服务 | 含义 |
|---|---|---|
| `BIND_ADDR` | gateway / accounts / market_data / 撮合 | HTTP 监听地址 |
| `SYMBOL` | 撮合 | 唯一的交易对 |
| `ORDER_RECEIVER_BIND` | 撮合 | 接收下单的 UDP 地址（`:9100`） |
| `GATEWAY_EVENT_ADDR` | 撮合 | 事件发往哪里（`gateway:9101`） |
| `EVENT_SENDER_BIND` | 撮合 | 事件发送端的本地 UDP 绑定（`:9103`） |
| `MATCHING_ENGINE_UDP_ADDR` | gateway | 下单发往哪里（`matching-engine-service:9100`） |
| `ORDER_SENDER_BIND` | gateway | 下单发送端的本地 UDP 绑定（`:9102`） |
| `EVENT_RECEIVER_BIND` | gateway | 接收事件的 UDP 地址（`:9101`） |
| `ACCOUNTS_URL` | gateway、撮合 | accounts 基础 URL |
| `MARKET_DATA_URL` | gateway | market_data 基础 URL（**目前无效**，见 Trace D） |
| `DATABASE_URL` | accounts、market_data | PostgreSQL DSN |
| `JWT_SECRET` | accounts | 签名密钥 |
| `ENVIRONMENT` | accounts | `development` 时启用 `/auth/dev-login` |
| `MAIL_PROVIDER` | accounts | `console` 或 `smtp` |
| `CORS_ALLOWED_ORIGINS` | gateway、accounts | 逗号分隔的白名单（credentials 模式） |
| `ENABLE_DB_CLEANUP` | accounts | 开启危险的数据清理任务（默认关闭，见 7.2） |
| `ENABLE_BOT_LOGIN` | accounts | 额外允许 `/auth/dev-login` 供机器人使用 |

CI（`.github/workflows/ci.yml`）：对 `matching_engine` 和 `accounts` 跑 `cargo test`，
对 `gateway` 和 `matching_engine_service` 跑 `cargo check`，外加前端 lint / 类型检查 / 构建。
部署（`.github/workflows/deploy.yml`）：release 构建、前端构建、部署到 EC2。

---

# 第七部分 如何评判这套架构

到这一步你已经可以有自己的判断了。下面是一个可以拿来反驳的起始立场。

## 7.1 确实做得好的地方

* **纯内核被正确地隔离了。** `matching_engine` 零 I/O、零 async，因此不需要任何基础设施就能
  单元测试（同文件内 30 个测试），而 crate 边界让"引擎完全不懂钱"成为事实而不是承诺。
* **金额从不碰 `f64`。** 账务全程 `Decimal`；`f64` 只出现在线上行情数据里，且转换被隔离在一个边界上。
* **先结算后发布**给了客户端一个有意义的保证：你能看见的事件，就是已经进了账本的事件。
* **幂等性是被认真考虑过的：** `exchange_fill_id` 唯一索引、把 `ALREADY_SETTLED` 当成功、
  以及 migration `006` 修掉了部分索引与 `ON CONFLICT` 不兼容的问题。
  （剩下的竞态见 7.2——但*意图*和*机制*都在，这已经比大多数系统强。）
* **账本在数据库层面不可变**，余额原则上可以独立推导并对账（`derive_balance`、`reconcile`）。
* **并发是被显式处理的：** 按 `(user, asset)` 加咨询锁，并按确定性的排序顺序获取——
  这是一个刻意的、考虑过死锁的设计，而不是一把全局互斥锁。
* **可观测性建在协议层里**——序号、gap 计数器、流状态、发送/接收统计。

## 7.2 确实有风险的地方（附证据）

1. **结算失败时没有任何补偿。** `main.rs:283-295` 打印
   `INCONSISTENCY ... Manual intervention required` 然后继续。订单簿已经动了，账本没有。
   `restore_order` 从未被调用。可选方案：补偿型 Saga、outbox/事件日志，
   或者让订单簿本身可以从账本推导出来。*（这也是个很好的面试题。）*
2. **两个写入者，没有对账循环。** accounts 持有持久化的订单状态，撮合引擎持有实时订单簿。
   一条丢失的 UDP 下单或撤单会让二者悄悄分叉。系统里没有周期性对账任务。
   README 把"没有事件回放"列为已知限制——这是它最尖锐的后果。
3. **有损的热路径，恢复故事很弱。** 增量被静默丢弃；只有周期性快照（每 10 次发布 tick）
   能修复网关和浏览器。`sequence` 被传出去了，但消费端没有用它做 gap 检测。
4. **进程内、无鉴权的内部 API 通过边缘暴露。** accounts 的 `/internal/*` 信任 `X-User-Id`
   且没有鉴权（`internal.rs`），而网关公开代理了 `/internal/*path`（`server.rs:134`）。
   任何能访问网关 origin 的人都能以任意用户身份下单，并调用 `/internal/mint`。
   应该把内部 API 绑到独立监听/独立网络，或者加上服务间认证（mTLS、签名服务 token）。
5. **每个进程一个 `OrderBook`，一个硬编码交易对。** 只能垂直扩展；没有按交易对分片、
   没有备机、没有故障切换。作为个人项目可以接受，作为生产系统不合格。
6. **`Arc<RwLock<ChannelManager>>`**（`server.rs:29`）在每次连接、认证、订阅、下单和断开时都会被触碰
   ——连接热路径上的一把全局锁。
7. **`ohlcv` 有两个写入者。** `accounts` 在结算路径里 upsert K 线，`market_data` 从 WebSocket 流里
   upsert K 线。谁都不是权威，谁都没有从 `trades` 回填的例程，
   而网关甚至访问不到 `market_data` 的接口（`proxy_market_data` 是死接线）。
   要么只留一个写入者，要么把这个投影做成显式可重建的。
8. **Migration 漂移。** `007_remove_exchange_order_id.sql` 存在，但不在 `run_migrations` 数组里
   （`db/mod.rs`）。任何对 migration 的改动都必须去那里核对。
9. **结算幂等性可能重复记账。** `fill_id` 预检查是在事务*外*发出的 `SELECT`
   （`trade.rs:97-101`），而 `ON CONFLICT` 保护在账本写入和 `add_fill` *之后*才执行。
   一个并发重复请求即使输掉插入竞争，它的余额变更仍然会被提交。
   唯一索引保护的是 `trades` 行，不是钱。
10. **手续费算了但没收。** `FEE_RATE = dec!(0.001)` 被存到 trade 行上；
    `accounts/src/` 从不往账本写 `EntryType::Fee`。交易所没有收入账户。
11. **没有资金守恒。** 当一方是匿名（每一笔机器人成交都是），那一方被跳过，
    于是这笔交易创造了基础币、销毁了计价币。没有系统账户，也没有任何对账任务来保证
    账本在所有用户之间净额为零。`reconcile` 存在于 `ledger.rs`，但没有被任何地方调用。
12. **开启 `ENABLE_DB_CLEANUP` 是危险的。** `scheduler.rs` 用 `.ok()`（吞掉错误）关掉
    `ledger_immutable` 触发器，然后删除账本行；它的 `trades` 删除引用了一个不存在的列
    （`created_at` vs `settled_at`），于是报错提前返回——可能把触发器留在关闭状态。
    一个配置开关，能悄悄废掉 schema 里最强的不变量。
13. **`balances.locked` 是死账务。** 没有任何代码路径写它，但它带着 CHECK 约束，
    还被 API 返回。任何读 schema 的人都会对"资金如何被预留"得出错误结论。
14. **死接线 / 过期产物。** `proxy_market_data` 和 `MARKET_DATA_URL` 配置了但从未挂路由
    （`server.rs`）；`OrderAccepted` 在发送前被过滤，因为它不在 FlatBuffers union 里；
    而 `matching_engine/benches/matching_engine_bench.rs` 调用
    `add_limit_order(side, price, quantity)`——3 个参数对 4 个参数的签名——所以 `cargo bench`
    根本编译不过。`matching_engine/README.md` 同样过期，还在声称 `OrderId` 是 `u64`。
    **把 README 里关于引擎的说法当作未经证实的。**

## 7.3 相对 README 目标架构还缺什么

| 缺失的部分 | 为什么重要 | 最小可做的第一步 |
|---|---|---|
| **事件日志 / 回放** | 支撑恢复、审计、以及重建 `market_data` | 发布前把每个 `Fill`/订单簿事件持久化到只追加表或类 Kafka 的日志 |
| **风控引擎** | 盘前保证金、持仓限额、胖手指校验 | 目前只有余额检查，且在 accounts 下单时做 |
| **独立结算服务** | 今天结算是撮合热路径里的同步 HTTP 调用 | 自持事务日志 + 异步确认 |
| **冗余 / 故障切换** | 到处是单点 | 用订单日志的确定性回放支撑热备撮合引擎 |
| **可观测性** | 只有日志，没有指标/链路追踪 | `udp_proto` 里已经有 Prometheus 风格的计数器（`SenderStats`）——导出来 |
| **交易对注册中心** | 只有一个硬编码交易对 | 一张 markets 表 + 网关里的 symbol 路由 |
| **K 线回填** | 从宕机中恢复 | 用 `trades` 表重建 K 线 |

---

# 第八部分 建议的阅读顺序

五个 session，每个结尾都有检查点——能回答就往下走。

### Session 1 —— 地图（60 分钟）

1. `README.md`（整篇，包括第 101 行开始的限制章节）
2. `docker-compose.yml`——运行时拓扑、端口和环境变量接线
3. 本指南第一部分和第六部分
4. `grep -rn "pub enum" --include=*.rs .`，把每个枚举的变体读一遍

**检查点：** 凭记忆画出 1.2 节的图，并给每条箭头标注协议 + 端口。

### Session 2 —— 内核（90 分钟）

1. `matching_engine/src/lib.rs` 第 1–240 行（类型、数据结构、`add_limit_order`）
2. `matching_engine/examples/` 里的五个文件——它们是全仓库最易读的代码
3. `matching_engine/src/lib.rs` 第 240–362 行（`match_order`、`add_order_to_book`）

**检查点：** 口头解释为什么 `BTreeMap` + `VecDeque` + `HashMap` 三者合起来能给出价格-时间优先，
同时插入 O(log n)、撤单 O(1)。然后运行
`cargo test --manifest-path matching_engine/Cargo.toml`。

### Session 3 —— 热路径（120 分钟）

1. `udp_proto/src/market_data.fbs`（契约）——读 `.fbs`，别读生成文件
2. `udp_proto/src/protocol.rs`（分帧）
3. `matching_engine_service/src/main.rs:223-383`（`process_order_command`），
   然后 `389-548`（`OrderBookPublisher`）
4. `matching_engine_service/src/settlement.rs`

**检查点：** 写下（撮合、结算、发布）的准确顺序，并说明如果第 N 步失败会造成什么不一致。
然后运行 `cargo test --manifest-path udp_proto/Cargo.toml`。

### Session 4 —— 边缘与资金（120 分钟）

1. `gateway/src/server.rs`（`router`、`start_event_broadcaster`）
2. `gateway/src/websocket.rs:310-560`（`ChannelManager`、`handle_websocket_connection`），
   然后 `718-950`（下单/撤单处理器）
3. `accounts/src/main.rs`（组装 + 认证中间件）
4. `accounts/migrations/001..006`——读 SQL；它是数据模型最清晰的表述
5. `accounts/src/models/order.rs:171` 和 `models/trade.rs:92`（下单 + 结算）

**检查点：** 追一笔 100 欧元的买单，从浏览器到账本记录，说出每一个函数边界。

### Session 5 —— 判断（90 分钟）

1. `gateway/src/channel_updates.rs`（读模型 / CQRS 投影）
2. `market_data/src/main.rs` + `ohlcv.rs`
3. `trading_bot/src/gateway_client.rs`（非人类客户端如何用同一套 API）
4. 重读第七部分，写出你自己版本的"我会改的前三件事"，并附证据

**检查点：** 用一个段落捍卫或攻击"结算应该留在热路径上"。

---

# 第九部分 动手练习

按难度排序，全部安全、全部在本地。

1. **读 examples。** `matching_engine/examples/*.rs` 每个约 25 行，
   覆盖了整个领域：构建订单簿、跨越价差、市价单、部分成交、撤单。
2. **跑内核测试。** `cargo test --manifest-path matching_engine/Cargo.toml`——
   首次构建会下载依赖，需要几分钟。
3. **跑协议测试。** `cargo test --manifest-path udp_proto/Cargo.toml`——624 行测试，
   覆盖分帧、gap、心跳和 FlatBuffers 往返。
4. **试跑基准测试——然后把它修好。** `cargo bench --manifest-path matching_engine/Cargo.toml`
   目前**编译不过**：`benches/matching_engine_bench.rs` 调用
   `add_limit_order(side, price, quantity)`（3 个参数），而真实签名是
   `add_limit_order(order_id, side, price, quantity)`（4 个参数）。
   补一个 `OrderId`（`uuid::Uuid::new_v4()`——`Uuid` 被 crate 重新导出了）让它编译通过。
   这是个很有价值的练习：它逼你读公开 API，也让你明白——
   **一棵树里的基准测试在它能编译能运行之前，什么都证明不了。**
5. **把它跑起来。** `docker-compose up --build`，然后打开 `http://localhost:5173`。
   盯日志：你会实实在在看到撮合引擎里的 `Sending Fill event via UDP` 和网关里的
   `Received Fill event via UDP`——Trace A 的实时版。
6. **故意弄坏它。** 在 `matching_engine_service/src/main.rs` 里把 `SNAPSHOT_INTERVAL`
   设成一个极大的数，观察增量丢失后浏览器里的订单簿如何漂移；再设回 1，看快照如何把它修回来。
   这会让"丢包 vs 恢复"的取舍变得可感知。
7. **追一次契约变更。** 在 `udp_proto/src/market_data.fbs` 的 `Fill` 里加一个字段，重新生成，
   然后顺着编译器错误一路走过 `binary.rs`、`matching_engine_service/src/udp_transport.rs`
   和 `gateway/src/udp_transport.rs`。这会让你体会在一条 5 跳管道里改 schema 的成本。
8. **修一个真实的不一致（设计练习，不用写代码）。** 写一页设计，让撮合引擎变得可恢复：
   订单事件日志、确定性回放、以及针对 `orders` 的对账任务。和 README 里
   "No event replay" 的限制对照一下。
9. **关掉信任边界。** 在网关和 accounts 之间的 `/internal/*` 上加一个共享密钥头（或 mTLS），
   并停止从公开路由代理 `/internal/*path`。记录哪些测试会挂。
10. **练习"验证论断"。** 本指南里每条架构论断都可以用 grep 验证。挑三条让你意外的——
    比如"`locked` 列从未被写入"、"手续费从未记入账本"、"`restore_order` 没有调用者"——
    自己去推翻或确认：

    ```bash
    grep -rn "locked *=" accounts/src --include=*.rs        # 预期：没有任何写入
    grep -rn "EntryType::Fee" accounts/src --include=*.rs   # 预期：只有 Display 实现
    grep -rn "restore_order" . --include=*.rs               # 预期：只有 matching_engine/src/lib.rs
    ```

    这个习惯，就是"读懂一个代码库"和"相信它的文档"之间的分界线——包括本文档，
    以及仓库自己的 `README.md`。
11. **补上手续费的洞（小改动、高价值）。** 让 `Trade::settle` 把买方和卖方的手续费
    作为 `EntryType::Fee` 账本记录记到一个系统账户上，使账本净额为零。
    然后解释：只要机器人那一方仍被跳过，为什么这个改动本身还不足以让账务变成复式记账。

---

# 第十部分 术语表

| 术语 | 在本仓库中的含义 |
|---|---|
| **Ask** | 订单簿的卖方 |
| **Bid** | 订单簿的买方 |
| **订单簿 Book** | 挂着的限价单，先按价格、再按到达时间组织 |
| **Maker / Taker** | 挂单方 vs 吃单的主动方 |
| **Crossing 交叉** | 新来的订单价格与对手方重叠，从而产生成交 |
| **Crate** | Rust 的编译单元/包（≈ npm package） |
| **Delta 增量** | 订单簿的增量变化（`Add`/`Update`/`Remove`），相对于快照而言 |
| **Fill** | 撮合引擎产生的撮合结果——*尚未*是结算后的成交 |
| **Trade** | 已结算的成交：钱动了，账本写了 |
| **FlatBuffers** | 支持零拷贝读取的二进制序列化；schema 在 `.fbs` |
| **Lock 锁仓** | 为挂单预留余额（实现方式是减少 `available`） |
| **Ledger 账本** | 所有余额变动的只追加流水 |
| **OHLCV** | 开/高/低/收/成交量 K 线 |
| **Settlement 结算** | 为成交转移资金：`POST /internal/settle` |
| **Snapshot 快照** | 订单簿全量状态，用于让副本重新同步 |
| **Stream / stream_id** | UDP 上一条逻辑有序的消息流 |
| **`Arc<T>`** | 共享的引用计数所有权 |
| **`RwLock<T>`** | 多读*或*一写 |
| **`tokio`** | 异步运行时；`tokio::spawn` 启动一个并发任务 |
| **`axum`** | 所有服务使用的 HTTP/WebSocket 框架 |
| **`sqlx`** | 异步 PostgreSQL 客户端（查询在构建期对着真实 schema 校验） |
| **`serde`** | 基于 derive 的 JSON 序列化/反序列化 |

---

# 第十一部分 自测题

不查资料回答。如果全部 16 题都能答，你就理解了这套架构。

1. 哪两个组件持有订单簿状态，其中哪个是权威？
2. 为什么结算是 HTTP 调用而不是一条 UDP 消息？
3. 究竟是什么保证了客户端永远不会看到一笔尚未进账本的成交？
4. 资金在哪里被锁定、在哪里被转移、在哪里被解锁？
5. `exchange_fill_id` 是干什么的，为什么它需要*非部分*唯一索引？
6. 说出 `OrderBook` 内部的三个数据结构，以及各自承担的工作。
7. 如果网关那条 UDP 撤单包在 accounts 已经撤单之后丢了，会发生什么？
8. 网关的订单簿副本如何从丢失的增量中恢复？
9. 哪张表是余额的事实来源，它靠什么防止被修改？
10. 为什么 `OrderAccepted` 从不通过 UDP 传输？
11. 哪个服务是 WebSocket *客户端*而不是服务端，还有谁在写它写的那张表？
12. 公网与内部服务之间的信任边界在哪里，它目前在哪里被破坏？
13. `balances` 里哪一列从未被写入？当一笔订单"锁定资金"时，实际发生了什么？
14. 交易手续费被收了吗？它被记录在哪里，又*没有*被记录在哪里？
15. 为什么 `ON CONFLICT (exchange_fill_id)` 不足以让结算具备幂等性？
16. 哪个开关一旦打开，可能悄悄禁用一个数据库级不变量？是怎么发生的？

---

## 附录——去哪儿找什么

| 你想了解… | 去读 |
|---|---|
| 线上契约 | `udp_proto/src/market_data.fbs` |
| 分帧 / MTU / 心跳 | `udp_proto/src/protocol.rs` |
| 丢包检测 | `udp_proto/src/receiver.rs`（`StreamState`、`GapInfo`、`ReceiverStats`） |
| 撮合算法 | `matching_engine/src/lib.rs:234+`（`match_order`） |
| 领域类型 | `matching_engine/src/lib.rs:1-102` |
| 下单 → 撮合 → 结算 → 发布 | `matching_engine_service/src/main.rs:223-383` |
| 快照/增量发布 | `matching_engine_service/src/main.rs:389-548` |
| 结算重试与错误码 | `matching_engine_service/src/settlement.rs:93-234` |
| 客户端协议 / 订阅 | `gateway/src/websocket.rs` |
| 事件扇出 | `gateway/src/server.rs:181-283` |
| 网关订单簿副本 | `gateway/src/channel_updates.rs` |
| HTTP 反向代理 | `gateway/src/proxy.rs` |
| 认证 + 余额 + 订单 API | `accounts/src/main.rs`、`accounts/src/routes/*` |
| 资金变动 | `accounts/src/models/ledger.rs`、`models/trade.rs`、`models/order.rs` |
| 数据库 schema | `accounts/migrations/*.sql` |
| K 线聚合 | `market_data/src/ohlcv.rs` |
| 机器人如何使用公开 API | `trading_bot/src/gateway_client.rs` |
| 客户端侧增量处理 | `frontend/src/hooks/useWebSocket.ts`、`workers/marketDataWorker.ts` |
| 已知限制 | `README.md:101-113` |
| CI 期望 | `.github/workflows/ci.yml` |
