"""Venue：把撮合、结算、发布串起来——对应 Rust 版 `matching_engine_service`。

**这个文件的核心价值，是让你看清 v1 的"先结算、后发布"顺序**
（Rust 版 `main.rs:223-383` 的 `process_order_command`）：

    1. 在 accounts 里校验余额并锁定资金
    2. 撮合（撮合临界区，微秒级）
    3. 对每一笔成交：**先**同步调用 accounts 结算
    4. **然后**才发布 Fill 事件
    5. 市价单未成交部分 → 通知 accounts 撤单 + 发布撤销事件
    6. 发布完全成交事件

顺序（3 在 4 之前）不是随意的：它保证"客户端能看到的成交，一定是已经进账本的成交"。
代价是热路径被一次 HTTP + 数据库事务绑住。

**v2 会怎么改**：撮合只负责产出事件并写入事件日志，结算异步消费事件流。
详见 `ARCHITECTURE_V2.md` 第 6.1 节。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Optional

from .accounts import AccountsService
from .book import OrderBook
from .domain import Fill, Order, OrderStatus, OrderType, Side, TimeInForce
from .ledger import ZERO, fmt

# 追踪回调：actor 表示"谁在做这件事"，message 是做了什么
TraceFn = Callable[[str, str], None]


@dataclass
class Event:
    type: str
    payload: dict = field(default_factory=dict)

    def __str__(self) -> str:
        inner = ", ".join(f"{k}={v}" for k, v in self.payload.items())
        return f"{self.type}({inner})"


class Venue:
    def __init__(
        self,
        book: OrderBook,
        accounts: AccountsService,
        trace: Optional[TraceFn] = None,
    ) -> None:
        self.book = book
        self.accounts = accounts
        self.symbol = book.symbol
        self.events: list[Event] = []
        self._trace = trace
        # 已发布的订单簿视图，用于计算增量（对应 v1 的 OrderBookPublisher）
        self._published_bids: dict[Decimal, Decimal] = {}
        self._published_asks: dict[Decimal, Decimal] = {}
        self._sequence = 0

    # ------------------------------------------------------------------ 工具

    def _t(self, actor: str, message: str) -> None:
        if self._trace:
            self._trace(actor, message)

    def _publish(self, event: Event) -> None:
        self.events.append(event)

    # ------------------------------------------------------------------ 下单

    def place_order(
        self,
        account_id: str,
        side: Side,
        order_type: OrderType,
        quantity: Decimal,
        ts: int,
        price: Optional[Decimal] = None,
        tif: TimeInForce = TimeInForce.GTC,
        post_only: bool = False,
    ) -> tuple[Optional[Order], list[Fill]]:
        m = self.accounts.market
        px = "market" if price is None else str(price)
        self._t(
            "client",
            f"place_order {side.value} {order_type.value} qty={quantity} price={px} "
            f"tif={tif.value}{' post_only' if post_only else ''}  (account={account_id})",
        )

        # 步骤 1：校验 + 预留（v1 里由网关调用 accounts 的 /internal/orders 完成）
        order = self.accounts.place_order(
            account_id=account_id,
            side=side,
            order_type=order_type,
            quantity=quantity,
            ts=ts,
            price=price,
            tif=tif,
            post_only=post_only,
        )
        res = self.accounts.reservations[order.order_id]
        self._t(
            "accounts",
            f"预留 {fmt(m.round_price(res.per_unit * order.quantity))} {res.asset} "
            f"→ 可用 {res.asset}={fmt(self.accounts.available(account_id, res.asset))}",
        )
        self._publish(Event("order_accepted", {"order_id": order.order_id[:8], "side": side.value}))

        # 步骤 2：撮合
        if order_type is OrderType.LIMIT:
            result = self.book.add_limit_order(order, ts)
        else:
            result = self.book.add_market_order(order, ts)

        if result.rejected:
            self.accounts.cancel_order(order.order_id, ZERO, ts)
            self._t("engine", f"❌ 拒绝：{result.rejected_reason}（预留已释放）")
            self._publish(Event("order_rejected", {"reason": result.rejected_reason}))
            return None, []

        self._t(
            "engine",
            f"撮合完成：{len(result.fills)} 笔成交，状态={order.status.value}"
            + (f"，挂单 @ {order.price}" if order.status is OrderStatus.OPEN else ""),
        )

        # 步骤 3 + 4：先结算，再发布（顺序就是重点）
        for fill in result.fills:
            txn = self.accounts.settle_fill(fill, ts)
            self._t(
                "accounts",
                f"结算成交 {fmt(fill.quantity)} {m.base_asset} @ {fmt(fill.price)} {m.quote_asset} → {txn}",
            )
            self._publish(
                Event(
                    "fill",
                    {
                        "price": str(fill.price),
                        "qty": str(fill.quantity),
                        "buyer": fill.buyer_id,
                        "seller": fill.seller_id,
                    },
                )
            )
            self._t("engine", "发布 Fill 事件 ← 注意：发生在结算之后")

        # 步骤 5：市价单未成交部分 → 撤单并释放资金
        filled_so_far = self.accounts.filled_quantity_of(order.order_id)
        if order_type is OrderType.MARKET and filled_so_far < order.quantity:
            filled = filled_so_far
            self.accounts.cancel_order(order.order_id, filled, ts)
            self._t(
                "engine",
                f"市价单仅成交 {filled}/{order.quantity}，撤销剩余并释放预留",
            )
            self._publish(
                Event("order_cancelled", {"order_id": order.order_id[:8], "filled": str(filled)})
            )

        # 步骤 6：完全成交事件
        for oid in result.completed_orders:
            self._publish(Event("order_filled", {"order_id": oid[:8]}))
            self._t("engine", f"发布 OrderFilled {oid[:8]}")

        if not result.fills and order.status is OrderStatus.OPEN:
            self._t("engine", f"未成交，挂单 → 最优买/卖 = {fmt(self.book.best_bid)}/{fmt(self.book.best_ask)}")

        return order, result.fills

    # ------------------------------------------------------------------ 撤单

    def cancel_order(self, order_id: str, ts: int) -> bool:
        order = self.accounts.orders.get(order_id)
        if order is None:
            self._t("engine", f"撤单失败：未知订单 {order_id[:8]}")
            return False

        cancelled_in_book = self.book.cancel_order(order_id)
        self.accounts.cancel_order(
            order_id, self.accounts.filled_quantity_of(order_id), ts
        )
        self._t(
            "engine",
            f"撤单 {order_id[:8]}（簿上存在={cancelled_in_book}），剩余预留已释放",
        )
        if cancelled_in_book:
            self._publish(Event("order_cancelled", {"order_id": order_id[:8]}))
        return cancelled_in_book

    # ------------------------------------------------------------------ 行情发布

    def publish_book_update(self) -> Optional[Event]:
        """计算订单簿增量并发布——对应 v1 的 `OrderBookPublisher`。

        v1 每 100ms 跑一次，每 10 次发一次全量快照，其余发增量；
        并且**有一个根本弱点**：它靠周期性快照兜底，但 `sequence` 传了却不用于缺口检测，
        所以客户端丢包后只能被动等下一次快照（见 v1 指南 7.2 第 3 条）。
        v2 会增加"序号 + 可请求重传"（`ARCHITECTURE_V2.md` 4.5 节）。
        """
        bids = dict(self.book.depth(Side.BID, levels=10))
        asks = dict(self.book.depth(Side.ASK, levels=10))

        deltas: list[dict] = []
        for side_name, prev, cur in (
            ("bid", self._published_bids, bids),
            ("ask", self._published_asks, asks),
        ):
            for price, qty in cur.items():
                old = prev.get(price)
                action = "add" if old is None else ("update" if old != qty else None)
                if action:
                    deltas.append({"action": action, "side": side_name, "price": str(price), "qty": str(qty)})
            for price in prev:
                if price not in cur:
                    deltas.append({"action": "remove", "side": side_name, "price": str(price), "qty": "0"})

        self._published_bids, self._published_asks = bids, asks
        if not deltas:
            return None

        self._sequence += 1
        event = Event("book_delta", {"seq": self._sequence, "deltas": len(deltas)})
        self._publish(event)
        self._t("engine", f"发布订单簿增量 seq={self._sequence}（{len(deltas)} 档变化）")
        return event

    # ------------------------------------------------------------------ 状态展示

    def book_text(self, levels: int = 5) -> str:
        lines = []
        for price, qty in reversed(self.book.depth(Side.ASK, levels)):
            lines.append(f"    ask {price:>10} │ {qty}")
        lines.append("    " + "─" * 22)
        for price, qty in self.book.depth(Side.BID, levels):
            lines.append(f"    bid {price:>10} │ {qty}")
        if len(lines) == 1:
            return "    （空）"
        return "\n".join(lines)
