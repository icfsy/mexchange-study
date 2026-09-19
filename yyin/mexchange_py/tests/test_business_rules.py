"""业务规则测试。

运行：
    cd yyin/mexchange_py
    python3 -m unittest discover -s tests -v

这些测试是**规格说明**：每一条都对应 `EXCHANGE_DOMAIN_NOTES.md` 或
`ARCHITECTURE_GUIDE.md` 里的一条业务规则。
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from meexchange.accounts import AccountsService, InsufficientFunds, MarketMeta
from meexchange.book import OrderBook
from meexchange.domain import (
    Order,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    new_id,
)
from meexchange.ledger import ZERO, AccountKind, Ledger
from meexchange.venue import Venue

D = Decimal


def limit(side: Side, price: str, qty: str, account: str = "acct") -> Order:
    return Order(
        order_id=new_id(),
        account_id=account,
        symbol="KCN/EUR",
        side=side,
        order_type=OrderType.LIMIT,
        quantity=D(qty),
        price=D(price),
    )


def market(side: Side, qty: str, account: str = "acct") -> Order:
    return Order(
        order_id=new_id(),
        account_id=account,
        symbol="KCN/EUR",
        side=side,
        order_type=OrderType.MARKET,
        quantity=D(qty),
    )


class MatchingRules(unittest.TestCase):
    """撮合规则（纯引擎，不涉及资金）。"""

    def test_price_priority_best_price_first(self) -> None:
        book = OrderBook("KCN/EUR")
        book.add_limit_order(limit(Side.ASK, "101", "5"), ts=1)
        book.add_limit_order(limit(Side.ASK, "100", "5"), ts=2)  # 更优价，后来但优先
        result = book.add_limit_order(limit(Side.BID, "101", "5"), ts=3)
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.fills[0].price, D("100"), "应吃掉最优价 100")

    def test_time_priority_fifo_within_a_level(self) -> None:
        book = OrderBook("KCN/EUR")
        first = limit(Side.ASK, "100", "5")
        book.add_limit_order(first, ts=1)
        second = limit(Side.ASK, "100", "5")
        book.add_limit_order(second, ts=2)
        result = book.add_limit_order(limit(Side.BID, "100", "5"), ts=3)
        self.assertEqual(result.fills[0].sell_order_id, first.order_id, "同价先到先得")

    def test_execution_at_resting_price_not_aggressor_price(self) -> None:
        """被动方定价：出价 105 撞挂着的 100，成交价必须是 100。"""
        book = OrderBook("KCN/EUR")
        book.add_limit_order(limit(Side.ASK, "100", "5"), ts=1)
        result = book.add_limit_order(limit(Side.BID, "105", "5"), ts=2)
        self.assertEqual(result.fills[0].price, D("100"))

    def test_market_order_never_rests(self) -> None:
        book = OrderBook("KCN/EUR")
        book.add_limit_order(limit(Side.BID, "100", "4"), ts=1)
        result = book.add_market_order(market(Side.ASK, "10"), ts=2)
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(result.fills[0].quantity, D("4"))
        self.assertEqual(book.best_ask, None, "市价单剩余部分不得挂单")
        self.assertEqual(book.resting_order_count, 0)

    def test_partial_fill_leaves_remainder_resting(self) -> None:
        book = OrderBook("KCN/EUR")
        book.add_limit_order(limit(Side.BID, "100", "5"), ts=1)
        seller = limit(Side.ASK, "100", "12")
        result = book.add_limit_order(seller, ts=2)
        self.assertEqual(result.fills[0].quantity, D("5"))
        self.assertEqual(seller.remaining, D("7"))
        self.assertEqual(seller.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(book.best_ask, D("100"))

    def test_fully_filled_order_is_not_added_to_book(self) -> None:
        book = OrderBook("KCN/EUR")
        book.add_limit_order(limit(Side.ASK, "100", "5"), ts=1)
        result = book.add_limit_order(limit(Side.BID, "100", "5"), ts=2)
        self.assertIn(result.order_id, result.completed_orders)
        self.assertEqual(book.resting_order_count, 0)


class TimeInForceRules(unittest.TestCase):
    """TIF 是撮合语义差异，不是参数差异。"""

    def _book(self) -> OrderBook:
        book = OrderBook("KCN/EUR")
        book.add_limit_order(limit(Side.BID, "100", "5"), ts=1)
        return book

    def test_fok_rejected_when_liquidity_insufficient(self) -> None:
        book = self._book()
        order = limit(Side.ASK, "99", "10")
        order.tif = TimeInForce.FOK
        result = book.add_limit_order(order, ts=2)
        self.assertTrue(result.rejected)
        self.assertEqual(len(result.fills), 0, "FOK 被拒时不能有任何成交")
        self.assertEqual(book.best_bid, D("100"), "FOK 被拒时订单簿必须不变")

    def test_fok_succeeds_when_fully_fillable(self) -> None:
        book = self._book()
        order = limit(Side.ASK, "99", "5")
        order.tif = TimeInForce.FOK
        result = book.add_limit_order(order, ts=2)
        self.assertFalse(result.rejected)
        self.assertEqual(result.fills[0].quantity, D("5"))

    def test_ioc_cancels_remainder_instead_of_resting(self) -> None:
        book = self._book()
        order = limit(Side.ASK, "99", "8")
        order.tif = TimeInForce.IOC
        result = book.add_limit_order(order, ts=2)
        self.assertEqual(result.fills[0].quantity, D("5"))
        self.assertEqual(order.status, OrderStatus.CANCELLED, "IOC 剩余不挂单")
        self.assertEqual(book.best_ask, None)

    def test_post_only_rejected_when_it_would_cross(self) -> None:
        book = self._book()
        order = limit(Side.ASK, "99", "2")
        order.post_only = True
        result = book.add_limit_order(order, ts=2)
        self.assertTrue(result.rejected)

    def test_post_only_rests_when_it_does_not_cross(self) -> None:
        book = self._book()
        order = limit(Side.ASK, "101", "2")
        order.post_only = True
        result = book.add_limit_order(order, ts=2)
        self.assertFalse(result.rejected)
        self.assertEqual(book.best_ask, D("101"))


class LedgerRules(unittest.TestCase):
    """复式记账与资金守恒。"""

    def _fixture(self):
        ledger = Ledger()
        ledger.create_account("SYS_EXTERNAL", AccountKind.EXTERNAL, "外部")
        ledger.create_account("SYS_FEE", AccountKind.FEE, "手续费")
        ledger.create_account("alice", AccountKind.USER, "alice")
        ledger.create_account("bob", AccountKind.USER, "bob")
        m = MarketMeta("KCN/EUR", "KCN", "EUR", 2, 8, fee_bps=10)
        accounts = AccountsService(ledger=ledger, market=m, fee_account_id="SYS_FEE")
        book = OrderBook("KCN/EUR")
        venue = Venue(book, accounts)
        return ledger, accounts, book, venue

    def test_unbalanced_transaction_is_rejected(self) -> None:
        ledger = Ledger()
        ledger.create_account("a", AccountKind.USER, "a")
        ledger.create_account("b", AccountKind.USER, "b")
        with self.assertRaises(Exception):
            ledger.post("t1", 1, [("a", "EUR", D("10"), "trade")])

    def test_deposit_is_balanced(self) -> None:
        ledger, accounts, _book, _venue = self._fixture()
        accounts.deposit("alice", "EUR", D("1000"), ts=1)
        self.assertEqual(ledger.balance("alice", "EUR"), D("1000"))
        self.assertEqual(ledger.balance("SYS_EXTERNAL", "EUR"), D("-1000"))
        self.assertTrue(ledger.is_conserved())

    def test_trade_conserves_funds_and_collects_fees(self) -> None:
        ledger, accounts, _book, venue = self._fixture()
        accounts.deposit("alice", "EUR", D("10000"), ts=1)
        accounts.deposit("bob", "KCN", D("10"), ts=2)

        venue.place_order("alice", Side.BID, OrderType.LIMIT, D("10"), ts=3, price=D("100.50"))
        venue.place_order("bob", Side.ASK, OrderType.LIMIT, D("10"), ts=4, price=D("100.00"))

        self.assertTrue(ledger.is_conserved(), "任何成交之后账目都必须守恒")
        # 成交价取挂单方 100.50，名义额 1005.00，双边各收 0.1%
        self.assertEqual(ledger.balance("SYS_FEE", "EUR"), D("2.02"))
        self.assertEqual(ledger.balance("alice", "KCN"), D("10"))
        self.assertEqual(ledger.balance("bob", "EUR"), D("1003.99"))

    def test_settlement_is_idempotent(self) -> None:
        ledger, accounts, _book, venue = self._fixture()
        accounts.deposit("alice", "EUR", D("10000"), ts=1)
        accounts.deposit("bob", "KCN", D("10"), ts=2)
        venue.place_order("alice", Side.BID, OrderType.LIMIT, D("5"), ts=3, price=D("100"))
        _order, fills = venue.place_order(
            "bob", Side.ASK, OrderType.LIMIT, D("5"), ts=4, price=D("100")
        )
        balance_before = ledger.balance("alice", "EUR")
        entries_before = len(ledger.entries)

        txn_first = accounts.settle_fill(fills[0], ts=5)
        txn_second = accounts.settle_fill(fills[0], ts=5)  # 重复投递

        self.assertEqual(txn_first, txn_second)
        self.assertEqual(len(ledger.entries), entries_before, "重复结算不得新增分录")
        self.assertEqual(ledger.balance("alice", "EUR"), balance_before)


class ReservationRules(unittest.TestCase):
    """下单冻结、撤单释放——v1 用一条负的 lock 流水来近似这件事。"""

    def _fixture(self):
        ledger = Ledger()
        ledger.create_account("SYS_EXTERNAL", AccountKind.EXTERNAL, "外部")
        ledger.create_account("SYS_FEE", AccountKind.FEE, "手续费")
        ledger.create_account("alice", AccountKind.USER, "alice")
        ledger.create_account("bob", AccountKind.USER, "bob")
        m = MarketMeta("KCN/EUR", "KCN", "EUR", 2, 8, fee_bps=10)
        accounts = AccountsService(ledger=ledger, market=m, fee_account_id="SYS_FEE")
        venue = Venue(OrderBook("KCN/EUR"), accounts)
        return ledger, accounts, venue

    def test_insufficient_funds_rejected_before_touching_the_book(self) -> None:
        _ledger, accounts, venue = self._fixture()
        accounts.deposit("alice", "EUR", D("100"), ts=1)
        with self.assertRaises(InsufficientFunds):
            venue.place_order("alice", Side.BID, OrderType.LIMIT, D("10"), ts=2, price=D("100"))
        self.assertEqual(venue.book.resting_order_count, 0, "被拒订单不得进入订单簿")
        self.assertEqual(accounts.reserved_amount("alice", "EUR"), ZERO)

    def test_cancel_releases_the_whole_reservation(self) -> None:
        ledger, accounts, venue = self._fixture()
        accounts.deposit("alice", "EUR", D("10000"), ts=1)
        order, _ = venue.place_order(
            "alice", Side.BID, OrderType.LIMIT, D("5"), ts=2, price=D("100")
        )
        # 预留 = 100 × 1.001 × 5 = 500.50
        self.assertEqual(accounts.reserved_amount("alice", "EUR"), D("500.50"))
        self.assertEqual(accounts.available("alice", "EUR"), D("9499.50"))
        # 注意：账本余额从未改变，变的只是"可用"
        self.assertEqual(ledger.balance("alice", "EUR"), D("10000"))

        venue.cancel_order(order.order_id, ts=3)
        self.assertEqual(accounts.reserved_amount("alice", "EUR"), ZERO)
        self.assertEqual(accounts.available("alice", "EUR"), D("10000"))

    def test_market_order_remainder_is_released(self) -> None:
        ledger, accounts, venue = self._fixture()
        accounts.deposit("alice", "EUR", D("10000"), ts=1)
        accounts.deposit("bob", "KCN", D("10"), ts=2)
        venue.place_order("alice", Side.BID, OrderType.LIMIT, D("4"), ts=3, price=D("100"))
        bob_order, _ = venue.place_order("bob", Side.ASK, OrderType.MARKET, D("10"), ts=4)
        self.assertEqual(accounts.reserved_amount("bob", "KCN"), ZERO, "未成交部分必须释放")
        # 注意：filled_quantity_of 以 order_id 为键，不是账户
        self.assertEqual(accounts.filled_quantity_of(bob_order.order_id), D("4"))


class KnownGaps(unittest.TestCase):
    """**故意把它们写成测试**：这些是 v1 的缺口，测试通过恰恰说明缺口存在。"""

    def test_self_trade_is_not_prevented(self) -> None:
        ledger = Ledger()
        ledger.create_account("SYS_EXTERNAL", AccountKind.EXTERNAL, "外部")
        ledger.create_account("SYS_FEE", AccountKind.FEE, "手续费")
        ledger.create_account("alice", AccountKind.USER, "alice")
        m = MarketMeta("KCN/EUR", "KCN", "EUR", 2, 8, fee_bps=10)
        accounts = AccountsService(ledger=ledger, market=m, fee_account_id="SYS_FEE")
        venue = Venue(OrderBook("KCN/EUR"), accounts)
        accounts.deposit("alice", "EUR", D("10000"), ts=1)
        accounts.deposit("alice", "KCN", D("10"), ts=2)

        venue.place_order("alice", Side.BID, OrderType.LIMIT, D("5"), ts=3, price=D("100"))
        _order, fills = venue.place_order(
            "alice", Side.ASK, OrderType.LIMIT, D("5"), ts=4, price=D("100")
        )

        self.assertEqual(len(fills), 1, "本模型不阻止自成交")
        # 自成交的代价：KCN 净变化为 0，EUR 只少了双边手续费
        self.assertEqual(ledger.balance("alice", "KCN"), D("10"))
        self.assertEqual(ledger.balance("alice", "EUR"), D("9999.00"))
        self.assertEqual(ledger.balance("SYS_FEE", "EUR"), D("1.00"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
