"""事件流模拟器：把一条下单链路完整跑一遍，逐步打印发生了什么。

用法：
    python3 simulator.py scenarios/01_rest_then_fill.json
    python3 simulator.py --all

设计要点：
  * **时间戳是虚拟的**（每步 +1），不读真实时钟。这样同样的场景永远得到同样的输出
    ——这就是"确定性"的最直观体现（v2 原则 5）。
  * 每一行日志都标了 **actor**：client / accounts / engine。看 actor 的切换，
    就能看出请求在服务之间怎么走——这是"gateway 做了什么、只看盘走哪些服务"这类问题的答案。
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

# 显式把脚本所在目录加入模块搜索路径：不依赖调用方的 cwd，
# 这样从任何目录 `python3 /path/to/simulator.py` 都能跑起来。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from mexchange.accounts import (  # noqa: E402
    AccountsService,
    InsufficientFunds,
    MarketMeta,
)
from mexchange.book import OrderBook  # noqa: E402
from mexchange.domain import OrderType, Side, TimeInForce  # noqa: E402
from mexchange.ledger import AccountKind, Ledger, fmt  # noqa: E402
from mexchange.venue import Venue  # noqa: E402

SCENARIO_DIR = _HERE / "scenarios"

ACTOR_WIDTH = 10


class Simulation:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.clock = 0
        self.step = 0
        self.lines: list[str] = []

        symbol = spec.get("symbol", "KCN/EUR")
        base, quote = symbol.split("/")
        self.market = MarketMeta(
            symbol=symbol,
            base_asset=base,
            quote_asset=quote,
            price_scale=spec.get("price_scale", 2),
            qty_scale=spec.get("qty_scale", 8),
            fee_bps=spec.get("fee_bps", 10),
        )

        self.ledger = Ledger()
        for account_id, kind, label in (
            ("SYS_EXTERNAL", AccountKind.EXTERNAL, "外部"),
            ("SYS_FEE", AccountKind.FEE, "手续费收入"),
            ("SYS_MINT", AccountKind.MINT, "铸造"),
        ):
            self.ledger.create_account(account_id, kind, label)

        self.accounts = AccountsService(
            ledger=self.ledger,
            market=self.market,
            market_price_cap=Decimal(spec.get("market_price_cap", "1000")),
        )
        self.book = OrderBook(symbol)
        self.venue = Venue(self.book, self.accounts, trace=self._trace)

        self._seed_accounts()
        self.labels: dict[str, str] = {}

    # ------------------------------------------------------------------ 追踪

    def _trace(self, actor: str, message: str) -> None:
        self.step += 1
        self.lines.append(f"[{self.step:>3}] {actor:<{ACTOR_WIDTH}} {message}")

    def _note(self, text: str) -> None:
        self.lines.append(f"      {'':<{ACTOR_WIDTH}} {text}")

    def _section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(f"── {title} " + "─" * max(0, 60 - len(title)))

    # ------------------------------------------------------------------ 初始化

    def _seed_accounts(self) -> None:
        for account_id, assets in self.spec.get("accounts", {}).items():
            self.ledger.create_account(account_id, AccountKind.USER, account_id)
            for asset, amount in assets.items():
                self.accounts.deposit(
                    account_id, asset, Decimal(str(amount)), self.clock
                )
                self.clock += 1

    # ------------------------------------------------------------------ 执行

    def run(self) -> None:
        self._section(f"场景：{self.spec.get('name', '(未命名)')}")
        if self.spec.get("description"):
            self._note(self.spec["description"])

        for raw_step in self.spec.get("steps", []):
            self._run_step(raw_step)

        self._print_book()
        self._print_state()

    def _run_step(self, step: dict[str, Any]) -> None:
        op = step.get("op")
        self.clock += 1

        if op == "place":
            self._op_place(step)
        elif op == "cancel":
            self._op_cancel(step)
        elif op == "book":
            self._print_book(inline=True)
        elif op == "state":
            self._print_state(inline=True)
        elif op == "note":
            self._note(step.get("text", ""))
        else:
            raise ValueError(f"未知操作：{op}")

        # 每次下单/撤单后都尝试发布一次订单簿增量（对应 v1 的 100ms 发布循环）
        if op in ("place", "cancel"):
            self.venue.publish_book_update()

    def _op_place(self, step: dict[str, Any]) -> None:
        order_type = OrderType(step.get("order_type", "limit"))
        side = Side(step["side"])
        tif = TimeInForce(step.get("tif", "gtc"))
        price_raw = step.get("price")
        price = Decimal(str(price_raw)) if price_raw is not None else None

        try:
            order, _fills = self.venue.place_order(
                account_id=step["account"],
                side=side,
                order_type=order_type,
                quantity=Decimal(str(step["quantity"])),
                ts=self.clock,
                price=price,
                tif=tif,
                post_only=bool(step.get("post_only", False)),
            )
        except (InsufficientFunds, ValueError) as exc:
            self.step += 1
            self.lines.append(
                f"[{self.step:>3}] {'accounts':<{ACTOR_WIDTH}} ❌ 拒绝：{exc}"
            )
            return

        if order is not None and step.get("as"):
            self.labels[step["as"]] = order.order_id

    def _op_cancel(self, step: dict[str, Any]) -> None:
        ref = step.get("order")
        order_id = self.labels.get(ref, ref) if ref else None
        if order_id is None:
            self.step += 1
            self.lines.append(
                f"[{self.step:>3}] {'engine':<{ACTOR_WIDTH}} ❌ 撤单缺少 order 引用"
            )
            return
        self.venue.cancel_order(order_id, self.clock)

    # ------------------------------------------------------------------ 展示

    def _print_book(self, inline: bool = False) -> None:
        self._section("订单簿")
        self.lines.append(self.venue.book_text())

    def _print_state(self, inline: bool = False) -> None:
        m = self.market
        self._section("账户余额")
        for account_id, account in sorted(self.ledger.accounts.items()):
            balances = {
                asset: amount
                for asset, amount in self.ledger.all_balances()
                .get(account_id, {})
                .items()
            }
            if not balances:
                continue
            reserved = {
                asset: self.accounts.reserved_amount(account_id, asset)
                for asset in balances
                if self.accounts.reserved_amount(account_id, asset) > 0
            }
            text = "  ".join(
                f"{asset}={fmt(amount)}" for asset, amount in sorted(balances.items())
            )
            extra = ""
            if reserved:
                extra = "   冻结: " + "  ".join(
                    f"{a}={fmt(v)}" for a, v in reserved.items()
                )
            self.lines.append(f"    {account.label:<12} {text}{extra}")

        self._section("最近账本分录")
        for entry in self.ledger.tail(14):
            self.lines.append(f"    {entry}")

        self._section("不变量")
        self.lines.append(f"    {self.ledger.conservation_report()}")
        fee_income = self.ledger.total_for_kind(AccountKind.FEE, m.quote_asset)
        self.lines.append(
            f"    交易所手续费收入: {fmt(fee_income)} {m.quote_asset}"
            f"   ← v1 永远得不到这个数（手续费从不入账）"
        )
        self.lines.append(f"    账本分录总数: {len(self.ledger.entries)}")

    def render(self) -> str:
        return "\n".join(self.lines)


def dispatch_place(
    venue: Venue, step: dict[str, Any], ts: int
) -> None:  # pragma: no cover - 保留给外部调用
    raise NotImplementedError


def run_scenario_file(path: Path) -> str:
    spec = json.loads(path.read_text(encoding="utf-8"))
    sim = Simulation(spec)
    sim.run()
    return sim.render()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mExchange 业务逻辑模拟器")
    parser.add_argument("scenario", nargs="?", help="场景 JSON 文件")
    parser.add_argument("--all", action="store_true", help="跑 scenarios/ 下的全部场景")
    args = parser.parse_args(argv)

    if args.all:
        paths = sorted(SCENARIO_DIR.glob("*.json"))
        if not paths:
            print("没有找到场景文件", file=sys.stderr)
            return 1
        for i, path in enumerate(paths):
            if i:
                print("\n" + "=" * 78 + "\n")
            print(run_scenario_file(path))
        return 0

    if not args.scenario:
        parser.print_help()
        return 1

    path = Path(args.scenario)
    if not path.exists():
        candidate = SCENARIO_DIR / path.name
        if candidate.exists():
            path = candidate
        else:
            print(f"找不到场景：{args.scenario}", file=sys.stderr)
            return 1

    print(run_scenario_file(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
