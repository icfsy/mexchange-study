"""撮合引擎：价格-时间优先（FIFO）。

对照 Rust 版 `matching_engine/src/lib.rs`：
  * `BTreeMap<Price, PriceLevel>` → 这里的 `dict[Decimal, PriceLevel]` + 取值时排序
  * `HashMap<OrderId, Order>`     → 这里的 `dict[str, Order]`
  * `VecDeque<Order>`（档位内 FIFO）→ `collections.deque`

**为什么 Rust 用 BTreeMap 而这里用 dict？**
BTreeMap 保持键有序，所以"最优价"是 O(1)（取两端）。Python 的 dict 无序，
所以这里每次要用 `min()`/`max()`，是 O(n)。对一个教学模型完全够用，
但它正好说明**有序映射买到了什么**：O(log n) 插入 + O(1) 取最优价。

**确定性**：本模块不读时钟、不用随机数。时间戳由调用方注入——
这是 v2 的原则 5（`ARCHITECTURE_V2.md` 第 2 节）。Rust 版 v1 在服务层自取时间，
这是它无法确定性回放的原因之一。
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Optional

from .domain import (
    Fill,
    Order,
    OrderResult,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)


class PriceLevel:
    """一个价位：FIFO 队列 + 缓存的聚合量。

    `total` 是缓存，避免每次求深度都要遍历队列——与 Rust 版的 `total_quantity` 同理。
    """

    __slots__ = ("price", "orders", "total")

    def __init__(self, price: Decimal) -> None:
        self.price = price
        self.orders: deque[Order] = deque()
        self.total: Decimal = Decimal(0)

    def __len__(self) -> int:
        return len(self.orders)

    def __repr__(self) -> str:
        return f"PriceLevel({self.price}, n={len(self.orders)}, total={self.total})"


class OrderBook:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.bids: dict[Decimal, PriceLevel] = {}
        self.asks: dict[Decimal, PriceLevel] = {}
        self.orders: dict[str, Order] = {}
        self._seq = 0

    # ------------------------------------------------------------------ 只读视图

    @property
    def best_bid(self) -> Optional[Decimal]:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> Optional[Decimal]:
        return min(self.asks) if self.asks else None

    @property
    def spread(self) -> Optional[Decimal]:
        b, a = self.best_bid, self.best_ask
        if b is None or a is None or a <= b:
            return None
        return a - b

    @property
    def resting_order_count(self) -> int:
        return len(self.orders)

    def level_quantity(self, side: Side, price: Decimal) -> Decimal:
        book = self.bids if side is Side.BID else self.asks
        level = book.get(price)
        return level.total if level else Decimal(0)

    def depth(self, side: Side, levels: int = 10) -> list[tuple[Decimal, Decimal]]:
        """返回前 N 档，最优价在前。与 Rust 版的 `get_bids` / `get_asks` 对应。"""
        book = self.bids if side is Side.BID else self.asks
        prices = sorted(book.keys(), reverse=(side is Side.BID))
        return [(p, book[p].total) for p in prices[:levels]]

    def order(self, order_id: str) -> Optional[Order]:
        return self.orders.get(order_id)

    # ------------------------------------------------------------------ 写操作

    def add_limit_order(self, order: Order, ts: int) -> OrderResult:
        if order.price is None:
            raise ValueError("限价单必须有价格")
        if order.price <= 0:
            raise ValueError("限价单价格必须为正")
        if order.quantity <= 0:
            raise ValueError("数量必须为正")

        result = OrderResult(order_id=order.order_id)
        order.remaining = order.quantity

        # Post-Only：只做挂单。若会立即成交则拒绝（撮合前预判）
        if order.post_only and self._would_cross(order):
            order.status = OrderStatus.REJECTED
            result.rejected_reason = "post_only 订单会立即成交，已拒绝"
            return result

        # FOK：要么全部成交，要么全撤。必须先预演可用流动性。
        if order.tif is TimeInForce.FOK:
            if self._available_liquidity(order) < order.quantity:
                order.status = OrderStatus.REJECTED
                result.rejected_reason = "FOK 无法全额成交，已拒绝"
                return result

        self._match(order, ts, result)

        if order.is_filled:
            order.status = OrderStatus.FILLED
            result.completed_orders.append(order.order_id)
        elif order.tif is TimeInForce.IOC or order.tif is TimeInForce.FOK:
            # IOC/FOK 不挂单；FOK 走到这里说明预演与执行不一致（不应发生）
            order.status = OrderStatus.CANCELLED
        else:
            self._rest(order)
            order.status = (
                OrderStatus.PARTIALLY_FILLED
                if order.filled_quantity > 0
                else OrderStatus.OPEN
            )

        return result

    def add_market_order(self, order: Order, ts: int) -> OrderResult:
        """市价单：吃掉对手方最优价，**从不挂单**。未成交部分由调用方负责撤销/上报。"""
        if order.quantity <= 0:
            raise ValueError("数量必须为正")

        result = OrderResult(order_id=order.order_id)
        order.remaining = order.quantity
        self._match(order, ts, result)

        if order.is_filled:
            order.status = OrderStatus.FILLED
            result.completed_orders.append(order.order_id)
        else:
            # 语义上等同 IOC：剩余部分被丢弃（Rust 版也是这样，且不在结果里留痕）
            order.status = OrderStatus.CANCELLED

        return result

    def cancel_order(self, order_id: str) -> bool:
        """撤单。返回 True 表示确实撤掉了一笔挂单。"""
        order = self.orders.pop(order_id, None)
        if order is None:
            return False
        book = self.bids if order.side is Side.BID else self.asks
        level = book.get(order.price)
        if level is not None:
            try:
                level.orders.remove(order)
            except ValueError:  # 理论上不会发生
                pass
            level.total -= order.remaining
            if not level.orders:
                del book[order.price]
        order.status = OrderStatus.CANCELLED
        return True

    def restore_order(self, order: Order) -> None:
        """把订单放回订单簿（回滚原语）。

        与 Rust 版一致：市价单和已完全成交的订单不能恢复。
        **注意**：Rust 版 v1 定义了这个方法但**从未调用**（详见 v1 指南 7.2 第 1 条）；
        这里保留它，是为了让你看到"回滚原语存在、但回滚策略缺失"这个落差。
        """
        if order.is_market or order.is_filled or order.price is None:
            return
        self._rest(order)

    # ------------------------------------------------------------------ 内部实现

    def _match(self, order: Order, ts: int, result: OrderResult) -> None:
        book = self.asks if order.side is Side.BID else self.bids
        # 先对价格取快照：Rust 版因为借用检查也必须这样做
        prices = sorted(book.keys(), reverse=(order.side is Side.ASK))

        for price in prices:
            if order.remaining <= 0:
                break
            level = book.get(price)
            if level is None:
                continue
            if not self._acceptable(order, price):
                break  # 价格不再可接受，后面的更差

            while level.orders and order.remaining > 0:
                resting = level.orders[0]  # FIFO：最早挂的排在最前
                qty = min(order.remaining, resting.remaining)

                order.remaining -= qty
                resting.remaining -= qty
                level.total -= qty

                # 成交价 = 被动方（挂单方）的价格 → 主动方获得价格改善
                if order.side is Side.BID:
                    buy_id, sell_id = order.order_id, resting.order_id
                    buyer, seller = order.account_id, resting.account_id
                else:
                    buy_id, sell_id = resting.order_id, order.order_id
                    buyer, seller = resting.account_id, order.account_id

                result.fills.append(
                    Fill(
                        symbol=self.symbol,
                        buy_order_id=buy_id,
                        sell_order_id=sell_id,
                        buyer_id=buyer,
                        seller_id=seller,
                        price=price,
                        quantity=qty,
                        timestamp=ts,
                    )
                )

                if resting.remaining <= 0:
                    level.orders.popleft()
                    self.orders.pop(resting.order_id, None)
                    resting.status = OrderStatus.FILLED
                    result.completed_orders.append(resting.order_id)
                else:
                    resting.status = OrderStatus.PARTIALLY_FILLED

            if not level.orders:
                # Rust 版把待删价位收集到列表、循环结束后统一删除（借用检查所迫）；
                # Python 没有这个限制，因为上面迭代的是价格快照列表。
                del book[price]

    def _acceptable(self, order: Order, price: Decimal) -> bool:
        if order.order_type is OrderType.MARKET:
            return True
        assert order.price is not None
        if order.side is Side.BID:
            return price <= order.price
        return price >= order.price

    def _available_liquidity(self, order: Order) -> Decimal:
        """可成交的对手方总量（用于 FOK 预演）。"""
        book = self.asks if order.side is Side.BID else self.bids
        total = Decimal(0)
        for price, level in book.items():
            if self._acceptable(order, price):
                total += level.total
        return total

    def _would_cross(self, order: Order) -> bool:
        book = self.asks if order.side is Side.BID else self.bids
        return any(self._acceptable(order, p) for p in book)

    def _rest(self, order: Order) -> None:
        self._seq += 1
        order.seq = self._seq
        self.orders[order.order_id] = order
        book = self.bids if order.side is Side.BID else self.asks
        level = book.get(order.price)
        if level is None:
            level = PriceLevel(order.price)
            book[order.price] = level
        level.orders.append(order)
        level.total += order.remaining
