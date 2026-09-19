"""领域类型。

与 Rust 版 `matching_engine/src/lib.rs` 的公开类型一一对应，便于对照阅读。
差别只有一处，且是刻意的：**这里的 `Fill` 携带买卖双方的账户**。
Rust 版的 `Fill` 只有订单 ID，所以那个引擎在结构上无法做自成交防护（STP）——
见 `EXCHANGE_DOMAIN_NOTES.md` 第 3 节。这个模型把参与者身份补上了。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional


def new_id() -> str:
    """订单 ID 由上游（accounts）生成，撮合引擎自己不生成——与 Rust 版一致。"""
    return str(uuid.uuid4())


class Side(str, Enum):
    BID = "bid"
    ASK = "ask"

    @property
    def opposite(self) -> "Side":
        return Side.ASK if self is Side.BID else Side.BID


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class TimeInForce(str, Enum):
    """有效期。Rust 版 v1 **没有 TIF**——这是本模型超出 v1 的业务能力。

    注意：IOC 和 FOK 不是"参数差异"，而是撮合语义差异：
    FOK 要求引擎先预演"能否全额成交"，才能决定是否执行。
    """

    GTC = "gtc"  # Good Till Cancelled：未成交部分挂着
    IOC = "ioc"  # Immediate Or Cancel：立即成交，剩余立即撤销
    FOK = "fok"  # Fill Or Kill：要么全部成交，要么全部撤销


class OrderStatus(str, Enum):
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class Order:
    order_id: str
    account_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    price: Optional[Decimal] = None
    tif: TimeInForce = TimeInForce.GTC
    post_only: bool = False
    remaining: Optional[Decimal] = None
    status: OrderStatus = OrderStatus.OPEN
    seq: int = 0  # 进入订单簿的序号（用于日志与时间优先的可读性）

    def __post_init__(self) -> None:
        if self.remaining is None:
            self.remaining = self.quantity

    @property
    def filled_quantity(self) -> Decimal:
        return self.quantity - self.remaining

    @property
    def is_filled(self) -> bool:
        return self.remaining <= 0

    @property
    def is_market(self) -> bool:
        return self.order_type is OrderType.MARKET

    def __str__(self) -> str:
        px = "market" if self.price is None else str(self.price)
        return f"{self.side.value} {self.quantity} @ {px} [{self.order_id[:8]}]"


@dataclass(frozen=True)
class Fill:
    """一次撮合的结果。注意：**成交价取被动方（挂单方）的价格**，不是主动方的限价。"""

    symbol: str
    buy_order_id: str
    sell_order_id: str
    buyer_id: str
    seller_id: str
    price: Decimal
    quantity: Decimal
    timestamp: int

    @property
    def fill_id(self) -> str:
        """幂等键。与 Rust 版 `Trade::settle` 里的 fill_id 构造方式一致。"""
        return f"{self.buy_order_id}-{self.sell_order_id}-{self.timestamp}"

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


@dataclass
class OrderResult:
    order_id: str
    fills: list[Fill] = field(default_factory=list)
    completed_orders: list[str] = field(default_factory=list)
    rejected_reason: Optional[str] = None

    @property
    def rejected(self) -> bool:
        return self.rejected_reason is not None
