"""复式记账账本。

**这是本模型与 Rust 版 v1 最重要的一处差异，而且是刻意升级。**

v1 是**单边流水**（single-sided ledger）：每行是一个用户的增减 + 滚动余额，
没有科目表、没有系统账户。后果是系统**无法自证资金守恒**——
详见 `EXCHANGE_DOMAIN_NOTES.md` 第 4.5 节，以及 v1 指南 7.2 第 11 条
（机器人那一方被跳过时，等于凭空创造/销毁资产）。

这里实现的是**复式记账**（double-entry）：
  * 每笔业务产生**多条分录**，且**每个资产的净额恒为零**
  * 用户的对方是**系统账户**（手续费、铸造、外部出入金）
  * 因此"钱既没多也没少"是一个**可以自动断言**的不变量，而不是靠人肉对账

对照 SQL 草图见 `ARCHITECTURE_V2.md` 第 5 节。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Iterable, Optional

ZERO = Decimal(0)


class AccountKind(str, Enum):
    """科目类型。真实系统还有 clearing（清算）、margin（保证金）等。"""

    USER = "user"  # 用户账户
    FEE = "system_fee"  # 交易所手续费收入 —— v1 完全没有这个概念
    MINT = "system_mint"  # 铸造（faucet / 空投）
    EXTERNAL = "system_external"  # 法币/链上出入金的对手方


@dataclass(frozen=True)
class Account:
    id: str
    kind: AccountKind
    label: str

    def __str__(self) -> str:
        return self.label


@dataclass(frozen=True)
class JournalEntry:
    txn_id: str
    ts: int
    account_id: str
    asset: str
    amount: Decimal  # 正 = 增加该账户资产；负 = 减少
    entry_type: str  # deposit | withdrawal | trade | fee | reserve | release
    memo: str = ""

    def __str__(self) -> str:
        sign = "+" if self.amount >= 0 else ""
        return (
            f"[{self.ts:>4}] {self.account_id:<10} {sign}{self.amount} {self.asset:<4} "
            f"({self.entry_type}) {self.memo}"
        )


class UnbalancedTransaction(Exception):
    """分录不平衡。在真实系统里这应当是"拒绝写入 + 告警"，而不是忽略。"""


class Ledger:
    def __init__(self) -> None:
        self.accounts: dict[str, Account] = {}
        self.entries: list[JournalEntry] = []
        self._txn_counter = 0

    # ------------------------------------------------------------------ 账户

    def create_account(self, account_id: str, kind: AccountKind, label: str) -> Account:
        account = Account(id=account_id, kind=kind, label=label)
        self.accounts[account_id] = account
        return account

    def account(self, account_id: str) -> Account:
        return self.accounts[account_id]

    # ------------------------------------------------------------------ 记账

    def next_txn_id(self, prefix: str = "txn") -> str:
        self._txn_counter += 1
        return f"{prefix}-{self._txn_counter:06d}"

    def post(
        self,
        txn_id: str,
        ts: int,
        lines: Iterable[tuple[str, str, Decimal, str]],
        memo: str = "",
    ) -> None:
        """写入一笔平衡的业务。

        `lines` 中每项为 `(account_id, asset, amount, entry_type)`。
        **每个资产的净额必须为零，否则抛异常且整体不写入**——
        这是 v1 做不到的校验（v1 每行都是独立的单边增量）。
        """
        lines = list(lines)

        totals: dict[str, Decimal] = {}
        for _account_id, asset, amount, _entry_type in lines:
            totals[asset] = totals.get(asset, ZERO) + amount

        unbalanced = {a: v for a, v in totals.items() if v != ZERO}
        if unbalanced:
            raise UnbalancedTransaction(
                f"分录不平衡，拒绝写入：{ {a: str(v) for a, v in unbalanced.items()} } "
                f"(txn={txn_id}, memo={memo})"
            )

        for account_id, asset, amount, entry_type in lines:
            if account_id not in self.accounts:
                raise KeyError(f"未知账户 {account_id}")
            self.entries.append(
                JournalEntry(
                    txn_id=txn_id,
                    ts=ts,
                    account_id=account_id,
                    asset=asset,
                    amount=amount,
                    entry_type=entry_type,
                    memo=memo,
                )
            )

    # ------------------------------------------------------------------ 查询

    def balance(self, account_id: str, asset: str) -> Decimal:
        total = ZERO
        for e in self.entries:
            if e.account_id == account_id and e.asset == asset:
                total += e.amount
        return total

    def all_balances(self) -> dict[str, dict[str, Decimal]]:
        result: dict[str, dict[str, Decimal]] = {}
        for e in self.entries:
            result.setdefault(e.account_id, {})
            result[e.account_id][e.asset] = (
                result[e.account_id].get(e.asset, ZERO) + e.amount
            )
        # 去掉四舍五入后为 0 的项，便于展示
        return {
            acct: {a: v for a, v in assets.items() if v != ZERO}
            for acct, assets in result.items()
        }

    def total_for_kind(self, kind: AccountKind, asset: str) -> Decimal:
        total = ZERO
        for account_id, account in self.accounts.items():
            if account.kind is kind:
                total += self.balance(account_id, asset)
        return total

    def tail(self, n: int = 12) -> list[JournalEntry]:
        return self.entries[-n:]

    # ------------------------------------------------------------------ 不变量

    def conservation(self) -> dict[str, Decimal]:
        """每个资产在**所有账户**上的净额。理论上必须全为 0。

        这就是 v1 无法回答的那个问题："钱有没有多出来或者少掉？"
        """
        totals: dict[str, Decimal] = {}
        for e in self.entries:
            totals[e.asset] = totals.get(e.asset, ZERO) + e.amount
        return totals

    def is_conserved(self) -> bool:
        return all(v == ZERO for v in self.conservation().values())

    def conservation_report(self) -> str:
        parts = [f"{asset}={fmt(amount)}" for asset, amount in sorted(self.conservation().items())]
        status = "OK" if self.is_conserved() else "❌ 不守恒"
        return f"资金守恒检查: {status}  ({', '.join(parts) if parts else '无记录'})"


def fmt(amount) -> str:
    """固定小数输出，避免 Decimal 打印出 `0E-8` 这类科学计数法。"""
    if amount is None:
        return "-"
    return f"{amount:f}"


def round_to(amount: Decimal, scale: int) -> Decimal:
    """按币种精度舍入。

    显式用 `ROUND_HALF_UP`（金融惯例），而不是 Python 默认的 `ROUND_HALF_EVEN`——
    真实系统必须**明确舍入方向与责任方**，因为舍入差异累积起来就是账目差额。
    """
    quant = Decimal(1).scaleb(-scale)
    return amount.quantize(quant, rounding=ROUND_HALF_UP)
