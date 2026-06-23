from datetime import date, timedelta
from unittest import IsolatedAsyncioTestCase, mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from market.models import Holding, Order, Stock
from monkey import services
from monkey.kis import KisClient
from monkey.models import (
    Account,
    KisAccessToken,
    KisAccountCache,
    Monkey,
    MonkeyDailySnapshot,
    MonkeyIndexBaseline,
)
from monkey.serializers import build_monkey_metrics

_ACCOUNT_SEQ = [50330000]


def make_account(account_type=None, number=None, **kw):
    """Create a mock KIS Account for tests (unique CANO per call)."""
    if number is None:
        _ACCOUNT_SEQ[0] += 1
        number = str(_ACCOUNT_SEQ[0])
    return Account.objects.create(
        account_type=account_type or Account.AccountType.MOCK,
        app_key="appkey",
        app_secret="appsecret",
        account_number=number,
        product_code="01",
        **kw,
    )


class FakeKisClient:
    def __init__(
        self,
        price=1000,
        response=None,
        fail_order=False,
        balance=0,
        holdings=None,
        holiday=False,
        executions=None,
        securities_value=0,
        total_assets=0,
        total_pl=0,
        earning_rate=0.0,
        account=None,
    ):
        self.price = price
        self.response = response or {
            "rt_cd": "0",
            "msg1": "ok",
            "output": {"ODNO": "12345"},
        }
        self.fail_order = fail_order
        self.orders = []
        self.balance = balance
        self.holdings = holdings or {}
        self.holiday = holiday
        self.executions = executions or {}
        self.securities_value = securities_value
        self.total_assets = total_assets
        self.total_pl = total_pl
        self.earning_rate = earning_rate
        self.account = account
        self.balance_calls = 0
        self._odno_seq = 0

    def get_stock_price(self, ticker):
        return self.price

    def get_account_balance(self, include_holdings=True):
        self.balance_calls += 1
        return {
            "cash_balance": self.balance,
            "securities_value": self.securities_value,
            "total_assets": self.total_assets,
            "total_pl": self.total_pl,
            "earning_rate": self.earning_rate,
            "holdings": self.holdings,
        }

    def is_holiday(self, date=None):
        return self.holiday

    def get_daily_order_executions(self, start_date=None, end_date=None, odno=None):
        if odno:
            key = odno.lstrip("0")
            return {k: v for k, v in self.executions.items() if k == key}
        return self.executions

    def order_stock(self, order_type, ticker, quantity):
        self._odno_seq += 1
        odno = str(10000 + self._odno_seq)
        self.orders.append(
            {
                "order_type": order_type,
                "ticker": ticker,
                "quantity": quantity,
                "odno": odno,
            }
        )
        if self.fail_order:
            from monkey.kis import KisClientError

            raise KisClientError("network failed")
        # Give every accepted order a distinct ODNO so a later finalize can match
        # fills per order (unless the canned response pins its own output/rt_cd).
        response = dict(self.response)
        if str(response.get("rt_cd")) == "0":
            response["output"] = {**response.get("output", {}), "ODNO": odno}
        return {"PDNO": ticker, "ORD_QTY": str(quantity)}, response

    def fill(self, order, *, quantity=None, avg_price=None, amount=None):
        """Register a KIS fill for an accepted (SUBMITTED) order, so a subsequent
        finalize moves it to EXECUTED. Defaults to a full fill at the estimated
        price. Returns ``self`` for chaining."""
        order.refresh_from_db()
        qty = order.requested_quantity if quantity is None else quantity
        price = order.estimated_price if avg_price is None else avg_price
        executed_amount = (qty * (price or 0)) if amount is None else amount
        odno = order.kis_order_id.lstrip("0")
        self.executions[odno] = {
            "executed_quantity": qty,
            "avg_price": price or 0,
            "executed_amount": executed_amount,
            # Mirror the real client's raw output1 fill record (saved on the Order).
            "raw": {
                "odno": odno,
                "tot_ccld_qty": str(qty),
                "avg_prvs": str(price or 0),
                "tot_ccld_amt": str(executed_amount),
            },
        }
        return self


def execute_order(client, order, *, quantity=None, avg_price=None, amount=None):
    """Test helper: fill ``order`` on ``client`` and finalize it (SUBMITTED →
    EXECUTED), returning the refreshed order."""
    client.fill(order, quantity=quantity, avg_price=avg_price, amount=amount)
    services.finalize_submitted_order(order, allow_partial=True, kis_client=client)
    order.refresh_from_db()
    return order


class FakeResponse:
    def __init__(self, data, status_ok=True, status_code=200, headers=None):
        self.data = data
        self.status_ok = status_ok
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if not self.status_ok:
            import requests

            raise requests.HTTPError("request failed")

    def json(self):
        return self.data


class KisClientTests(TestCase):
    def setUp(self):
        self.account = make_account()

    @mock.patch("monkey.kis.requests.post")
    def test_refresh_access_token_persists_token(self, post):
        post.return_value = FakeResponse(
            {
                "access_token": "token-1",
                "expires_in": 3600,
            }
        )

        token = KisClient(self.account).refresh_access_token()

        self.assertEqual(token.token, "token-1")
        self.assertEqual(KisAccessToken.objects.count(), 1)
        self.assertGreater(token.expires_at, timezone.now())

    @mock.patch("monkey.kis.requests.post")
    def test_order_stock_uses_market_buy_payload(self, post):
        KisAccessToken.objects.create(
            account=self.account,
            token="token-1",
            expires_at=timezone.now() + timedelta(hours=1),
        )
        post.return_value = FakeResponse({"rt_cd": "0", "output": {"ODNO": "1"}})

        payload, data = KisClient(self.account).order_stock(
            order_type=Order.OrderTypeChoices.BUY,
            ticker="005930",
            quantity=3,
        )

        self.assertEqual(payload["ORD_DVSN"], "01")
        self.assertEqual(payload["ORD_UNPR"], "0")
        self.assertEqual(payload["ORD_QTY"], "3")
        self.assertEqual(data["rt_cd"], "0")
        self.assertEqual(post.call_args.kwargs["headers"]["tr_id"], "VTTC0012U")

    @mock.patch("monkey.kis.requests.post")
    def test_order_stock_uses_market_sell_tr_id(self, post):
        KisAccessToken.objects.create(
            account=self.account,
            token="token-1",
            expires_at=timezone.now() + timedelta(hours=1),
        )
        post.return_value = FakeResponse({"rt_cd": "0", "output": {"ODNO": "1"}})

        KisClient(self.account).order_stock(
            order_type=Order.OrderTypeChoices.SELL,
            ticker="005930",
            quantity=2,
        )

        self.assertEqual(post.call_args.kwargs["headers"]["tr_id"], "VTTC0011U")


class MonkeyServiceTests(TestCase):
    def setUp(self):
        self.account = make_account()
        self.stock = Stock.objects.create(
            market="KOSPI",
            ticker="005930",
            name="Samsung Electronics",
        )

    def test_successful_buy_updates_local_ledger(self):
        monkey = Monkey.objects.create(
            name="A",
            balance=5000,
            initial_balance=5000,
        )
        client = FakeKisClient(price=1000)

        order = services.submit_monkey_order(
            monkey.id,
            self.stock.id,
            Order.OrderTypeChoices.BUY,
            3,
            kis_client=client,
        )

        # Acceptance alone doesn't move the ledger — only the order is recorded.
        monkey.refresh_from_db()
        self.assertEqual(order.status, Order.StatusChoices.SUBMITTED)
        self.assertEqual(monkey.balance, 5000)
        self.assertFalse(Holding.objects.filter(monkey=monkey).exists())
        # 주문가능금액 already reflects the reserved cost.
        self.assertEqual(services.available_cash(monkey), 2000)

        # The fill applies the ledger with KIS's real numbers.
        execute_order(client, order)
        monkey.refresh_from_db()
        holding = Holding.objects.get(monkey=monkey, stock=self.stock)
        self.assertEqual(order.status, Order.StatusChoices.EXECUTED)
        self.assertEqual(monkey.balance, 2000)
        self.assertEqual(holding.quantity, 3)
        self.assertEqual(order.executed_price, 1000)

    def test_buy_failure_when_balance_is_insufficient(self):
        monkey = Monkey.objects.create(name="A", balance=500, initial_balance=500)

        order = services.submit_monkey_order(
            monkey.id,
            self.stock.id,
            Order.OrderTypeChoices.BUY,
            1,
            kis_client=FakeKisClient(price=1000),
        )

        monkey.refresh_from_db()
        self.assertEqual(order.status, Order.StatusChoices.FAILED)
        self.assertIn("Insufficient monkey balance", order.failure_reason)
        self.assertEqual(monkey.balance, 500)
        self.assertFalse(Holding.objects.filter(monkey=monkey).exists())

    def test_sell_failure_when_holdings_are_insufficient(self):
        monkey = Monkey.objects.create(name="A", balance=0, initial_balance=0)
        Holding.objects.create(monkey=monkey, stock=self.stock, quantity=1)

        order = services.submit_monkey_order(
            monkey.id,
            self.stock.id,
            Order.OrderTypeChoices.SELL,
            2,
            kis_client=FakeKisClient(price=1000),
        )

        self.assertEqual(order.status, Order.StatusChoices.FAILED)
        self.assertIn("Insufficient monkey holdings", order.failure_reason)
        self.assertEqual(
            Holding.objects.get(monkey=monkey, stock=self.stock).quantity, 1
        )

    def test_kis_rejection_records_failed_order_without_local_update(self):
        monkey = Monkey.objects.create(name="A", balance=5000, initial_balance=5000)
        client = FakeKisClient(price=1000, response={"rt_cd": "1", "msg1": "rejected"})

        order = services.submit_monkey_order(
            monkey.id,
            self.stock.id,
            Order.OrderTypeChoices.BUY,
            1,
            kis_client=client,
        )

        monkey.refresh_from_db()
        self.assertEqual(order.status, Order.StatusChoices.FAILED)
        self.assertEqual(order.kis_response["rt_cd"], "1")
        self.assertEqual(monkey.balance, 5000)

    def test_random_buy_quantity_scales_with_balls(self):
        # balance 5000 / price 100 = 50 affordable; balls 0.4 → floor(50*0.4) = 20.
        monkey = Monkey.objects.create(
            name="A",
            balance=5000,
            initial_balance=5000,
            balls=0.4,
        )

        class Rng:
            def choice(self, values):
                return Order.OrderTypeChoices.BUY

        client = FakeKisClient(price=100)
        order = services.run_random_monkey_order(
            monkey.id,
            kis_client=client,
            rng=Rng(),
        )

        self.assertEqual(order.requested_quantity, 20)
        self.assertEqual(order.status, Order.StatusChoices.SUBMITTED)
        execute_order(client, order)
        self.assertEqual(order.status, Order.StatusChoices.EXECUTED)
        monkey.refresh_from_db()
        self.assertEqual(monkey.balance, 3000)

    def test_random_buy_skipped_when_amount_rounds_to_zero(self):
        # Price exceeds balance → 0 affordable → SKIPPED, no balance change.
        monkey = Monkey.objects.create(
            name="A", balance=500, initial_balance=500, balls=0.9
        )

        class Rng:
            def choice(self, values):
                return Order.OrderTypeChoices.BUY

        order = services.run_random_monkey_order(
            monkey.id,
            kis_client=FakeKisClient(price=1000),
            rng=Rng(),
        )

        self.assertEqual(order.status, Order.StatusChoices.SKIPPED)
        monkey.refresh_from_db()
        self.assertEqual(monkey.balance, 500)

    def test_random_sell_order_sells_balls_fraction_rounded_up(self):
        # holding 21 * balls 0.4 = 8.4 → ceil = 9 sold, 12 remain.
        monkey = Monkey.objects.create(
            name="A",
            balance=5000,
            initial_balance=5000,
            balls=0.4,
        )
        other_stock = Stock.objects.create(
            market="KOSPI",
            ticker="000660",
            name="SK Hynix",
        )
        Holding.objects.create(monkey=monkey, stock=self.stock, quantity=21)

        class Rng:
            def choice(self, values):
                return Order.OrderTypeChoices.SELL

        client = FakeKisClient(price=100)
        order = services.run_random_monkey_order(
            monkey.id,
            kis_client=client,
            rng=Rng(),
        )

        self.assertEqual(order.stock_id, self.stock.id)
        self.assertNotEqual(order.stock_id, other_stock.id)
        self.assertEqual(order.requested_quantity, 9)
        self.assertEqual(order.status, Order.StatusChoices.SUBMITTED)
        # Holding only drops once the sell fill is applied.
        self.assertEqual(
            Holding.objects.get(monkey=monkey, stock=self.stock).quantity, 21
        )
        execute_order(client, order)
        self.assertEqual(order.status, Order.StatusChoices.EXECUTED)
        self.assertEqual(
            Holding.objects.get(monkey=monkey, stock=self.stock).quantity, 12
        )

    def test_random_sell_of_single_share_holding_still_sells(self):
        # ceil(1 * any balls in (0,1]) == 1, so a 1-share holding always sells.
        monkey = Monkey.objects.create(
            name="A", balance=0, initial_balance=5000, balls=0.05
        )
        Holding.objects.create(monkey=monkey, stock=self.stock, quantity=1)

        class Rng:
            def choice(self, values):
                return Order.OrderTypeChoices.SELL

        client = FakeKisClient(price=100)
        order = services.run_random_monkey_order(
            monkey.id,
            kis_client=client,
            rng=Rng(),
        )

        self.assertEqual(order.requested_quantity, 1)
        self.assertEqual(order.status, Order.StatusChoices.SUBMITTED)
        execute_order(client, order)
        self.assertEqual(order.status, Order.StatusChoices.EXECUTED)
        self.assertFalse(
            Holding.objects.filter(monkey=monkey, stock=self.stock).exists()
        )

    def test_create_monkeys_assigns_pet_names_and_intervals(self):
        with mock.patch("monkey.names.random.choice", return_value="Arthur"):
            first = services.create_monkeys(
                self.account, count=1, starting_balance=1000
            )[0]
            second = services.create_monkeys(
                self.account, count=1, starting_balance=1000
            )[0]
            third = services.create_monkeys(
                self.account, count=1, starting_balance=1000
            )[0]

        self.assertEqual(first.name, "Arthur")
        self.assertEqual(second.name, "Arthur II")
        self.assertEqual(third.name, "Arthur III")
        for monkey in (first, second, third):
            self.assertTrue(60 <= monkey.order_interval_seconds <= 1800)
            self.assertTrue(services.TRAIT_FLOOR <= monkey.haste <= 1.0)
            self.assertTrue(services.TRAIT_FLOOR <= monkey.balls <= 1.0)

    def test_kill_monkey_transfers_holdings_to_system_monkey(self):
        services.set_trading_enabled(True)

        monkey = Monkey.objects.create(
            account=self.account, name="A", balance=1000, initial_balance=1000
        )
        other_stock = Stock.objects.create(
            market="KOSPI", ticker="000660", name="SK Hynix"
        )
        Holding.objects.create(monkey=monkey, stock=self.stock, quantity=2)
        Holding.objects.create(monkey=monkey, stock=other_stock, quantity=3)

        services.kill_monkey(monkey)

        monkey.refresh_from_db()
        self.assertFalse(monkey.is_active)
        self.assertIsNotNone(monkey.killed_at)
        # No selling happens here; holdings move to the system monkey instead.
        self.assertFalse(Holding.objects.filter(monkey=monkey).exists())
        self.assertFalse(Order.objects.exists())
        system_monkey = Monkey.objects.get(is_system=True)
        self.assertEqual(
            Holding.objects.get(monkey=system_monkey, stock=self.stock).quantity, 2
        )
        self.assertEqual(
            Holding.objects.get(monkey=system_monkey, stock=other_stock).quantity, 3
        )

    def test_kill_monkey_allowed_when_trading_disabled(self):
        # Killing only moves holdings (DB-only), so it no longer needs the gate.
        monkey = Monkey.objects.create(
            account=self.account, name="A", balance=1000, initial_balance=1000
        )
        Holding.objects.create(monkey=monkey, stock=self.stock, quantity=2)

        services.kill_monkey(monkey)

        monkey.refresh_from_db()
        self.assertFalse(monkey.is_active)
        self.assertIsNotNone(monkey.killed_at)
        self.assertFalse(Holding.objects.filter(monkey=monkey).exists())
        system_monkey = Monkey.objects.get(is_system=True)
        self.assertEqual(
            Holding.objects.get(monkey=system_monkey, stock=self.stock).quantity, 2
        )

    def test_auto_create_monkeys_uses_kis_cash_balance(self):
        Monkey.objects.create(
            account=self.account,
            name="A",
            balance=1_000_000,
            initial_balance=1_000_000,
        )
        fake_client = FakeKisClient(balance=3_000_000)

        with mock.patch("monkey.services.KisClient", return_value=fake_client):
            monkeys = services.auto_create_monkeys()

        self.assertEqual(len(monkeys), 2)
        self.assertEqual(Monkey.objects.filter(is_system=False).count(), 3)
        starting_balance = services.get_global_control().auto_create_starting_balance
        for monkey in monkeys:
            self.assertEqual(monkey.balance, starting_balance)

    def test_auto_create_uses_configured_starting_balance(self):
        control = services.get_global_control()
        control.auto_create_starting_balance = 500_000
        control.save(update_fields=["auto_create_starting_balance"])
        fake_client = FakeKisClient(balance=1_500_000)

        with mock.patch("monkey.services.KisClient", return_value=fake_client):
            monkeys = services.auto_create_monkeys()

        self.assertEqual(len(monkeys), 3)
        for monkey in monkeys:
            self.assertEqual(monkey.balance, 500_000)

    def test_create_monkeys_uses_configured_interval_range(self):
        control = services.get_global_control()
        control.auto_create_min_interval_seconds = 300
        control.auto_create_max_interval_seconds = 300
        control.save(
            update_fields=[
                "auto_create_min_interval_seconds",
                "auto_create_max_interval_seconds",
            ]
        )

        monkeys = services.create_monkeys(self.account, count=3, starting_balance=1000)

        for monkey in monkeys:
            self.assertEqual(monkey.order_interval_seconds, 300)


class OrphanedHoldingsTests(TestCase):
    def setUp(self):
        self.account = make_account()
        self.stock = Stock.objects.create(
            market="KOSPI", ticker="005930", name="Samsung Electronics"
        )

    def _monkey(self, **kw):
        kw.setdefault("balance", 0)
        kw.setdefault("initial_balance", 0)
        return Monkey.objects.create(account=self.account, **kw)

    def test_reconcile_holdings_absorbs_excess_into_system_monkey(self):
        fake_client = FakeKisClient(price=100, holdings={"005930": 5})

        result = services.reconcile_holdings(self.account, kis_client=fake_client)

        self.assertEqual(len(result["absorbed"]), 1)
        absorbed = result["absorbed"][0]
        self.assertEqual(absorbed["ticker"], "005930")
        self.assertEqual(absorbed["quantity"], 5)
        self.assertEqual(result["clamped"], [])

        # Excess is parked on the system monkey, not sold; its own task does that.
        system_monkey = Monkey.objects.get(is_system=True)
        self.assertEqual(
            Holding.objects.get(monkey=system_monkey, stock=self.stock).quantity, 5
        )
        self.assertFalse(Order.objects.exists())

    def test_reconcile_holdings_clamps_phantom_holdings(self):
        monkey = self._monkey(name="A")
        holding = Holding.objects.create(monkey=monkey, stock=self.stock, quantity=5)

        fake_client = FakeKisClient(price=100, holdings={"005930": 2})

        result = services.reconcile_holdings(self.account, kis_client=fake_client)

        self.assertEqual(result["absorbed"], [])
        self.assertEqual(len(result["clamped"]), 1)
        clamped = result["clamped"][0]
        self.assertEqual(clamped["ticker"], "005930")
        self.assertEqual(clamped["quantity"], 3)

        holding.refresh_from_db()
        self.assertEqual(holding.quantity, 2)
        self.assertEqual(Order.objects.count(), 0)

    def _succeeded(self, monkey, order_type, quantity):
        Order.objects.create(
            monkey=monkey,
            stock=self.stock,
            order_type=order_type,
            status=Order.StatusChoices.EXECUTED,
            requested_quantity=quantity,
            executed_quantity=quantity,
            estimated_price=100,
            executed_price=100,
        )

    def test_clamp_attributes_phantom_to_inconsistent_monkey(self):
        # monkey1: holds 5, order history nets 5 (consistent).
        monkey1 = self._monkey(name="A")
        Holding.objects.create(monkey=monkey1, stock=self.stock, quantity=5)
        self._succeeded(monkey1, Order.OrderTypeChoices.BUY, 5)
        # monkey2: holds 3, but orders net only 1 (partial-fill divergence of 2).
        monkey2 = self._monkey(name="B")
        Holding.objects.create(monkey=monkey2, stock=self.stock, quantity=3)
        self._succeeded(monkey2, Order.OrderTypeChoices.BUY, 1)

        # Real account = 6; local aggregate = 8 → phantom 2, all from monkey2.
        fake_client = FakeKisClient(price=100, holdings={"005930": 6})
        result = services.reconcile_holdings(self.account, kis_client=fake_client)

        self.assertEqual(result["absorbed"], [])
        self.assertEqual(len(result["clamped"]), 1)
        # Largest-first would have wrongly docked monkey1; attribution spares it.
        self.assertEqual(
            Holding.objects.get(monkey=monkey1, stock=self.stock).quantity, 5
        )
        self.assertEqual(
            Holding.objects.get(monkey=monkey2, stock=self.stock).quantity, 1
        )

    def test_clamp_falls_back_to_largest_when_no_divergence(self):
        # Both monkeys' holdings match their order history (no per-monkey signal).
        monkey1 = self._monkey(name="A")
        Holding.objects.create(monkey=monkey1, stock=self.stock, quantity=5)
        self._succeeded(monkey1, Order.OrderTypeChoices.BUY, 5)
        monkey2 = self._monkey(name="B")
        Holding.objects.create(monkey=monkey2, stock=self.stock, quantity=2)
        self._succeeded(monkey2, Order.OrderTypeChoices.BUY, 2)

        # Real = 6; local = 7 → phantom 1 from real drift → fallback docks largest.
        fake_client = FakeKisClient(price=100, holdings={"005930": 6})
        result = services.reconcile_holdings(self.account, kis_client=fake_client)

        self.assertEqual(result["absorbed"], [])
        self.assertEqual(len(result["clamped"]), 1)
        total = sum(
            Holding.objects.filter(stock=self.stock, quantity__gt=0).values_list(
                "quantity", flat=True
            )
        )
        self.assertEqual(total, 6)
        # Largest holder (monkey1) absorbs the unattributable drift.
        self.assertEqual(
            Holding.objects.get(monkey=monkey1, stock=self.stock).quantity, 4
        )
        self.assertEqual(
            Holding.objects.get(monkey=monkey2, stock=self.stock).quantity, 2
        )

    def test_reconcile_matches_prefixed_ticker_by_short_code(self):
        # KIS strips the leading prefix char from tickers longer than 6 chars, so
        # ETN "Q610039" reports as "610039" and warrant "J0669721F" as "0669721F".
        # reconciliation must match on short_code so prefixed holdings are neither
        # absorbed nor clamped.
        etn = Stock.objects.create(market="KOSDAQ", ticker="Q610039", name="Some ETN")
        warrant = Stock.objects.create(
            market="KOSPI", ticker="J0669721F", name="Some warrant"
        )
        self.assertEqual(etn.short_code, "610039")
        self.assertEqual(warrant.short_code, "0669721F")

        monkey = self._monkey(name="A")
        Holding.objects.create(monkey=monkey, stock=etn, quantity=4)
        Holding.objects.create(monkey=monkey, stock=warrant, quantity=7)

        fake_client = FakeKisClient(price=100, holdings={"610039": 4, "0669721F": 7})
        result = services.reconcile_holdings(self.account, kis_client=fake_client)

        self.assertEqual(result["absorbed"], [])
        self.assertEqual(result["clamped"], [])
        self.assertEqual(Holding.objects.get(monkey=monkey, stock=etn).quantity, 4)
        self.assertEqual(Holding.objects.get(monkey=monkey, stock=warrant).quantity, 7)

    def test_daily_maintenance_transfers_delisted_to_system_monkey(self):
        delisted_stock = Stock.objects.create(
            market="KOSPI", ticker="999999", name="Delisted Co", is_active=False
        )
        monkey = self._monkey(name="A", balance=1000, initial_balance=1000)
        Holding.objects.create(monkey=monkey, stock=self.stock, quantity=2)
        Holding.objects.create(monkey=monkey, stock=delisted_stock, quantity=1)

        fake_client = FakeKisClient(price=100, holdings={"005930": 2, "999999": 1})

        with mock.patch("monkey.services.KisClient", return_value=fake_client):
            result = services.run_daily_maintenance()

        self.assertEqual(result["delisted_transfers"], 1)
        # Active-stock holding is untouched; delisted one moves to the system monkey.
        self.assertEqual(
            Holding.objects.get(monkey=monkey, stock=self.stock).quantity, 2
        )
        self.assertFalse(
            Holding.objects.filter(monkey=monkey, stock=delisted_stock).exists()
        )
        system_monkey = Monkey.objects.get(is_system=True)
        self.assertEqual(
            Holding.objects.get(monkey=system_monkey, stock=delisted_stock).quantity,
            1,
        )
        self.assertFalse(Order.objects.exists())

    def test_daily_maintenance_runs_with_gate_closed(self):
        # No selling happens here, so it must work even when trading is disabled.
        services.set_trading_enabled(False)

        killed = self._monkey(
            name="Z", balance=0, initial_balance=1000, state=Monkey.State.DEAD
        )
        Holding.objects.create(monkey=killed, stock=self.stock, quantity=3)

        fake_client = FakeKisClient(price=100, holdings={"005930": 3})

        with mock.patch("monkey.services.KisClient", return_value=fake_client):
            result = services.run_daily_maintenance()

        self.assertEqual(result["killed_transfers"], 1)
        self.assertFalse(
            Holding.objects.filter(monkey=killed, stock=self.stock).exists()
        )
        system_monkey = Monkey.objects.get(is_system=True)
        self.assertEqual(
            Holding.objects.get(monkey=system_monkey, stock=self.stock).quantity, 3
        )

    def test_daily_maintenance_skips_while_market_open(self):
        # Killing during a session would break the index baseline, so the task
        # no-ops whenever the trading gate is open.
        services.set_trading_enabled(True)
        loser = self._monkey(name="L", balance=0, initial_balance=1000)

        from monkey.tasks import daily_maintenance

        result = daily_maintenance()

        self.assertEqual(result["output"], {"skipped": "market_open"})
        loser.refresh_from_db()
        self.assertEqual(loser.state, Monkey.State.ACTIVE)

    def test_daily_maintenance_culls_monkeys_inactive_for_three_trading_days(self):
        today = timezone.localdate()
        # Three trading days (today, -1, -2) registered via the index baseline.
        recent_days = [today - timedelta(days=offset) for offset in range(3)]
        for day in recent_days:
            MonkeyIndexBaseline.objects.create(
                date=day, base_index=10000.0, base_equity=1000
            )

        # All three monkeys predate the window (no grace exemption).
        loser = self._monkey(name="L", balance=0, initial_balance=1000)
        winner = self._monkey(name="W", balance=1000, initial_balance=1000)
        Monkey.objects.filter(pk__in=[loser.pk, winner.pk]).update(
            created_at=timezone.now() - timedelta(days=5)
        )
        # The winner traded successfully within the window; the loser did not.
        Order.objects.create(
            monkey=winner,
            stock=self.stock,
            order_type=Order.OrderTypeChoices.BUY,
            requested_quantity=1,
            status=Order.StatusChoices.EXECUTED,
        )

        # A fresh monkey (created today) is spared by the grace period.
        newbie = self._monkey(name="N", balance=0, initial_balance=1000)

        with mock.patch("monkey.services.KisClient", return_value=FakeKisClient()):
            result = services.run_daily_maintenance()

        self.assertEqual(result["killed"], 1)
        loser.refresh_from_db()
        winner.refresh_from_db()
        newbie.refresh_from_db()
        self.assertEqual(loser.state, Monkey.State.DEAD)
        self.assertEqual(winner.state, Monkey.State.ACTIVE)
        self.assertEqual(newbie.state, Monkey.State.ACTIVE)

    def test_kill_inactive_noop_without_enough_trading_history(self):
        # Fewer than INACTIVITY_KILL_DAYS baselines → no culling.
        MonkeyIndexBaseline.objects.create(
            date=timezone.localdate(), base_index=10000.0, base_equity=1000
        )
        loser = self._monkey(name="L", balance=0, initial_balance=1000)
        Monkey.objects.filter(pk=loser.pk).update(
            created_at=timezone.now() - timedelta(days=5)
        )

        self.assertEqual(services.kill_inactive_monkeys(), 0)
        loser.refresh_from_db()
        self.assertEqual(loser.state, Monkey.State.ACTIVE)

    def test_run_system_monkey_order_sells_full_quantity_and_keeps_zero_balance(self):
        system_monkey = services.get_or_create_system_monkey(self.account)
        Holding.objects.create(monkey=system_monkey, stock=self.stock, quantity=4)
        fake_client = FakeKisClient(price=100)

        orders = services.run_system_monkey_order(kis_client=fake_client)

        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order.order_type, Order.OrderTypeChoices.SELL)
        self.assertEqual(order.status, Order.StatusChoices.SUBMITTED)
        execute_order(fake_client, order)
        self.assertEqual(order.status, Order.StatusChoices.EXECUTED)
        self.assertEqual(order.executed_quantity, 4)
        self.assertFalse(
            Holding.objects.filter(monkey=system_monkey, stock=self.stock).exists()
        )
        # The system monkey never retains the sale proceeds.
        system_monkey.refresh_from_db()
        self.assertEqual(system_monkey.balance, 0)

    def test_run_system_monkey_order_no_holdings_is_noop(self):
        services.get_or_create_system_monkey(self.account)

        self.assertEqual(
            services.run_system_monkey_order(kis_client=FakeKisClient()), []
        )
        self.assertFalse(Order.objects.exists())

    def test_transfer_holdings_merges_into_existing_system_holding(self):
        system_monkey = services.get_or_create_system_monkey(self.account)
        Holding.objects.create(monkey=system_monkey, stock=self.stock, quantity=1)
        monkey = self._monkey(name="A")
        Holding.objects.create(monkey=monkey, stock=self.stock, quantity=2)

        services.transfer_holdings_to_system_monkey(monkey)

        self.assertFalse(Holding.objects.filter(monkey=monkey).exists())
        self.assertEqual(
            Holding.objects.get(monkey=system_monkey, stock=self.stock).quantity, 3
        )


class MonkeyApiTests(APITestCase):
    def test_public_can_read_monkeys_but_not_bulk_create(self):
        Monkey.objects.create(name="A", balance=1000, initial_balance=1000)

        read_response = self.client.get(reverse("monkey-list"))
        write_response = self.client.post(
            reverse("monkey-bulk-create"),
            {"count": 1, "starting_balance": 1000},
            format="json",
        )

        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(write_response.status_code, 401)

    def test_admin_can_bulk_create_monkeys(self):
        user = get_user_model().objects.create_user(
            username="admin",
            password="pw",
            is_staff=True,
        )
        self.client.force_authenticate(user)
        account = make_account()

        with mock.patch(
            "monkey.services.KisClient",
            return_value=FakeKisClient(balance=1_000_000),
        ):
            response = self.client.post(
                reverse("monkey-bulk-create"),
                {
                    "account": account.id,
                    "count": 2,
                    "starting_balance": 1000,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Monkey.objects.count(), 2)

    def test_force_kill_endpoint_requires_admin_and_liquidates(self):
        services.set_trading_enabled(True)

        account = make_account()
        stock = Stock.objects.create(
            market="KOSPI", ticker="005930", name="Samsung Electronics"
        )
        monkey = Monkey.objects.create(
            account=account, name="A", balance=1000, initial_balance=1000
        )
        Holding.objects.create(monkey=monkey, stock=stock, quantity=2)

        response = self.client.post(reverse("monkey-force-kill", args=[monkey.id]))
        self.assertEqual(response.status_code, 401)

        user = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user)

        with mock.patch(
            "monkey.services.KisClient", return_value=FakeKisClient(price=100)
        ):
            response = self.client.post(reverse("monkey-force-kill", args=[monkey.id]))

        self.assertEqual(response.status_code, 200)
        monkey.refresh_from_db()
        self.assertFalse(monkey.is_active)
        self.assertIsNotNone(monkey.killed_at)
        self.assertFalse(Holding.objects.filter(monkey=monkey, stock=stock).exists())

    def test_force_kill_endpoint_allowed_when_trading_disabled(self):
        # Killing now only transfers holdings to the system monkey, so the
        # endpoint succeeds even while trading is disabled.
        account = make_account()
        stock = Stock.objects.create(
            market="KOSPI", ticker="005930", name="Samsung Electronics"
        )
        monkey = Monkey.objects.create(
            account=account, name="A", balance=1000, initial_balance=1000
        )
        Holding.objects.create(monkey=monkey, stock=stock, quantity=2)

        user = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user)

        response = self.client.post(reverse("monkey-force-kill", args=[monkey.id]))

        self.assertEqual(response.status_code, 200)
        monkey.refresh_from_db()
        self.assertFalse(monkey.is_active)
        self.assertIsNotNone(monkey.killed_at)
        self.assertFalse(Holding.objects.filter(monkey=monkey).exists())

    def test_system_monkey_excluded_from_dashboard_and_monkey_list(self):
        account = make_account()
        system_monkey = services.get_or_create_system_monkey(account)
        Monkey.objects.create(
            account=account, name="A", balance=1000, initial_balance=1000
        )

        list_response = self.client.get(reverse("monkey-list"))
        self.assertNotIn(system_monkey.id, [item["id"] for item in list_response.data])

        self.assertEqual(len(services._alive_monkeys()), 1)

        snapshot_result = services.snapshot_all_monkeys()
        self.assertEqual(snapshot_result["snapshots"], 1)
        self.assertFalse(
            MonkeyDailySnapshot.objects.filter(monkey=system_monkey).exists()
        )

    def test_public_can_read_global_control_and_admin_can_patch(self):
        response = self.client.get(reverse("global-monkey-control-current"))
        self.assertEqual(response.status_code, 200)
        # Default: manual + holiday gates open, time gate closed → not enabled.
        self.assertEqual(response.data["enabled"], False)
        self.assertEqual(response.data["manual_enabled"], True)

        user = get_user_model().objects.create_user(
            username="admin",
            password="pw",
            is_staff=True,
        )
        self.client.force_authenticate(user)

        # Open the time gate (as the market-open task would).
        services.set_trading_enabled(True)
        get_after_time = self.client.get(reverse("global-monkey-control-current"))
        self.assertEqual(get_after_time.data["enabled"], True)

        # Admin closes the manual gate → trading disabled even with time gate open.
        patch_response = self.client.patch(
            reverse("global-monkey-control-current"),
            {"manual_enabled": False},
            format="json",
        )
        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(patch_response.data["manual_enabled"], False)
        self.assertEqual(patch_response.data["enabled"], False)

    def test_admin_can_patch_global_config_fields(self):
        # Auto-create config is global, edited via /global-monkey-control/current/.
        user = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user)
        response = self.client.patch(
            reverse("global-monkey-control-current"),
            {
                "auto_create_starting_balance": 250_000,
                "auto_create_min_interval_seconds": 120,
                "auto_create_max_interval_seconds": 600,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        control = services.get_global_control()
        self.assertEqual(control.auto_create_starting_balance, 250_000)
        self.assertEqual(control.auto_create_min_interval_seconds, 120)
        self.assertEqual(control.auto_create_max_interval_seconds, 600)

    def test_patch_rejects_interval_max_below_min(self):
        user = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user)
        response = self.client.patch(
            reverse("global-monkey-control-current"),
            {
                "auto_create_min_interval_seconds": 600,
                "auto_create_max_interval_seconds": 300,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("auto_create_max_interval_seconds", response.data)

    def test_read_only_gates_cannot_be_patched(self):
        user = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user)
        patch_response = self.client.patch(
            reverse("global-monkey-control-current"),
            {"time_enabled": True, "holiday_enabled": False},
            format="json",
        )
        self.assertEqual(patch_response.status_code, 200)
        control = services.get_global_control()
        # System-managed gates ignore client input.
        self.assertFalse(control.time_enabled)
        self.assertTrue(control.holiday_enabled)

    def test_task_catalog_requires_staff(self):
        non_staff = get_user_model().objects.create_user(
            username="viewer", password="pw"
        )
        self.client.force_authenticate(non_staff)
        response = self.client.get(reverse("global-monkey-control-tasks"))
        self.assertEqual(response.status_code, 403)

        user = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user)
        response = self.client.get(reverse("global-monkey-control-tasks"))
        self.assertEqual(response.status_code, 200)
        names = {entry["name"] for entry in response.data}
        self.assertIn("daily_maintenance", names)
        # Every entry carries a Korean label + description for the UI.
        self.assertTrue(
            all(entry["label"] and entry["description"] for entry in response.data)
        )
        # Dangerous tasks expose a non-empty warnings list; safe ones don't.
        by_name = {entry["name"]: entry for entry in response.data}
        self.assertTrue(by_name["daily_maintenance"]["dangerous"])
        self.assertTrue(len(by_name["daily_maintenance"]["warnings"]) >= 1)
        self.assertEqual(
            by_name["daily_maintenance"]["task"], "monkey.tasks.daily_maintenance"
        )
        self.assertFalse(by_name["snapshot_monkeys"]["dangerous"])
        self.assertEqual(by_name["snapshot_monkeys"]["warnings"], [])

    def test_run_task_enqueues_known_task(self):
        user = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user)
        with mock.patch(
            "monkey.task_catalog.monkey_tasks.daily_maintenance.delay"
        ) as delay:
            delay.return_value = mock.Mock(id="abc-123")
            response = self.client.post(
                reverse("global-monkey-control-run-task"),
                {"task": "daily_maintenance"},
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"task": "daily_maintenance", "id": "abc-123"})
        delay.assert_called_once_with()

    def test_run_task_rejects_unknown_task(self):
        user = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user)
        response = self.client.post(
            reverse("global-monkey-control-run-task"),
            {"task": "drop_database"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_run_task_requires_staff(self):
        non_staff = get_user_model().objects.create_user(
            username="viewer", password="pw"
        )
        self.client.force_authenticate(non_staff)
        response = self.client.post(
            reverse("global-monkey-control-run-task"),
            {"task": "run_system_monkey"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)


class TaskScheduleApiTests(APITestCase):
    """The migrations seed the crontab + interval PeriodicTasks we test against."""

    def setUp(self):
        from django_celery_beat.models import PeriodicTask

        self.check_holiday = PeriodicTask.objects.get(name="monkey.check_holiday")
        self.check_holiday.crontab.day_of_week = "1-5"
        self.check_holiday.crontab.save()
        self.admin = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )

    def test_schedules_list_is_crontab_only_and_time_sorted(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse("global-monkey-control-schedules"))
        self.assertEqual(response.status_code, 200)
        names = [row["name"] for row in response.data]
        # The interval-scheduled price poll is not a time-of-day task → excluded.
        self.assertNotIn("monkey.update_held_stock_prices", names)
        self.assertIn("monkey.check_holiday", names)
        # Ascending by time of day.
        times = [(row["hour"], row["minute"]) for row in response.data]
        self.assertEqual(times, sorted(times))
        # Korean label resolved from the task path.
        holiday = next(r for r in response.data if r["name"] == "monkey.check_holiday")
        self.assertEqual(holiday["label"], "휴장일 확인")
        self.assertEqual((holiday["hour"], holiday["minute"]), (8, 0))

    def test_schedules_require_staff(self):
        viewer = get_user_model().objects.create_user(username="viewer", password="pw")
        self.client.force_authenticate(viewer)
        response = self.client.get(reverse("global-monkey-control-schedules"))
        self.assertEqual(response.status_code, 403)

    def test_update_schedule_changes_time_and_keeps_day_of_week(self):
        from django_celery_beat.models import PeriodicTask

        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("global-monkey-control-update-schedule"),
            {"id": self.check_holiday.id, "hour": 7, "minute": 30},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["hour"], 7)
        self.assertEqual(response.data["minute"], 30)
        task = PeriodicTask.objects.get(pk=self.check_holiday.id)
        self.assertEqual(task.crontab.hour, "7")
        self.assertEqual(task.crontab.minute, "30")
        # Day-of-week is preserved across the reschedule.
        self.assertEqual(task.crontab.day_of_week, "1-5")

    def test_update_schedule_rejects_out_of_range_time(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("global-monkey-control-update-schedule"),
            {"id": self.check_holiday.id, "hour": 25, "minute": 0},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_update_schedule_unknown_id_returns_404(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("global-monkey-control-update-schedule"),
            {"id": 999999, "hour": 9, "minute": 0},
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_interval_schedules_exclude_per_monkey_tasks(self):
        from django_celery_beat.models import IntervalSchedule, PeriodicTask

        # A per-monkey order task (interval-scheduled) must not appear.
        interval, _ = IntervalSchedule.objects.get_or_create(every=90, period="seconds")
        PeriodicTask.objects.create(
            name="monkey.run.999",
            task="monkey.tasks.run_monkey",
            interval=interval,
        )
        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse("global-monkey-control-interval-schedules"))
        self.assertEqual(response.status_code, 200)
        names = [row["name"] for row in response.data]
        self.assertNotIn("monkey.run.999", names)
        self.assertIn("monkey.update_held_stock_prices", names)
        self.assertIn("monkey.finalize_order", names)
        # Ascending by cadence.
        everys = [row["every"] for row in response.data]
        self.assertEqual(everys, sorted(everys))
        price = next(
            r for r in response.data if r["name"] == "monkey.update_held_stock_prices"
        )
        self.assertEqual(price["label"], "보유 종목 시세 갱신")

    def test_update_interval_changes_cadence(self):
        from django_celery_beat.models import PeriodicTask

        price = PeriodicTask.objects.get(name="monkey.update_held_stock_prices")
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("global-monkey-control-update-interval"),
            {"id": price.id, "every": 300},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["every"], 300)
        price.refresh_from_db()
        self.assertEqual(price.interval.every, 300)

    def test_update_interval_rejects_per_monkey_task(self):
        from django_celery_beat.models import IntervalSchedule, PeriodicTask

        interval, _ = IntervalSchedule.objects.get_or_create(every=90, period="seconds")
        run_task = PeriodicTask.objects.create(
            name="monkey.run.123",
            task="monkey.tasks.run_monkey",
            interval=interval,
        )
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("global-monkey-control-update-interval"),
            {"id": run_task.id, "every": 300},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_update_interval_rejects_out_of_range(self):
        from django_celery_beat.models import PeriodicTask

        price = PeriodicTask.objects.get(name="monkey.update_held_stock_prices")
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("global-monkey-control-update-interval"),
            {"id": price.id, "every": 99999},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class MonkeyDailySnapshotTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(
            market="KOSPI", ticker="005930", name="Samsung Electronics"
        )

    def test_snapshot_all_monkeys_creates_one_per_monkey_matching_metrics(self):
        monkey_a = Monkey.objects.create(name="A", balance=5000, initial_balance=5000)
        monkey_b = Monkey.objects.create(name="B", balance=8000, initial_balance=10000)
        Holding.objects.create(monkey=monkey_b, stock=self.stock, quantity=2)
        Order.objects.create(
            monkey=monkey_b,
            stock=self.stock,
            order_type=Order.OrderTypeChoices.BUY,
            status=Order.StatusChoices.EXECUTED,
            requested_quantity=2,
            executed_quantity=2,
            estimated_price=1000,
            executed_price=1200,
        )

        target_date = date(2026, 6, 9)
        result = services.snapshot_all_monkeys(target_date)

        self.assertEqual(result, {"date": "2026-06-09", "snapshots": 2})
        self.assertEqual(MonkeyDailySnapshot.objects.count(), 2)

        for monkey in (monkey_a, monkey_b):
            snapshot = MonkeyDailySnapshot.objects.get(monkey=monkey, date=target_date)
            expected = build_monkey_metrics(monkey)
            self.assertEqual(snapshot.cash_balance, expected["cash_balance"])
            self.assertEqual(snapshot.holdings_value, expected["holdings_value"])
            self.assertEqual(snapshot.total_equity, expected["total_equity"])
            self.assertEqual(snapshot.total_pl, expected["total_pl"])
            self.assertEqual(snapshot.realized_pl, expected["realized_pl"])
            self.assertEqual(snapshot.unrealized_pl, expected["unrealized_pl"])
            self.assertEqual(snapshot.earning_ratio, expected["earning_ratio"])

    def test_snapshot_is_idempotent_per_day(self):
        monkey = Monkey.objects.create(name="A", balance=5000, initial_balance=5000)
        target_date = date(2026, 6, 9)

        services.snapshot_all_monkeys(target_date)
        Order.objects.create(
            monkey=monkey,
            stock=self.stock,
            order_type=Order.OrderTypeChoices.BUY,
            status=Order.StatusChoices.EXECUTED,
            requested_quantity=1,
            executed_quantity=1,
            estimated_price=1000,
            executed_price=1000,
        )
        services.snapshot_all_monkeys(target_date)

        self.assertEqual(
            MonkeyDailySnapshot.objects.filter(monkey=monkey, date=target_date).count(),
            1,
        )

    def test_earning_ratio_guards_zero_initial_balance(self):
        monkey = Monkey.objects.create(name="A", balance=0, initial_balance=0)
        self.assertEqual(build_monkey_metrics(monkey)["earning_ratio"], 0.0)


class DashboardSummaryApiTests(APITestCase):
    def setUp(self):
        self.stock = Stock.objects.create(
            market="KOSPI", ticker="005930", name="Samsung Electronics"
        )

    def test_dashboard_summary_is_public(self):
        response = self.client.get(reverse("dashboard-summary"))
        self.assertEqual(response.status_code, 200)

    def test_dashboard_summary_handles_no_monkeys(self):
        response = self.client.get(reverse("dashboard-summary"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["active_monkey_count"], 0)
        # No baseline/ticks yet: index sits at the cold-start base, flat vs "open".
        self.assertEqual(response.data["monkey_index"], services.MONKEY_INDEX_BASE)
        self.assertEqual(response.data["monkey_index_change"], 0.0)
        self.assertEqual(response.data["latest_orders"], [])

    def test_dashboard_summary_shape_and_values(self):
        active_monkey = Monkey.objects.create(
            name="A", balance=6000, initial_balance=5000, state=Monkey.State.ACTIVE
        )
        # An INACTIVE monkey still exists but must not count toward active_monkey_count.
        Monkey.objects.create(
            name="B", balance=4000, initial_balance=5000, state=Monkey.State.INACTIVE
        )

        orders = [
            Order.objects.create(
                monkey=active_monkey,
                stock=self.stock,
                order_type=Order.OrderTypeChoices.BUY,
                status=Order.StatusChoices.EXECUTED,
                requested_quantity=1,
                executed_quantity=1,
                estimated_price=1000,
                executed_price=1000,
            )
            for _ in range(7)
        ]
        # A failed order must never appear on the dashboard (success-only feed).
        Order.objects.create(
            monkey=active_monkey,
            stock=self.stock,
            order_type=Order.OrderTypeChoices.BUY,
            status=Order.StatusChoices.FAILED,
            requested_quantity=1,
        )
        # auto_now_add timestamps may tie at high resolution; force distinct, deterministic
        # ordering so the "-created_at" comparison below isn't flaky
        base_time = timezone.now()
        for offset, order in enumerate(orders):
            Order.objects.filter(pk=order.pk).update(
                created_at=base_time - timedelta(minutes=offset)
            )

        # Today's baseline (i=10,000, a=10,000) plus a tick at 10,500 → the index
        # reads 10,500, up 5% vs the day's open.
        from monkey.models import MonkeyIndexBaseline, MonkeyIndexTick

        MonkeyIndexBaseline.objects.create(
            date=timezone.localdate(), base_index=10000.0, base_equity=10000
        )
        MonkeyIndexTick.objects.create(value=10500.0)

        response = self.client.get(reverse("dashboard-summary"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["active_monkey_count"], 1)
        self.assertAlmostEqual(response.data["monkey_index"], 10500.0)
        self.assertAlmostEqual(response.data["monkey_index_open"], 10000.0)
        self.assertAlmostEqual(response.data["monkey_index_change"], 0.05)

        latest_orders = response.data["latest_orders"]
        # Up to 10 executed orders, newest first; the failed order is excluded.
        self.assertEqual(len(latest_orders), 7)
        self.assertTrue(all(order["status"] == "executed" for order in latest_orders))
        self.assertEqual(
            [order["id"] for order in latest_orders],
            list(
                Order.objects.filter(status=Order.StatusChoices.EXECUTED)
                .order_by("-created_at")
                .values_list("id", flat=True)[:10]
            ),
        )


class JWTAuthTests(APITestCase):
    def test_admin_can_obtain_and_use_jwt(self):
        get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )

        token_response = self.client.post(
            reverse("token_obtain_pair"),
            {"username": "admin", "password": "pw"},
            format="json",
        )
        self.assertEqual(token_response.status_code, 200)
        access = token_response.data["access"]

        response = self.client.get(
            reverse("kis-access-token-list"),
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )
        self.assertEqual(response.status_code, 200)

    def test_non_staff_cannot_access_admin_endpoints_via_jwt(self):
        get_user_model().objects.create_user(
            username="user", password="pw", is_staff=False
        )

        token_response = self.client.post(
            reverse("token_obtain_pair"),
            {"username": "user", "password": "pw"},
            format="json",
        )
        access = token_response.data["access"]

        response = self.client.get(
            reverse("kis-access-token-list"),
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )
        self.assertEqual(response.status_code, 403)

    def test_refresh_token_issues_new_access_token(self):
        get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        token_response = self.client.post(
            reverse("token_obtain_pair"),
            {"username": "admin", "password": "pw"},
            format="json",
        )
        refresh = token_response.data["refresh"]

        refresh_response = self.client.post(
            reverse("token_refresh"),
            {"refresh": refresh},
            format="json",
        )

        self.assertEqual(refresh_response.status_code, 200)
        self.assertIn("access", refresh_response.data)


class ThreeGateControlTests(TestCase):
    def test_effective_enabled_is_and_of_all_gates(self):
        control = services.get_global_control()
        # Fresh control: time gate closed by default.
        self.assertFalse(control.enabled)

        control.time_enabled = True
        control.holiday_enabled = True
        control.manual_enabled = True
        self.assertTrue(control.enabled)

        for gate in ("time_enabled", "holiday_enabled", "manual_enabled"):
            setattr(control, gate, False)
            self.assertFalse(control.enabled, f"{gate} off should disable trading")
            setattr(control, gate, True)

    def test_set_trading_enabled_only_flips_time_gate(self):
        services.set_holiday_closed(False)
        services.set_trading_enabled(True)
        control = services.get_global_control()
        self.assertTrue(control.time_enabled)
        self.assertTrue(control.holiday_enabled)
        self.assertTrue(control.manual_enabled)

    def test_set_holiday_closed_only_flips_holiday_gate(self):
        services.set_trading_enabled(True)
        services.set_holiday_closed(True)
        control = services.get_global_control()
        self.assertFalse(control.holiday_enabled)
        self.assertTrue(control.time_enabled)
        self.assertFalse(control.enabled)

    def test_check_holiday_task_sets_holiday_gate(self):
        make_account()
        with mock.patch(
            "monkey.services.KisClient",
            return_value=FakeKisClient(holiday=True),
        ):
            from monkey.tasks import check_holiday

            check_holiday()
        self.assertFalse(services.get_global_control().holiday_enabled)


class HoldingsBreakdownTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(
            market="KOSPI", ticker="005930", name="Samsung Electronics"
        )
        self.monkey = Monkey.objects.create(
            name="A", balance=1000, initial_balance=10000
        )

    def _buy(self, quantity, price, minutes_ago):
        order = Order.objects.create(
            monkey=self.monkey,
            stock=self.stock,
            order_type=Order.OrderTypeChoices.BUY,
            status=Order.StatusChoices.EXECUTED,
            requested_quantity=quantity,
            executed_quantity=quantity,
            estimated_price=price,
            executed_price=price,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes_ago)
        )
        return order

    def test_average_price_is_weighted_across_multiple_buys(self):
        from monkey.serializers import build_holdings_breakdown

        self._buy(2, 1000, minutes_ago=10)
        self._buy(3, 2000, minutes_ago=5)
        Holding.objects.create(monkey=self.monkey, stock=self.stock, quantity=5)
        self.stock.current_price = 3000
        self.stock.save(update_fields=["current_price"])

        breakdown = build_holdings_breakdown(self.monkey)[self.stock.id]
        # cost basis 2*1000 + 3*2000 = 8000 over 5 shares → avg 1600
        self.assertEqual(breakdown["average_price"], 1600)
        self.assertEqual(breakdown["current_price"], 3000)
        self.assertEqual(breakdown["evaluation"], 15000)
        self.assertEqual(breakdown["profit"], 7000)
        self.assertAlmostEqual(breakdown["profit_rate"], 7000 / 8000)

    def test_average_price_after_partial_sell(self):
        from monkey.serializers import build_holdings_breakdown

        self._buy(4, 1000, minutes_ago=10)
        sell = Order.objects.create(
            monkey=self.monkey,
            stock=self.stock,
            order_type=Order.OrderTypeChoices.SELL,
            status=Order.StatusChoices.EXECUTED,
            requested_quantity=1,
            executed_quantity=1,
            estimated_price=1500,
            executed_price=1500,
        )
        Order.objects.filter(pk=sell.pk).update(
            created_at=timezone.now() - timedelta(minutes=5)
        )
        Holding.objects.create(monkey=self.monkey, stock=self.stock, quantity=3)
        self.stock.current_price = 1000
        self.stock.save(update_fields=["current_price"])

        breakdown = build_holdings_breakdown(self.monkey)[self.stock.id]
        # Average stays 1000 after a partial sell at average cost.
        self.assertEqual(breakdown["average_price"], 1000)

    def test_update_held_stock_prices_writes_live_price(self):
        Holding.objects.create(monkey=self.monkey, stock=self.stock, quantity=2)
        result = services.update_held_stock_prices(kis_client=FakeKisClient(price=4321))
        self.stock.refresh_from_db()
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.stock.current_price, 4321)
        self.assertIsNotNone(self.stock.price_updated_at)

    def test_update_all_stock_prices_prices_every_active_stock(self):
        # self.stock is active and unheld; another active stock and a delisted one.
        other = Stock.objects.create(market="KOSDAQ", ticker="000660", name="SK Hynix")
        delisted = Stock.objects.create(
            market="KOSPI", ticker="999999", name="Delisted", is_active=False
        )

        # Not gated on the market switch (left closed here).
        result = services.update_all_stock_prices(kis_client=FakeKisClient(price=4321))

        self.assertEqual(result, {"updated": 2, "failed": 0})
        self.stock.refresh_from_db()
        other.refresh_from_db()
        delisted.refresh_from_db()
        self.assertEqual(self.stock.current_price, 4321)
        self.assertEqual(other.current_price, 4321)
        # Delisted stocks are skipped (left at their default, unpriced).
        self.assertIsNone(delisted.current_price)


class ExecutionReconciliationTests(TestCase):
    def setUp(self):
        self.account = make_account()
        self.stock = Stock.objects.create(
            market="KOSPI", ticker="005930", name="Samsung Electronics"
        )
        self.monkey = Monkey.objects.create(
            account=self.account, name="A", balance=1000, initial_balance=10000
        )
        services.set_trading_enabled(True)

    def test_after_close_finalize_applies_real_fill(self):
        # After close, a pending order is committed with KIS's actual numbers.
        services.set_trading_enabled(False)
        order = Order.objects.create(
            monkey=self.monkey,
            stock=self.stock,
            order_type=Order.OrderTypeChoices.BUY,
            status=Order.StatusChoices.SUBMITTED,
            requested_quantity=1,
            estimated_price=1000,
            kis_order_id="0000012345",
        )
        client = FakeKisClient(
            executions={
                "12345": {
                    "executed_quantity": 1,
                    "avg_price": 1050,
                    "executed_amount": 1050,
                    "raw": {"odno": "12345", "tot_ccld_qty": "1", "avg_prvs": "1050"},
                }
            }
        )
        with mock.patch("monkey.services.KisClient", return_value=client):
            result = services.finalize_orders()

        order.refresh_from_db()
        self.monkey.refresh_from_db()
        self.assertEqual(result["finalized"], 1)
        self.assertEqual(order.status, Order.StatusChoices.EXECUTED)
        self.assertEqual(order.executed_price, 1050)
        self.assertEqual(order.executed_quantity, 1)
        # The raw output1 fill record is captured on the order for auditing.
        self.assertEqual(order.execution_detail["odno"], "12345")
        # Cash debited by KIS's exact total amount (1000 − 1050).
        self.assertEqual(self.monkey.balance, -50)
        self.assertEqual(
            Holding.objects.get(monkey=self.monkey, stock=self.stock).quantity, 1
        )

    def test_partial_fill_waits_midsession_then_settles_at_close(self):
        # A buy of 10 @ ~1000 reserves 10,000; only 4 actually fill.
        monkey = Monkey.objects.create(name="P", balance=20000, initial_balance=20000)
        services.set_trading_enabled(True)
        client = FakeKisClient(price=1000)
        order = services.submit_monkey_order(
            monkey.id,
            self.stock.id,
            Order.OrderTypeChoices.BUY,
            10,
            kis_client=client,
        )
        self.assertEqual(order.status, Order.StatusChoices.SUBMITTED)

        # Mid-session: only a partial fill is reported — the order stays pending.
        client.fill(order, quantity=4, avg_price=1000, amount=4000)
        services.finalize_order(kis_client=client)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.StatusChoices.SUBMITTED)
        monkey.refresh_from_db()
        self.assertEqual(monkey.balance, 20000)  # untouched while pending

        # After close: the partial is committed and the unfilled reserve is freed.
        services.set_trading_enabled(False)
        result = services.finalize_orders(kis_client=client)
        order.refresh_from_db()
        monkey.refresh_from_db()
        self.assertEqual(result["finalized"], 1)
        self.assertEqual(order.status, Order.StatusChoices.EXECUTED)
        self.assertEqual(order.executed_quantity, 4)
        # Only the 4 shares' real cost left the ledger — no starvation.
        self.assertEqual(monkey.balance, 20000 - 4000)
        self.assertEqual(services.available_cash(monkey), 16000)
        self.assertEqual(
            Holding.objects.get(monkey=monkey, stock=self.stock).quantity, 4
        )

    def test_zero_fill_settles_with_no_ledger_change(self):
        # An accepted order that never executes (halt / no volume) settles flat.
        monkey = Monkey.objects.create(name="Z", balance=5000, initial_balance=5000)
        services.set_trading_enabled(True)
        client = FakeKisClient(price=1000)
        order = services.submit_monkey_order(
            monkey.id,
            self.stock.id,
            Order.OrderTypeChoices.BUY,
            3,
            kis_client=client,
        )
        # No fill registered on the client at all.
        services.set_trading_enabled(False)
        result = services.finalize_orders(kis_client=client)

        order.refresh_from_db()
        monkey.refresh_from_db()
        self.assertEqual(result["finalized"], 1)
        self.assertEqual(order.status, Order.StatusChoices.EXECUTED)
        self.assertEqual(order.executed_quantity, 0)
        self.assertEqual(monkey.balance, 5000)
        self.assertFalse(Holding.objects.filter(monkey=monkey).exists())
        # Reserve released now that the order left the pending state.
        self.assertEqual(services.available_cash(monkey), 5000)

    def test_pending_buy_blocks_overspending(self):
        # A monkey can't queue a second buy beyond its available (reserved) cash.
        monkey = Monkey.objects.create(name="O", balance=3000, initial_balance=3000)
        services.set_trading_enabled(True)
        client = FakeKisClient(price=1000)
        first = services.submit_monkey_order(
            monkey.id, self.stock.id, Order.OrderTypeChoices.BUY, 2, kis_client=client
        )
        self.assertEqual(first.status, Order.StatusChoices.SUBMITTED)
        self.assertEqual(services.available_cash(monkey), 1000)

        # Second buy of 2 (cost 2000) exceeds the 1000 still available → rejected.
        second = services.submit_monkey_order(
            monkey.id, self.stock.id, Order.OrderTypeChoices.BUY, 2, kis_client=client
        )
        self.assertEqual(second.status, Order.StatusChoices.FAILED)
        self.assertIn("Insufficient monkey balance", second.failure_reason)


class CandlestickApiTests(APITestCase):
    def test_candlesticks_endpoint_is_public_and_buckets_by_unit(self):
        from monkey.models import MonkeyIndexTick

        ticks = [
            MonkeyIndexTick.objects.create(value=value)
            for value in (10100.0, 10300.0, 10200.0)
        ]
        base = timezone.now()
        for offset, tick in enumerate(ticks):
            MonkeyIndexTick.objects.filter(pk=tick.pk).update(
                recorded_at=base - timedelta(minutes=2 - offset)
            )

        response = self.client.get(reverse("candlesticks"), {"unit": "1d"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        candle = response.data[0]
        self.assertEqual(candle["open"], 10100.0)
        self.assertEqual(candle["high"], 10300.0)
        self.assertEqual(candle["low"], 10100.0)
        self.assertEqual(candle["close"], 10200.0)
        self.assertIn("time", candle)

    def test_intraday_buckets_are_shifted_to_kst(self):
        from monkey.models import MonkeyIndexTick

        tick = MonkeyIndexTick.objects.create(value=10100.0)
        known = timezone.now().replace(microsecond=0)
        MonkeyIndexTick.objects.filter(pk=tick.pk).update(recorded_at=known)

        candles = services.build_index_candlesticks(unit="1m")

        shifted = int(known.timestamp()) + services.KST_OFFSET_SECONDS
        expected = shifted - (shifted % 60)
        self.assertEqual(candles[-1]["time"], expected)

    def _seed_minute_ticks(self, count):
        from monkey.models import MonkeyIndexTick

        base = timezone.now().replace(second=0, microsecond=0)
        for offset in range(count):
            tick = MonkeyIndexTick.objects.create(value=10000.0 + offset)
            MonkeyIndexTick.objects.filter(pk=tick.pk).update(
                recorded_at=base - timedelta(minutes=count - 1 - offset)
            )

    def test_candlesticks_before_returns_only_older(self):
        self._seed_minute_ticks(5)
        all_candles = services.build_index_candlesticks(unit="1m")
        self.assertEqual(len(all_candles), 5)
        cutoff = all_candles[3]["time"]

        response = self.client.get(
            reverse("candlesticks"), {"unit": "1m", "before": cutoff}
        )
        self.assertEqual(response.status_code, 200)
        times = [candle["time"] for candle in response.data]
        self.assertTrue(all(time < cutoff for time in times))
        self.assertEqual(times, [candle["time"] for candle in all_candles[:3]])

    def test_candlesticks_before_respects_limit(self):
        self._seed_minute_ticks(5)
        all_candles = services.build_index_candlesticks(unit="1m")
        cutoff = all_candles[4]["time"]

        # before excludes the newest candle; limit returns the 2 just older.
        page = services.build_index_candlesticks(unit="1m", limit=2, before=cutoff)
        self.assertEqual(
            [candle["time"] for candle in page],
            [candle["time"] for candle in all_candles[2:4]],
        )


class IndexReturnsApiTests(APITestCase):
    def _tick(self, value, days_ago):
        from monkey.models import MonkeyIndexTick

        tick = MonkeyIndexTick.objects.create(value=value)
        MonkeyIndexTick.objects.filter(pk=tick.pk).update(
            recorded_at=timezone.now() - timedelta(days=days_ago)
        )
        return tick

    def test_index_returns_computes_available_periods(self):
        self._tick(800.0, 8)  # week reference (close on/before 7 days ago)
        self._tick(1000.0, 1)  # day reference (yesterday's close)
        self._tick(1100.0, 0)  # current

        returns = services.build_index_returns()
        today = timezone.localdate()
        periods = returns["periods"]

        self.assertAlmostEqual(returns["current"], 1100.0)

        self.assertEqual(
            periods["day"]["date"], (today - timedelta(days=1)).isoformat()
        )
        self.assertAlmostEqual(periods["day"]["index"], 1000.0)
        self.assertAlmostEqual(periods["day"]["rate"], 0.1)

        self.assertEqual(
            periods["week"]["date"], (today - timedelta(days=8)).isoformat()
        )
        self.assertAlmostEqual(periods["week"]["index"], 800.0)
        self.assertAlmostEqual(periods["week"]["rate"], 1100 / 800 - 1)

        # No data a month/quarter back yet.
        self.assertIsNone(periods["month"])
        self.assertIsNone(periods["quarter"])

    def test_index_returns_endpoint_is_public_with_all_periods(self):
        response = self.client.get(reverse("index-returns"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.data.keys()), {"current", "periods"})
        self.assertEqual(
            set(response.data["periods"].keys()), {"day", "week", "month", "quarter"}
        )


class MonkeyIndexTests(TestCase):
    def test_baseline_cold_starts_at_base_value(self):
        Monkey.objects.create(name="A", balance=10000, initial_balance=10000)

        baseline = services.capture_index_baseline()

        self.assertEqual(baseline.base_index, services.MONKEY_INDEX_BASE)
        self.assertEqual(baseline.base_equity, 10000)

    def test_baseline_carries_forward_yesterday_close(self):
        from monkey.models import MonkeyIndexTick

        Monkey.objects.create(name="A", balance=10000, initial_balance=10000)
        tick = MonkeyIndexTick.objects.create(value=10500.0)
        yesterday = timezone.now() - timedelta(days=1)
        MonkeyIndexTick.objects.filter(pk=tick.pk).update(recorded_at=yesterday)

        baseline = services.capture_index_baseline()

        self.assertEqual(baseline.base_index, 10500.0)

    def test_record_tick_skips_without_baseline(self):
        self.assertEqual(services._record_index_tick(), {"baseline": False})

    def test_record_tick_scales_index_by_equity_ratio(self):
        from monkey.models import MonkeyIndexBaseline, MonkeyIndexTick

        # Alive equity now is 12,000; baseline a=10,000, i=10,000 → 10,000 * 1.2.
        Monkey.objects.create(name="A", balance=12000, initial_balance=10000)
        MonkeyIndexBaseline.objects.create(
            date=timezone.localdate(), base_index=10000.0, base_equity=10000
        )

        result = services._record_index_tick()

        self.assertAlmostEqual(result["value"], 12000.0)
        self.assertAlmostEqual(
            MonkeyIndexTick.objects.latest("recorded_at").value, 12000.0
        )

    def test_record_tick_flat_when_base_equity_zero(self):
        from monkey.models import MonkeyIndexBaseline

        MonkeyIndexBaseline.objects.create(
            date=timezone.localdate(), base_index=10000.0, base_equity=0
        )

        result = services._record_index_tick()

        self.assertAlmostEqual(result["value"], 10000.0)


class MonkeyStateLifecycleTests(TestCase):
    def _task(self, monkey):
        from django_celery_beat.models import PeriodicTask

        return PeriodicTask.objects.filter(name=f"monkey.run.{monkey.id}").first()

    def test_active_monkey_task_disabled_while_gate_closed(self):
        monkey = Monkey.objects.create(name="A", balance=1000, initial_balance=1000)
        task = self._task(monkey)
        self.assertIsNotNone(task)
        self.assertFalse(task.enabled)  # default gate (time) is closed
        self.assertEqual(task.expire_seconds, min(monkey.order_interval_seconds, 120))

    def test_active_monkey_task_enabled_when_created_with_gate_open(self):
        services.set_trading_enabled(True)
        services.set_holiday_closed(False)
        monkey = Monkey.objects.create(name="A", balance=1000, initial_balance=1000)
        self.assertTrue(self._task(monkey).enabled)

    def test_killing_monkey_deletes_its_task(self):
        services.set_trading_enabled(True)
        monkey = Monkey.objects.create(name="A", balance=1000, initial_balance=1000)
        self.assertIsNotNone(self._task(monkey))

        with mock.patch(
            "monkey.services.KisClient", return_value=FakeKisClient(price=100)
        ):
            services.kill_monkey(monkey)

        monkey.refresh_from_db()
        self.assertEqual(monkey.state, Monkey.State.DEAD)
        self.assertIsNone(self._task(monkey))

    def test_sync_enables_active_disables_inactive(self):
        active = Monkey.objects.create(name="A", balance=1000, initial_balance=1000)
        paused = Monkey.objects.create(name="B", balance=1000, initial_balance=1000)
        paused.state = Monkey.State.INACTIVE
        paused.save(update_fields=["state"])

        services.set_trading_enabled(True)
        services.set_holiday_closed(False)
        services.sync_monkey_periodic_tasks()

        self.assertTrue(self._task(active).enabled)
        self.assertFalse(self._task(paused).enabled)

        services.set_trading_enabled(False)
        services.sync_monkey_periodic_tasks()
        self.assertFalse(self._task(active).enabled)

    def test_sync_toggles_market_hours_tasks_with_market_state(self):
        from django_celery_beat.models import PeriodicTask

        market_hours_tasks = [
            "monkey.update_held_stock_prices",
            "monkey.run_system",
            "monkey.finalize_order",
        ]

        services.set_trading_enabled(True)
        services.set_holiday_closed(False)
        services.sync_monkey_periodic_tasks()
        for name in market_hours_tasks:
            self.assertTrue(
                PeriodicTask.objects.get(name=name).enabled, f"{name} should be on"
            )

        # Market closed → every market-hours task (incl. finalize) is disabled.
        services.set_trading_enabled(False)
        services.sync_monkey_periodic_tasks()
        for name in market_hours_tasks:
            self.assertFalse(
                PeriodicTask.objects.get(name=name).enabled, f"{name} should be off"
            )


class CashAllocationTests(TestCase):
    def setUp(self):
        self.account = make_account()

    def test_unallocated_cash_excludes_dead_monkeys(self):
        Monkey.objects.create(
            account=self.account,
            name="alive",
            balance=1_000_000,
            initial_balance=1_000_000,
        )
        Monkey.objects.create(
            account=self.account,
            name="dead",
            balance=1_000_000,
            initial_balance=1_000_000,
            state=Monkey.State.DEAD,
        )
        client = FakeKisClient(balance=3_000_000)

        self.assertEqual(
            services.unallocated_cash(self.account, kis_client=client), 2_000_000
        )

    def test_create_monkeys_checked_rejects_when_insufficient(self):
        client = FakeKisClient(balance=500)
        with self.assertRaises(services.InsufficientCashError):
            services.create_monkeys_checked(
                self.account, count=1, starting_balance=1000, kis_client=client
            )
        self.assertEqual(Monkey.objects.count(), 0)


class CachedPriceOrderTests(TestCase):
    def test_buy_uses_cached_price_without_live_fetch(self):
        stock = Stock.objects.create(
            market="KOSPI", ticker="005930", name="Samsung", current_price=1234
        )
        monkey = Monkey.objects.create(name="A", balance=5000, initial_balance=5000)

        class NoFetchClient(FakeKisClient):
            def get_stock_price(self, ticker):
                raise AssertionError("should not fetch live price when cached exists")

        client = NoFetchClient()
        order = services.submit_monkey_order(
            monkey.id,
            stock.id,
            Order.OrderTypeChoices.BUY,
            1,
            kis_client=client,
        )

        # Cached price is used as the estimate; the fill confirms it.
        self.assertEqual(order.status, Order.StatusChoices.SUBMITTED)
        self.assertEqual(order.estimated_price, 1234)
        execute_order(client, order)
        self.assertEqual(order.status, Order.StatusChoices.EXECUTED)
        self.assertEqual(order.executed_price, 1234)


class KisTransientRetryTests(TestCase):
    def setUp(self):
        self.account = make_account()
        KisAccessToken.objects.create(
            account=self.account,
            token="seed",
            expires_at=timezone.now() + timedelta(hours=1),
        )

    @mock.patch("monkey.kis.requests.post")
    def test_retries_on_server_error_then_succeeds(self, post):
        post.side_effect = [
            FakeResponse({}, status_code=500),
            FakeResponse({"access_token": "t-ok", "expires_in": 3600}),
        ]
        token = KisClient(self.account).refresh_access_token()
        self.assertEqual(token.token, "t-ok")
        self.assertEqual(post.call_count, 2)

    @mock.patch("monkey.kis.requests.post")
    def test_retries_on_rate_limit_message(self, post):
        post.side_effect = [
            FakeResponse(
                {"msg1": "초당 거래건수를 초과하였습니다.", "msg_cd": "EGW00201"}
            ),
            FakeResponse({"access_token": "t-ok", "expires_in": 3600}),
        ]
        token = KisClient(self.account).refresh_access_token()
        self.assertEqual(token.token, "t-ok")
        self.assertEqual(post.call_count, 2)

    @mock.patch("monkey.kis.requests.post")
    def test_raises_after_exhausting_retries(self, post):
        post.return_value = FakeResponse({}, status_code=500)
        from monkey.kis import KisClientError

        with self.assertRaises(KisClientError):
            KisClient(self.account).refresh_access_token()
        # KIS_MAX_RETRIES (3) + 1 initial attempt
        self.assertEqual(post.call_count, 4)


class MonkeyListQueryCountTests(APITestCase):
    """The monkey list serializes per-monkey metrics + holdings breakdown. These
    are batched via prefetch in MonkeyViewSet.get_queryset, so the query count
    must not grow with the number of *stocks* each monkey holds (the old N+1)."""

    def _give_holdings(self, monkey, stocks):
        for stock in stocks:
            Holding.objects.create(monkey=monkey, stock=stock, quantity=2)
            Order.objects.create(
                monkey=monkey,
                stock=stock,
                order_type=Order.OrderTypeChoices.BUY,
                status=Order.StatusChoices.EXECUTED,
                requested_quantity=2,
                executed_quantity=2,
                estimated_price=500,
                executed_price=500,
            )

    def _count_list_queries(self):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse("monkey-list"))
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_list_query_count_independent_of_holdings_per_monkey(self):
        stocks = [
            Stock.objects.create(market="KOSPI", ticker=f"00000{i}", name=f"S{i}")
            for i in range(5)
        ]
        monkey_a = Monkey.objects.create(name="A", balance=1000, initial_balance=10000)
        monkey_b = Monkey.objects.create(name="B", balance=1000, initial_balance=10000)

        # Each monkey holds a single stock.
        self._give_holdings(monkey_a, stocks[:1])
        self._give_holdings(monkey_b, stocks[:1])
        baseline = self._count_list_queries()

        # Now each monkey holds five stocks. With prefetching, the per-stock FIFO
        # work adds no queries, so the total is unchanged.
        self._give_holdings(monkey_a, stocks[1:])
        self._give_holdings(monkey_b, stocks[1:])
        self.assertEqual(self._count_list_queries(), baseline)


class AccountCacheTests(TestCase):
    def setUp(self):
        self.account = make_account()

    def test_build_account_summary_reads_cache_without_calling_kis(self):
        KisAccountCache.objects.create(
            account=self.account,
            cash_balance=500_000,
            securities_value=200_000,
            total_assets=700_000,
            total_pl=50_000,
            earning_rate=0.07,
        )
        Monkey.objects.create(
            account=self.account, name="A", balance=100_000, initial_balance=100_000
        )
        client = FakeKisClient(balance=999)

        summary = services.build_account_summary(self.account, kis_client=client)

        # Served entirely from the cache — KIS is never queried.
        self.assertEqual(client.balance_calls, 0)
        self.assertEqual(summary["kis_cash_balance"], 500_000)
        self.assertEqual(summary["kis_total_assets"], 700_000)
        self.assertEqual(summary["kis_earning_rate"], 0.07)
        # Unallocated cash is derived locally from the cached cash balance.
        self.assertEqual(summary["unallocated_cash"], 400_000)
        self.assertEqual(summary["monkey_count"], 1)

    def test_build_account_summary_cold_cache_falls_back_to_live_fetch(self):
        client = FakeKisClient(
            balance=300_000,
            securities_value=100_000,
            total_assets=400_000,
            total_pl=10_000,
            earning_rate=0.025,
        )

        summary = services.build_account_summary(self.account, kis_client=client)

        # One live fetch populated the cache, which is now served back.
        self.assertEqual(client.balance_calls, 1)
        self.assertEqual(summary["kis_cash_balance"], 300_000)
        cache = KisAccountCache.objects.get(account=self.account)
        self.assertEqual(cache.total_assets, 400_000)

    def test_update_held_stock_prices_refreshes_account_cache(self):
        stock = Stock.objects.create(market="KOSPI", ticker="005930", name="Samsung")
        monkey = Monkey.objects.create(
            account=self.account, name="A", balance=1000, initial_balance=10000
        )
        Holding.objects.create(monkey=monkey, stock=stock, quantity=2)
        services.set_trading_enabled(True)

        client = FakeKisClient(
            price=4321,
            balance=600_000,
            securities_value=150_000,
            total_assets=750_000,
        )
        with mock.patch("monkey.services.KisClient", return_value=client):
            result = services.update_held_stock_prices()

        self.assertTrue(result["cache_refreshed"])
        cache = KisAccountCache.objects.get(account=self.account)
        self.assertEqual(cache.cash_balance, 600_000)
        self.assertEqual(cache.total_assets, 750_000)


class SystemMonkeyVisibilityTests(APITestCase):
    def setUp(self):
        self.account = make_account()
        self.system = services.get_or_create_system_monkey(self.account)
        self.trader = Monkey.objects.create(
            account=self.account, name="A", balance=1000, initial_balance=1000
        )

    def test_guest_list_excludes_system_monkey(self):
        response = self.client.get(reverse("monkey-list"))
        self.assertEqual(response.status_code, 200)
        ids = {row["id"] for row in response.data}
        self.assertIn(self.trader.id, ids)
        self.assertNotIn(self.system.id, ids)

    def test_staff_list_includes_system_monkey(self):
        user = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user)

        response = self.client.get(reverse("monkey-list"))
        self.assertEqual(response.status_code, 200)
        rows = {row["id"]: row for row in response.data}
        self.assertIn(self.system.id, rows)
        self.assertTrue(rows[self.system.id]["is_system"])
        self.assertFalse(rows[self.trader.id]["is_system"])

    def test_summary_excludes_system_monkey_even_for_staff(self):
        user = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user)

        response = self.client.get(reverse("monkey-summary"))
        self.assertEqual(response.status_code, 200)
        ids = {row["id"] for row in response.data}
        self.assertNotIn(self.system.id, ids)


class TraitTests(TestCase):
    def test_derive_interval_interpolates_across_min_max(self):
        control = services.get_global_control()
        control.auto_create_min_interval_seconds = 100
        control.auto_create_max_interval_seconds = 1100
        control.save(
            update_fields=[
                "auto_create_min_interval_seconds",
                "auto_create_max_interval_seconds",
            ]
        )
        # haste=1 → fastest (min); haste=0 → slowest (max); 0.5 → midpoint.
        self.assertEqual(services.derive_interval(1.0, control), 100)
        self.assertEqual(services.derive_interval(0.0, control), 1100)
        self.assertEqual(services.derive_interval(0.5, control), 600)

    def test_mate_traits_stay_in_range_and_center_on_parent_mean(self):
        import random as _random

        parent_a = Monkey(name="A", balance=0, haste=0.4, balls=0.6)
        parent_b = Monkey(name="B", balance=0, haste=0.6, balls=0.8)
        rng = _random.Random(1234)

        hastes = []
        ballss = []
        for _ in range(400):
            haste, balls = services.mate_traits(parent_a, parent_b, rng)
            self.assertTrue(services.TRAIT_FLOOR <= haste <= 1.0)
            self.assertTrue(services.TRAIT_FLOOR <= balls <= 1.0)
            hastes.append(haste)
            ballss.append(balls)

        # Averages should sit near the parents' midpoints (0.5 and 0.7).
        self.assertAlmostEqual(sum(hastes) / len(hastes), 0.5, delta=0.1)
        self.assertAlmostEqual(sum(ballss) / len(ballss), 0.7, delta=0.1)

    def test_auto_create_breeds_traits_from_parents(self):
        # Two distinctive parents already alive → children are bred from them.
        account = make_account()
        Monkey.objects.create(
            account=account,
            name="P1",
            balance=1_000_000,
            initial_balance=1_000_000,
            haste=0.5,
            balls=0.5,
        )
        Monkey.objects.create(
            account=account,
            name="P2",
            balance=1_000_000,
            initial_balance=1_000_000,
            haste=0.5,
            balls=0.5,
        )
        fake_client = FakeKisClient(balance=4_000_000)

        with mock.patch("monkey.services.KisClient", return_value=fake_client):
            children = services.auto_create_monkeys()

        self.assertEqual(len(children), 2)
        control = services.get_global_control()
        for child in children:
            self.assertTrue(services.TRAIT_FLOOR <= child.haste <= 1.0)
            self.assertTrue(services.TRAIT_FLOOR <= child.balls <= 1.0)
            self.assertEqual(
                child.order_interval_seconds,
                services.derive_interval(child.haste, control),
            )


class AccountModelTests(TestCase):
    def test_encrypted_keys_round_trip_and_are_opaque_in_db(self):
        account = make_account()
        account.app_key = "super-secret-key"
        account.app_secret = "super-secret-secret"
        account.save(update_fields=["app_key", "app_secret"])

        # Re-read through the ORM: decrypts transparently.
        fresh = Account.objects.get(pk=account.pk)
        self.assertEqual(fresh.app_key, "super-secret-key")
        self.assertEqual(fresh.app_secret, "super-secret-secret")

        # Raw column never contains the plaintext.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT app_key, app_secret FROM monkey_account WHERE id = %s",
                [account.pk],
            )
            raw_key, raw_secret = cursor.fetchone()
        self.assertNotIn("super-secret-key", raw_key or "")
        self.assertNotIn("super-secret-secret", raw_secret or "")

    def test_display_id_formats_cano_and_product(self):
        account = make_account(number="50333044")
        self.assertEqual(account.display_id, "5033-3044-01")

    def test_tr_ids_and_base_url_differ_by_type(self):
        mock_account = make_account(account_type=Account.AccountType.MOCK)
        real_account = make_account(account_type=Account.AccountType.REAL)
        self.assertTrue(mock_account.buy_tr_id.startswith("V"))
        self.assertIn("openapivts", mock_account.base_url)
        self.assertTrue(real_account.buy_tr_id.startswith("T"))
        self.assertIn("openapi.koreainvestment", real_account.base_url)
        self.assertNotIn("openapivts", real_account.base_url)

    def test_rate_limit_keys_are_per_account(self):
        a = make_account()
        b = make_account()
        self.assertNotEqual(a.rate_limit_key, b.rate_limit_key)


class AccountFreeClientTests(TestCase):
    def test_prefers_real_account_then_falls_back_to_mock(self):
        mock_account = make_account(account_type=Account.AccountType.MOCK)
        client = services.get_account_free_client()
        self.assertEqual(client.account, mock_account)

        real_account = make_account(account_type=Account.AccountType.REAL)
        client = services.get_account_free_client()
        self.assertEqual(client.account, real_account)

    def test_raises_when_no_account(self):
        with self.assertRaises(services.NoAccountAvailableError):
            services.get_account_free_client()


class SoftDeleteAccountTests(TestCase):
    def test_soft_delete_wipes_keys_kills_monkeys_drops_holdings_keeps_orders(self):
        account = make_account()
        stock = Stock.objects.create(market="KOSPI", ticker="005930", name="S")
        monkey = Monkey.objects.create(
            account=account, name="A", balance=1000, initial_balance=1000
        )
        Holding.objects.create(monkey=monkey, stock=stock, quantity=5)
        Order.objects.create(
            monkey=monkey,
            stock=stock,
            order_type=Order.OrderTypeChoices.BUY,
            status=Order.StatusChoices.SUCCEEDED,
            requested_quantity=5,
            executed_quantity=5,
        )

        services.soft_delete_account(account)

        account.refresh_from_db()
        monkey.refresh_from_db()
        self.assertFalse(account.is_active)
        self.assertEqual(account.app_key, "")
        self.assertEqual(account.app_secret, "")
        self.assertEqual(monkey.state, Monkey.State.DEAD)
        self.assertFalse(Holding.objects.filter(monkey=monkey).exists())
        # Orders are retained for history.
        self.assertEqual(Order.objects.filter(monkey=monkey).count(), 1)


class ReconcileIsolationTests(TestCase):
    def test_reconcile_is_scoped_to_one_account(self):
        stock = Stock.objects.create(market="KOSPI", ticker="005930", name="S")
        account_a = make_account()
        account_b = make_account()
        monkey_a = Monkey.objects.create(
            account=account_a, name="A", balance=0, initial_balance=0
        )
        monkey_b = Monkey.objects.create(
            account=account_b, name="B", balance=0, initial_balance=0
        )
        Holding.objects.create(monkey=monkey_a, stock=stock, quantity=5)
        Holding.objects.create(monkey=monkey_b, stock=stock, quantity=5)

        # Account A's real balance shows 5 (matches its own monkey) — no clamp.
        result = services.reconcile_holdings(
            account_a, kis_client=FakeKisClient(holdings={"005930": 5})
        )
        self.assertEqual(result["absorbed"], [])
        self.assertEqual(result["clamped"], [])
        # Account B's holding is untouched (not clamped against A's balance).
        self.assertEqual(Holding.objects.get(monkey=monkey_b, stock=stock).quantity, 5)


class AccountApiTests(APITestCase):
    def _admin(self):
        user = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user)
        return user

    def test_account_register_never_returns_keys(self):
        self._admin()
        response = self.client.post(
            reverse("account-list"),
            {
                "account_type": "mock",
                "app_key": "k",
                "app_secret": "s",
                "account_number": "50331234",
                "product_code": "01",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("app_key", response.data)
        self.assertNotIn("app_secret", response.data)
        self.assertEqual(response.data["display_id"], "5033-1234-01")

    def test_account_list_requires_admin(self):
        response = self.client.get(reverse("account-list"))
        self.assertEqual(response.status_code, 401)

    def test_delete_soft_deletes(self):
        self._admin()
        account = make_account()
        response = self.client.delete(reverse("account-detail", args=[account.id]))
        self.assertEqual(response.status_code, 204)
        account.refresh_from_db()
        self.assertFalse(account.is_active)


class RealtimePublisherTests(TestCase):
    def _capture_layer(self):
        sent = []

        class FakeLayer:
            async def group_send(self, group, message):
                sent.append((group, message))

        return FakeLayer(), sent

    def test_publish_order_sends_to_orders_group(self):
        from monkey import realtime

        account = make_account()
        stock = Stock.objects.create(market="KOSPI", ticker="005930", name="S")
        monkey = Monkey.objects.create(
            account=account, name="A", balance=1000, initial_balance=1000
        )
        order = Order.objects.create(
            monkey=monkey,
            stock=stock,
            order_type=Order.OrderTypeChoices.BUY,
            status=Order.StatusChoices.SUCCEEDED,
            requested_quantity=1,
            executed_quantity=1,
            estimated_price=100,
            executed_price=100,
        )
        layer, sent = self._capture_layer()
        with mock.patch("monkey.realtime.get_channel_layer", return_value=layer):
            realtime.publish_order(order)

        self.assertEqual(len(sent), 1)
        group, message = sent[0]
        self.assertEqual(group, "dashboard.orders")
        self.assertEqual(message["type"], "order_event")
        self.assertEqual(message["data"]["event"], "order.succeeded")

    def test_publish_skips_system_monkey_orders(self):
        from monkey import realtime

        account = make_account()
        stock = Stock.objects.create(market="KOSPI", ticker="005930", name="S")
        system = services.get_or_create_system_monkey(account)
        order = Order.objects.create(
            monkey=system,
            stock=stock,
            order_type=Order.OrderTypeChoices.SELL,
            status=Order.StatusChoices.SUCCEEDED,
            requested_quantity=1,
            executed_quantity=1,
        )
        layer, sent = self._capture_layer()
        with mock.patch("monkey.realtime.get_channel_layer", return_value=layer):
            realtime.publish_order(order)
        self.assertEqual(sent, [])

    def test_publish_is_noop_without_channel_layer(self):
        from monkey import realtime

        with mock.patch("monkey.realtime.get_channel_layer", return_value=None):
            # Must not raise.
            realtime.publish_task("monkey.tasks.daily_maintenance", "id-1", "started")


class SingleInstanceLockTests(TestCase):
    def test_skips_when_lock_already_held(self):
        # A second concurrent run gets nothing (SET NX returns False) → skips.
        from monkey import tasks

        services.set_trading_enabled(True)
        fake_redis = mock.Mock()
        fake_redis.set.return_value = False
        with mock.patch("monkey.kis._get_redis", return_value=fake_redis):
            # BaseTask wraps the return value in a {"output": ...} envelope.
            result = tasks.update_held_stock_prices()
        self.assertEqual(result["output"], {"skipped": "already_running"})

    def test_fails_open_when_redis_unavailable(self):
        # If Redis can't be reached the task must still run (lock yields True).
        from monkey.kis import single_instance

        with mock.patch("monkey.kis._get_redis", side_effect=RuntimeError("down")):
            with single_instance("x", ttl=10) as acquired:
                self.assertTrue(acquired)


class ConsumerAuthTests(IsolatedAsyncioTestCase):
    async def test_dashboard_consumer_accepts_anonymous(self):
        from channels.testing import WebsocketCommunicator
        from django.contrib.auth.models import AnonymousUser

        from monkey.consumers import DashboardConsumer

        communicator = WebsocketCommunicator(
            DashboardConsumer.as_asgi(), "/ws/dashboard/"
        )
        communicator.scope["user"] = AnonymousUser()
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_admin_consumer_rejects_non_staff(self):
        from channels.testing import WebsocketCommunicator
        from django.contrib.auth.models import AnonymousUser

        from monkey.consumers import AdminConsumer

        communicator = WebsocketCommunicator(AdminConsumer.as_asgi(), "/ws/admin/")
        communicator.scope["user"] = AnonymousUser()
        connected, _ = await communicator.connect()
        self.assertFalse(connected)
