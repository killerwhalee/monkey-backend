from datetime import date, timedelta
from unittest import mock

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
from monkey.models import KisAccessToken, Monkey, MonkeyDailySnapshot
from monkey.serializers import build_monkey_metrics


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

    def get_stock_price(self, ticker):
        return self.price

    def get_account_balance(self, include_holdings=True):
        return {"cash_balance": self.balance, "holdings": self.holdings}

    def is_holiday(self, date=None):
        return self.holiday

    def get_daily_order_executions(self, start_date=None, end_date=None):
        return self.executions

    def order_stock(self, order_type, ticker, quantity):
        self.orders.append(
            {
                "order_type": order_type,
                "ticker": ticker,
                "quantity": quantity,
            }
        )
        if self.fail_order:
            from monkey.kis import KisClientError

            raise KisClientError("network failed")
        return {"PDNO": ticker, "ORD_QTY": str(quantity)}, self.response


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
    @mock.patch("monkey.kis.requests.post")
    def test_refresh_access_token_persists_token(self, post):
        post.return_value = FakeResponse(
            {
                "access_token": "token-1",
                "expires_in": 3600,
            }
        )

        token = KisClient().refresh_access_token()

        self.assertEqual(token.token, "token-1")
        self.assertEqual(KisAccessToken.objects.count(), 1)
        self.assertGreater(token.expires_at, timezone.now())

    @mock.patch("monkey.kis.requests.post")
    def test_order_stock_uses_market_buy_payload(self, post):
        KisAccessToken.objects.create(
            environment="virtual",
            token="token-1",
            expires_at=timezone.now() + timedelta(hours=1),
        )
        post.return_value = FakeResponse({"rt_cd": "0", "output": {"ODNO": "1"}})

        payload, data = KisClient().order_stock(
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
            environment="virtual",
            token="token-1",
            expires_at=timezone.now() + timedelta(hours=1),
        )
        post.return_value = FakeResponse({"rt_cd": "0", "output": {"ODNO": "1"}})

        KisClient().order_stock(
            order_type=Order.OrderTypeChoices.SELL,
            ticker="005930",
            quantity=2,
        )

        self.assertEqual(post.call_args.kwargs["headers"]["tr_id"], "VTTC0011U")


class MonkeyServiceTests(TestCase):
    def setUp(self):
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

        monkey.refresh_from_db()
        holding = Holding.objects.get(monkey=monkey, stock=self.stock)
        self.assertEqual(order.status, Order.StatusChoices.SUCCEEDED)
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

    def test_global_kill_switch_prevents_scheduled_orders(self):
        Monkey.objects.create(name="A", balance=5000, initial_balance=5000)

        result = services.run_active_monkeys()

        self.assertEqual(result["enabled"], False)
        self.assertEqual(Order.objects.count(), 0)

    def test_random_order_quantity_is_always_one(self):
        monkey = Monkey.objects.create(
            name="A",
            balance=5000,
            initial_balance=5000,
        )

        class Rng:
            def choice(self, values):
                return Order.OrderTypeChoices.BUY

        order = services.run_random_monkey_order(
            monkey.id,
            kis_client=FakeKisClient(price=100),
            rng=Rng(),
        )

        self.assertEqual(order.requested_quantity, 1)
        self.assertEqual(order.status, Order.StatusChoices.SUCCEEDED)

    def test_random_sell_order_picks_holding_stock_and_sells_one_share(self):
        monkey = Monkey.objects.create(
            name="A",
            balance=5000,
            initial_balance=5000,
        )
        other_stock = Stock.objects.create(
            market="KOSPI",
            ticker="000660",
            name="SK Hynix",
        )
        Holding.objects.create(monkey=monkey, stock=self.stock, quantity=2)

        class Rng:
            def choice(self, values):
                return Order.OrderTypeChoices.SELL

        order = services.run_random_monkey_order(
            monkey.id,
            kis_client=FakeKisClient(price=100),
            rng=Rng(),
        )

        self.assertEqual(order.stock_id, self.stock.id)
        self.assertNotEqual(order.stock_id, other_stock.id)
        self.assertEqual(order.requested_quantity, 1)
        self.assertEqual(order.status, Order.StatusChoices.SUCCEEDED)
        self.assertEqual(
            Holding.objects.get(monkey=monkey, stock=self.stock).quantity, 1
        )

    def test_create_monkeys_assigns_pet_names_and_intervals(self):
        with mock.patch("monkey.names.random.choice", return_value="Arthur"):
            first = services.create_monkeys(count=1, starting_balance=1000)[0]
            second = services.create_monkeys(count=1, starting_balance=1000)[0]
            third = services.create_monkeys(count=1, starting_balance=1000)[0]

        self.assertEqual(first.name, "Arthur")
        self.assertEqual(second.name, "Arthur II")
        self.assertEqual(third.name, "Arthur III")
        for monkey in (first, second, third):
            self.assertTrue(60 <= monkey.order_interval_seconds <= 1800)

    def test_kill_monkey_transfers_holdings_to_system_monkey(self):
        services.set_trading_enabled(True)

        monkey = Monkey.objects.create(name="A", balance=1000, initial_balance=1000)
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
        monkey = Monkey.objects.create(name="A", balance=1000, initial_balance=1000)
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
        Monkey.objects.create(name="A", balance=1_000_000, initial_balance=1_000_000)
        fake_client = FakeKisClient(balance=3_000_000)

        monkeys = services.auto_create_monkeys(kis_client=fake_client)

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

        monkeys = services.auto_create_monkeys(kis_client=fake_client)

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

        monkeys = services.create_monkeys(count=3, starting_balance=1000)

        for monkey in monkeys:
            self.assertEqual(monkey.order_interval_seconds, 300)


class OrphanedHoldingsTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(
            market="KOSPI", ticker="005930", name="Samsung Electronics"
        )

    def test_reconcile_holdings_absorbs_excess_into_system_monkey(self):
        fake_client = FakeKisClient(price=100, holdings={"005930": 5})

        result = services.reconcile_holdings(kis_client=fake_client)

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
        monkey = Monkey.objects.create(name="A", balance=0, initial_balance=0)
        holding = Holding.objects.create(monkey=monkey, stock=self.stock, quantity=5)

        fake_client = FakeKisClient(price=100, holdings={"005930": 2})

        result = services.reconcile_holdings(kis_client=fake_client)

        self.assertEqual(result["absorbed"], [])
        self.assertEqual(len(result["clamped"]), 1)
        clamped = result["clamped"][0]
        self.assertEqual(clamped["ticker"], "005930")
        self.assertEqual(clamped["quantity"], 3)

        holding.refresh_from_db()
        self.assertEqual(holding.quantity, 2)
        self.assertEqual(Order.objects.count(), 0)

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

        monkey = Monkey.objects.create(name="A", balance=0, initial_balance=0)
        Holding.objects.create(monkey=monkey, stock=etn, quantity=4)
        Holding.objects.create(monkey=monkey, stock=warrant, quantity=7)

        fake_client = FakeKisClient(price=100, holdings={"610039": 4, "0669721F": 7})
        result = services.reconcile_holdings(kis_client=fake_client)

        self.assertEqual(result["absorbed"], [])
        self.assertEqual(result["clamped"], [])
        self.assertEqual(Holding.objects.get(monkey=monkey, stock=etn).quantity, 4)
        self.assertEqual(Holding.objects.get(monkey=monkey, stock=warrant).quantity, 7)

    def test_daily_maintenance_transfers_delisted_to_system_monkey(self):
        delisted_stock = Stock.objects.create(
            market="KOSPI", ticker="999999", name="Delisted Co", is_active=False
        )
        monkey = Monkey.objects.create(name="A", balance=1000, initial_balance=1000)
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

        killed = Monkey.objects.create(
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
        loser = Monkey.objects.create(name="L", balance=0, initial_balance=1000)

        with mock.patch("monkey.services.KisClient", return_value=FakeKisClient()):
            result = services.run_daily_maintenance()

        self.assertEqual(result, {"skipped": "market_open"})
        loser.refresh_from_db()
        self.assertEqual(loser.state, Monkey.State.ACTIVE)

    def test_daily_maintenance_culls_underperforming_monkeys(self):
        # earning_ratio = (balance - initial) / initial = (100 - 1000)/1000 = -0.9,
        # below the default kill threshold (-0.5), so the monkey is culled.
        loser = Monkey.objects.create(name="L", balance=100, initial_balance=1000)
        winner = Monkey.objects.create(name="W", balance=1000, initial_balance=1000)

        with mock.patch("monkey.services.KisClient", return_value=FakeKisClient()):
            result = services.run_daily_maintenance()

        self.assertEqual(result["killed"], 1)
        loser.refresh_from_db()
        winner.refresh_from_db()
        self.assertEqual(loser.state, Monkey.State.DEAD)
        self.assertEqual(winner.state, Monkey.State.ACTIVE)

    def test_run_system_monkey_order_sells_full_quantity_and_keeps_zero_balance(self):
        system_monkey = services.get_or_create_system_monkey()
        Holding.objects.create(monkey=system_monkey, stock=self.stock, quantity=4)
        fake_client = FakeKisClient(price=100)

        order = services.run_system_monkey_order(kis_client=fake_client)

        self.assertEqual(order.order_type, Order.OrderTypeChoices.SELL)
        self.assertEqual(order.status, Order.StatusChoices.SUCCEEDED)
        self.assertEqual(order.executed_quantity, 4)
        self.assertFalse(
            Holding.objects.filter(monkey=system_monkey, stock=self.stock).exists()
        )
        # The system monkey never retains the sale proceeds.
        system_monkey.refresh_from_db()
        self.assertEqual(system_monkey.balance, 0)

    def test_run_system_monkey_order_no_holdings_is_noop(self):
        services.get_or_create_system_monkey()

        self.assertIsNone(services.run_system_monkey_order(kis_client=FakeKisClient()))
        self.assertFalse(Order.objects.exists())

    def test_transfer_holdings_merges_into_existing_system_holding(self):
        system_monkey = services.get_or_create_system_monkey()
        Holding.objects.create(monkey=system_monkey, stock=self.stock, quantity=1)
        monkey = Monkey.objects.create(name="A", balance=0, initial_balance=0)
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

        with mock.patch(
            "monkey.services.KisClient",
            return_value=FakeKisClient(balance=1_000_000),
        ):
            response = self.client.post(
                reverse("monkey-bulk-create"),
                {
                    "count": 2,
                    "starting_balance": 1000,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Monkey.objects.count(), 2)

    def test_force_kill_endpoint_requires_admin_and_liquidates(self):
        services.set_trading_enabled(True)

        stock = Stock.objects.create(
            market="KOSPI", ticker="005930", name="Samsung Electronics"
        )
        monkey = Monkey.objects.create(name="A", balance=1000, initial_balance=1000)
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
        stock = Stock.objects.create(
            market="KOSPI", ticker="005930", name="Samsung Electronics"
        )
        monkey = Monkey.objects.create(name="A", balance=1000, initial_balance=1000)
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
        system_monkey = services.get_or_create_system_monkey()
        Monkey.objects.create(name="A", balance=1000, initial_balance=1000)

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

    def test_admin_can_patch_monkey_config_fields(self):
        user = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user)
        response = self.client.patch(
            reverse("global-monkey-control-current"),
            {
                "auto_create_starting_balance": 250_000,
                "kill_threshold": -0.8,
                "auto_create_min_interval_seconds": 120,
                "auto_create_max_interval_seconds": 600,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        control = services.get_global_control()
        self.assertEqual(control.auto_create_starting_balance, 250_000)
        self.assertEqual(control.kill_threshold, -0.8)
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
            {"task": "run_monkeys"},
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
        self.assertIn("monkey.index_tick", names)
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
            status=Order.StatusChoices.SUCCEEDED,
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
            status=Order.StatusChoices.SUCCEEDED,
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
                status=Order.StatusChoices.SUCCEEDED,
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
        # Up to 10 succeeded orders, newest first; the failed order is excluded.
        self.assertEqual(len(latest_orders), 7)
        self.assertTrue(all(order["status"] == "succeeded" for order in latest_orders))
        self.assertEqual(
            [order["id"] for order in latest_orders],
            list(
                Order.objects.filter(status=Order.StatusChoices.SUCCEEDED)
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

    def test_holiday_gate_short_circuits_run_active_monkeys(self):
        services.set_trading_enabled(True)
        services.set_holiday_closed(True)
        result = services.run_active_monkeys()
        self.assertEqual(result, {"enabled": False, "orders": 0})

    def test_check_holiday_task_sets_holiday_gate(self):
        with mock.patch(
            "monkey.tasks.KisClient",
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
            status=Order.StatusChoices.SUCCEEDED,
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
            status=Order.StatusChoices.SUCCEEDED,
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
        services.set_trading_enabled(True)
        result = services.update_held_stock_prices(kis_client=FakeKisClient(price=4321))
        self.stock.refresh_from_db()
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.stock.current_price, 4321)
        self.assertIsNotNone(self.stock.price_updated_at)

    def test_update_held_stock_prices_gated_on_global_switch(self):
        Holding.objects.create(monkey=self.monkey, stock=self.stock, quantity=2)
        result = services.update_held_stock_prices(kis_client=FakeKisClient(price=4321))
        self.assertEqual(result, {"market_open": False})


class ExecutionReconciliationTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(
            market="KOSPI", ticker="005930", name="Samsung Electronics"
        )
        self.monkey = Monkey.objects.create(
            name="A", balance=1000, initial_balance=10000
        )
        services.set_trading_enabled(True)

    def test_reconcile_corrects_executed_price_and_quantity(self):
        # Reconciliation is a market-closed task; setUp opened the gate.
        services.set_trading_enabled(False)
        order = Order.objects.create(
            monkey=self.monkey,
            stock=self.stock,
            order_type=Order.OrderTypeChoices.BUY,
            status=Order.StatusChoices.SUCCEEDED,
            requested_quantity=1,
            executed_quantity=1,
            estimated_price=1000,
            executed_price=1000,
            kis_order_id="0000012345",
        )
        client = FakeKisClient(
            executions={"12345": {"executed_quantity": 1, "avg_price": 1050}}
        )
        result = services.reconcile_order_executions(kis_client=client)

        order.refresh_from_db()
        self.assertEqual(result["reconciled"], 1)
        self.assertEqual(order.executed_price, 1050)

    def test_reconcile_gated_on_global_switch(self):
        # Reconciliation skips while the market is open (runs after close).
        services.set_trading_enabled(True)
        result = services.reconcile_order_executions(kis_client=FakeKisClient())
        self.assertEqual(result, {"skipped": "market_open"})


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

    def test_record_tick_is_gated(self):
        services.set_trading_enabled(False)
        self.assertEqual(services.record_index_tick(), {"market_open": False})

    def test_record_tick_skips_without_baseline(self):
        services.set_trading_enabled(True)
        self.assertEqual(
            services.record_index_tick(), {"enabled": True, "baseline": False}
        )

    def test_record_tick_scales_index_by_equity_ratio(self):
        from monkey.models import MonkeyIndexBaseline, MonkeyIndexTick

        services.set_trading_enabled(True)
        # Alive equity now is 12,000; baseline a=10,000, i=10,000 → 10,000 * 1.2.
        Monkey.objects.create(name="A", balance=12000, initial_balance=10000)
        MonkeyIndexBaseline.objects.create(
            date=timezone.localdate(), base_index=10000.0, base_equity=10000
        )

        result = services.record_index_tick()

        self.assertAlmostEqual(result["value"], 12000.0)
        self.assertAlmostEqual(
            MonkeyIndexTick.objects.latest("recorded_at").value, 12000.0
        )

    def test_record_tick_flat_when_base_equity_zero(self):
        from monkey.models import MonkeyIndexBaseline

        services.set_trading_enabled(True)
        MonkeyIndexBaseline.objects.create(
            date=timezone.localdate(), base_index=10000.0, base_equity=0
        )

        result = services.record_index_tick()

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


class CashAllocationTests(TestCase):
    def test_unallocated_cash_excludes_dead_monkeys(self):
        Monkey.objects.create(
            name="alive", balance=1_000_000, initial_balance=1_000_000
        )
        Monkey.objects.create(
            name="dead",
            balance=1_000_000,
            initial_balance=1_000_000,
            state=Monkey.State.DEAD,
        )
        client = FakeKisClient(balance=3_000_000)

        self.assertEqual(services.unallocated_cash(kis_client=client), 2_000_000)

    def test_create_monkeys_checked_rejects_when_insufficient(self):
        client = FakeKisClient(balance=500)
        with self.assertRaises(services.InsufficientCashError):
            services.create_monkeys_checked(
                count=1, starting_balance=1000, kis_client=client
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

        order = services.submit_monkey_order(
            monkey.id,
            stock.id,
            Order.OrderTypeChoices.BUY,
            1,
            kis_client=NoFetchClient(),
        )

        self.assertEqual(order.status, Order.StatusChoices.SUCCEEDED)
        self.assertEqual(order.executed_price, 1234)


class KisTransientRetryTests(TestCase):
    def setUp(self):
        KisAccessToken.objects.create(
            environment="virtual",
            token="seed",
            expires_at=timezone.now() + timedelta(hours=1),
        )

    @mock.patch("monkey.kis.requests.post")
    def test_retries_on_server_error_then_succeeds(self, post):
        post.side_effect = [
            FakeResponse({}, status_code=500),
            FakeResponse({"access_token": "t-ok", "expires_in": 3600}),
        ]
        token = KisClient().refresh_access_token()
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
        token = KisClient().refresh_access_token()
        self.assertEqual(token.token, "t-ok")
        self.assertEqual(post.call_count, 2)

    @mock.patch("monkey.kis.requests.post")
    def test_raises_after_exhausting_retries(self, post):
        post.return_value = FakeResponse({}, status_code=500)
        from monkey.kis import KisClientError

        with self.assertRaises(KisClientError):
            KisClient().refresh_access_token()
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
                status=Order.StatusChoices.SUCCEEDED,
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
