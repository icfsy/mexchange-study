"""mexchange —— mExchange 业务逻辑的 Python 参考模型。

**定位（重要）**：这是一个**业务逻辑模型**，不是 Rust 版的移植，也不追求性能。

它存在的理由是：Rust 版里"业务规则"和"性能/并发机制"混在一起，
很难看清哪部分是业务、哪部分是为了快。这个模型只保留业务规则，把
UDP、FlatBuffers、tokio、锁、零拷贝全部扔掉——因为它们与"业务逻辑是什么"无关。

它**不能**替代对架构的理解：单进程 Python 里，异步、失败模式、
一致性问题全都不会发生，而这些恰恰是 v1 一半设计的理由。
学架构请结合 `yyin/docs/` 下的三份文档。

模块对应关系：
    domain.py    ↔  matching_engine 的领域类型
    book.py      ↔  matching_engine 的撮合算法
    ledger.py    ↔  accounts 的账本（这里是复式记账，v1 是单边流水）
    accounts.py  ↔  accounts 的下单/结算/幂等
    venue.py     ↔  matching_engine_service 的编排（含"先结算后发布"）
"""

from .accounts import AccountsService, InsufficientFunds, MarketMeta
from .book import OrderBook
from .domain import (
    Fill,
    Order,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    new_id,
)
from .ledger import Account, AccountKind, JournalEntry, Ledger, UnbalancedTransaction, round_to
from .venue import Event, Venue

__all__ = [
    "AccountsService",
    "Account",
    "AccountKind",
    "Event",
    "Fill",
    "InsufficientFunds",
    "JournalEntry",
    "Ledger",
    "MarketMeta",
    "Order",
    "OrderBook",
    "OrderResult",
    "OrderStatus",
    "OrderType",
    "Side",
    "TimeInForce",
    "UnbalancedTransaction",
    "Venue",
    "new_id",
    "round_to",
]
