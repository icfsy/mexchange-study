# Attribution / 来源说明

## English

### What this is

This repository is a **derivative** of **[mExchange](https://github.com/afterburn/mExchange)**,
not the original project. The MIT [`LICENSE`](LICENSE) preserves both copyright notices.

**Original project**

- Name: mExchange — "A modular, high-performance cryptocurrency exchange platform built with Rust"
- Author: **Kevin Karsopawiro** <k.karsopawiro@gmail.com>
- Upstream: https://github.com/afterburn/mExchange
- Site: https://exchange.kevin.rs

### What came from where

| Path | Origin |
|---|---|
| `accounts/`, `gateway/`, `matching_engine/`, `matching_engine_service/`, `market_data/`, `trading_bot/`, `udp_proto/`, `frontend/`, `e2e-tests/`, `docs/screenshot.png`, `.github/`, `docker-compose.yml`, the root `README.md` (apart from the notice at the top) | **Original author's work** |
| `yyin/docs/` | Documentation added in this repository |
| `yyin/mexchange_py/` | Python reference model added in this repository |
| `LICENSE`, `NOTICE.md` | Added in this repository |

In terms of commit history: of the 43 commits present when this notice was written,
**40 are by Kevin Karsopawiro and 3 were added here**.

### What this repository adds (`yyin/`)

- **`yyin/docs/ARCHITECTURE_GUIDE.md`** and **`ARCHITECTURE_GUIDE.zh-CN.md`** — a guided walkthrough
  of the existing architecture and code, with findings that were individually verified against the
  source.
- **`yyin/docs/EXCHANGE_DOMAIN_NOTES.md`** — background on how exchanges generally work:
  matching and allocation algorithms (FIFO vs pro-rata vs LMM), order types and time-in-force,
  self-trade prevention, decimal vs fixed-point money, single-sided vs double-entry ledgers,
  audit and regulatory requirements, pre-trade risk, why market data and order flow use different
  transports, FIX session semantics, and clearing vs settlement. Includes external references.
- **`yyin/docs/ARCHITECTURE_V2.md`** — a target architecture for production use (event log, risk
  gateway, FIX gateway, asynchronous settlement, market-data gap recovery), with SQL sketches,
  flow sequences, per-boundary delivery semantics, a six-phase migration path, and explicit
  non-goals.
- **`yyin/mexchange_py/`** — a Python model of the **business logic**: 7 runnable scenarios and
  19 tests. It deliberately keeps only the business rules and drops the parts that exist purely for
  performance or concurrency (UDP, FlatBuffers, async, locks). It documents where it intentionally
  diverges from the original (double-entry ledger, fees actually posted, explicit reservations).

### Disclaimer

- This repository **does not represent** the original author. He did not participate in, endorse,
  or maintain it.
- For the upstream project's development, issues, and roadmap, use
  [afterburn/mExchange](https://github.com/afterburn/mExchange).
- The documentation here contains critical analysis of the code (for example the settlement
  idempotency race, the unauthenticated internal API, and a cleanup job that can disable the ledger
  immutability trigger). These are technical judgements from reading the source, not criticism of
  the author; each one cites a location you can check.

---

## 中文

### 这是什么

本仓库是 **[mExchange](https://github.com/afterburn/mExchange)** 的**衍生（derivative）仓库**，
不是原始项目。MIT [`LICENSE`](LICENSE) 同时保留了原作者与本仓库新增部分的版权声明。

**原始项目**：作者 **Kevin Karsopawiro** <k.karsopawiro@gmail.com>，
上游 https://github.com/afterburn/mExchange。

### 内容构成

| 路径 | 来源 |
|---|---|
| `accounts/`、`gateway/`、`matching_engine/`、`matching_engine_service/`、`market_data/`、`trading_bot/`、`udp_proto/`、`frontend/`、`e2e-tests/`、`docs/screenshot.png`、`.github/`、`docker-compose.yml`、根 `README.md`（除顶部说明外） | **原作者成果** |
| `yyin/docs/` | 本仓库新增的文档 |
| `yyin/mexchange_py/` | 本仓库新增的 Python 参考模型 |
| `LICENSE`、`NOTICE.md` | 本仓库新增 |

就提交历史而言：截至本说明撰写时，**43 个提交中 40 个来自 Kevin Karsopawiro，3 个为本仓库新增**。

### 本仓库新增了什么（`yyin/`）

- **架构导读**（英文 + 中文）：v1 现状与代码导读，含逐条核查过的结论。
- **交易所业务知识笔记**：撮合与分配算法、订单类型与 TIF、自成交防护、金额精度与记账、
  可审计与合规、交易前风控、行情与订单流的通道分离、FIX、清算与结算，附外部资料链接。
- **v2 目标架构**：事件日志、风控网关、FIX 网关、异步结算、行情缺口恢复；含 SQL 草图、
  流程时序、各边界交付语义、六阶段迁移路线与刻意的非目标。
- **Python 业务逻辑参考模型**：7 个可执行场景 + 19 个测试。刻意只保留业务规则，
  去掉仅服务于性能与并发的部分；并明确标注了哪些地方与上游做法不同（复式记账、手续费入账、显式预留）。

### 免责

- 本仓库**不代表**原作者立场；原作者**未**参与、**未**背书、**未**维护本仓库。
- 上游项目的开发、问题反馈与路线图请以
  [afterburn/mExchange](https://github.com/afterburn/mExchange) 为准。
- 文档中包含对代码的批评性分析（如结算幂等竞态、内部接口未鉴权、清理任务可能关闭账本不可变触发器等）。
  这些是阅读源码得出的技术判断，不是对原作者的指责；每条结论都注明了可核查位置。
