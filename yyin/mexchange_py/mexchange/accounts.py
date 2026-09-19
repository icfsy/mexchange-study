"""账户服务：余额校验、资金预留、成交结算、幂等。

对照 Rust 版 `accounts`（`models/order.rs`、`models/trade.rs`、`routes/internal.rs`）。

**与 v1 的两处刻意差异：**

1. **预留（reservation）是显式的。**
   v1 把"锁仓"实现为一条负的 `lock` 流水（减少 `available`），而 `balances.locked`
   这一列**从未被写入**（死列）。这里改成显式的 `reserved` 记账，
   语义清晰、可超时释放——这正是 v2 风控网关的模型（见 `ARCHITECTURE_V2.md` 4.3 节）。

2. **结算写复式分录，并真的收取手续费。**
   v1 把手续费算出来存进 `trades` 表却**从不入账**，所以交易所没有收入。
   这里手续费进入 `system_fee` 系统账户。

**关于幂等**：v1 的 `fill_id` 去重是"先在事务外 SELECT、再写"，存在竞态
（v1 指南 7.2 第 9 条）。真实系统必须把去重和写入放在同一个原子操作里
（唯一约束 / 单线程 / CAS）。这里用单线程 + 先登记 `settled` 再写账本模拟这个原子性。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .domain import Fill, Order, OrderStatus, OrderType, Side, TimeInForce, new_id
from .ledger import ZERO, AccountKind, Ledger, fmt, round_to


class InsufficientFunds(Exception):
    pass


class NegativeBalance(Exception):
    """余额为负——真实系统里这是数据库 CHECK 约束在兜底（v1 migration 003）。"""


@dataclass(frozen=True)
class MarketMeta:
    symbol: str
    base_asset: str
    quote_asset: str
    price_scale: int
    qty_scale: int
    fee_bps: int = 10  # 0.10%

    @property
    def fee_rate(self) -> Decimal:
        return Decimal(self.fee_bps) / Decimal(10_000)

    def round_price(self, v: Decimal) -> Decimal:
        return round_to(v, self.price_scale)

    def round_qty(self, v: Decimal) -> Decimal:
        return round_to(v, self.qty_scale)


@dataclass
class Reservation:
    """一笔挂单占用的资金。`per_unit` 是每个剩余单位占用的金额。"""

    order_id: str
    account_id: str
    asset: str
    per_unit: Decimal


class AccountsService:
    def __init__(
        self,
        ledger: Ledger,
        market: MarketMeta,
        market_price_cap: Decimal = Decimal("1000"),
        fee_account_id: str = "SYS_FEE",
    ) -> None:
        self.ledger = ledger
        self.market = market
        self.market_price_cap = market_price_cap
        self.fee_account_id = fee_account_id
        self.reserved: dict[tuple[str, str], Decimal] = {}
        self.reservations: dict[str, Reservation] = {}
        self.orders: dict[str, Order] = {}
        self.settled_fills: dict[str, str] = {}  # fill_id -> txn_id（幂等表）
        # 账本侧的成交量。**刻意与撮合引擎的 `order.remaining` 分开**：
        # Rust 版里引擎的 Order 和 accounts 的 orders 行是两份数据；
        # 本模型两者共用一个对象，如果共用一个计数就会被重复扣减。
        self.filled: dict[str, Decimal] = {}

    # ------------------------------------------------------------------ 余额

    def _key(self, account_id: str, asset: str) -> tuple[str, str]:
        return (account_id, asset)

    def balance(self, account_id: str, asset: str) -> Decimal:
        return self.ledger.balance(account_id, asset)

    def filled_quantity_of(self, order_id: str) -> Decimal:
        """账本侧记录的已成交量。"""
        return self.filled.get(order_id, ZERO)

    def reserved_amount(self, account_id: str, asset: str) -> Decimal:
        return self.reserved.get(self._key(account_id, asset), ZERO)

    def available(self, account_id: str, asset: str) -> Decimal:
        """可用 = 账本余额 − 未决预留。这就是风控真正要看的数字。"""
        return self.balance(account_id, asset) - self.reserved_amount(account_id, asset)

    # ------------------------------------------------------------------ 出入金

    def deposit(self, account_id: str, asset: str, amount: Decimal, ts: int, memo: str = "") -> None:
        """外部入金。对手方是系统 EXTERNAL 账户——所以账目仍然平衡。"""
        txn = self.ledger.next_txn_id("deposit")
        self.ledger.post(
            txn,
            ts,
            [
                ("SYS_EXTERNAL", asset, -amount, "deposit"),
                (account_id, asset, amount, "deposit"),
            ],
            memo=memo or f"deposit {amount} {asset} → {account_id}",
        )

    # ------------------------------------------------------------------ 预留

    def _reserve(self, account_id: str, asset: str, amount: Decimal) -> None:
        key = self._key(account_id, asset)
        self.reserved[key] = self.reserved.get(key, ZERO) + amount

    def _release(self, account_id: str, asset: str, amount: Decimal) -> None:
        key = self._key(account_id, asset)
        self.reserved[key] = self.reserved.get(key, ZERO) - amount
        if self.reserved[key] <= ZERO:
            self.reserved.pop(key, None)

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
    ) -> Order:
        """校验 + 预留资金，返回**尚未撮合**的订单。

        对应 v1 的 `POST /internal/orders` → `Order::place`。
        注意：v1 是在 accounts 里**同步查库并锁资金**，这会成为热路径上的数据库事务；
        v2 把这一步改成内存投影 + 预留（`ARCHITECTURE_V2.md` 4.3 节）。
        """
        m = self.market
        quantity = m.round_qty(quantity)
        if quantity <= ZERO:
            raise ValueError("数量必须为正")

        if order_type is OrderType.LIMIT:
            if price is None or price <= ZERO:
                raise ValueError("限价单必须给出正的价格")
            price = m.round_price(price)
        else:
            if post_only:
                raise ValueError("市价单不能是 post_only")
            price = None

        # 买方冻结计价币，卖方冻结基础币
        if side is Side.BID:
            unit = price if price is not None else self.market_price_cap
            # 预留必须覆盖"成交金额 + 手续费"，否则结算时可能把余额打成负数
            per_unit = m.round_price(unit * (Decimal(1) + m.fee_rate))
            asset = m.quote_asset
        else:
            per_unit = Decimal(1)
            asset = m.base_asset

        needed = m.round_qty(per_unit * quantity) if asset == m.base_asset else m.round_price(per_unit * quantity)
        if self.available(account_id, asset) < needed:
            raise InsufficientFunds(
                f"{account_id} 的 {asset} 不足：需要 {fmt(needed)}，可用 {fmt(self.available(account_id, asset))}"
            )

        order = Order(
            order_id=new_id(),
            account_id=account_id,
            symbol=m.symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            tif=tif,
            post_only=post_only,
        )
        self._reserve(account_id, asset, needed)
        self.reservations[order.order_id] = Reservation(
            order_id=order.order_id,
            account_id=account_id,
            asset=asset,
            per_unit=per_unit,
        )
        self.orders[order.order_id] = order
        return order

    # ------------------------------------------------------------------ 撤单

    def cancel_order(
        self, order_id: str, filled_quantity: Optional[Decimal], ts: int
    ) -> Optional[Order]:
        """撤销订单并释放**未成交部分**的预留。

        对应 v1 的 `POST /internal/cancel` 与 `Order::cancel_internal`。
        未成交部分的预留计算方式与 v1 一致：`per_unit × (quantity − filled)`。
        """
        order = self.orders.get(order_id)
        if order is None:
            return None  # v1: ORDER_NOT_FOUND → 匿名单，静默跳过
        if filled_quantity is None:
            filled_quantity = self.filled_quantity_of(order_id)
        remaining = order.quantity - filled_quantity
        if remaining < ZERO:
            remaining = ZERO
        self._release_units(order_id, remaining)
        order.status = OrderStatus.CANCELLED
        return order

    def _release_units(self, order_id: str, units: Decimal) -> None:
        res = self.reservations.get(order_id)
        if res is None or units <= ZERO:
            return
        self._release(res.account_id, res.asset, res.per_unit * units)

    # ------------------------------------------------------------------ 结算

    def settle_fill(self, fill: Fill, ts: int) -> str:
        """结算一笔成交：写复式分录 + 释放预留 + 更新订单状态。

        返回 txn_id。**幂等**：同一个 `fill_id` 重复调用只结算一次。
        """
        # 幂等登记（真实系统里这一步与下面的写入在同一事务/同一原子操作内）
        if fill.fill_id in self.settled_fills:
            return self.settled_fills[fill.fill_id]

        m = self.market
        notional = m.round_price(fill.price * fill.quantity)
        buyer_fee = m.round_price(notional * m.fee_rate)
        seller_fee = m.round_price(notional * m.fee_rate)

        txn = self.ledger.next_txn_id("trade")
        lines = [
            # 买方：付出计价币，收到基础币
            (fill.buyer_id, m.quote_asset, -notional, "trade"),
            (fill.buyer_id, m.base_asset, fill.quantity, "trade"),
            # 卖方：付出基础币，收到计价币
            (fill.seller_id, m.base_asset, -fill.quantity, "trade"),
            (fill.seller_id, m.quote_asset, notional, "trade"),
            # 手续费——v1 从不入账，这里进入系统手续费账户
            (fill.buyer_id, m.quote_asset, -buyer_fee, "fee"),
            (self.fee_account_id, m.quote_asset, buyer_fee, "fee"),
            (fill.seller_id, m.quote_asset, -seller_fee, "fee"),
            (self.fee_account_id, m.quote_asset, seller_fee, "fee"),
        ]
        memo = f"成交 {fill.quantity} {m.base_asset} @ {fill.price} {m.quote_asset}"
        self.ledger.post(txn, ts, lines, memo=memo)
        self.settled_fills[fill.fill_id] = txn

        # 释放被这笔成交消耗掉的预留（成交价可能优于限价 → 多冻的部分自动解冻）
        self._release_units(fill.buy_order_id, fill.quantity)
        self._release_units(fill.sell_order_id, fill.quantity)

        self._apply_fill_to_order(fill.buy_order_id, fill.quantity)
        self._apply_fill_to_order(fill.sell_order_id, fill.quantity)

        # 余额非负——真实系统靠数据库 CHECK 约束兜底（v1 migration 003）
        for account_id in (fill.buyer_id, fill.seller_id):
            for asset in (m.base_asset, m.quote_asset):
                if self.balance(account_id, asset) < ZERO:
                    raise NegativeBalance(
                        f"{account_id} 的 {asset} 余额为负：{self.balance(account_id, asset)}"
                    )
        return txn

    def _apply_fill_to_order(self, order_id: str, qty: Decimal) -> None:
        order = self.orders.get(order_id)
        if order is None:
            return  # 匿名（机器人）订单：v1 会跳过整个一侧
        self.filled[order_id] = self.filled.get(order_id, ZERO) + qty
        if order.status is OrderStatus.CANCELLED:
            return
        if self.filled[order_id] >= order.quantity:
            order.status = OrderStatus.FILLED
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
