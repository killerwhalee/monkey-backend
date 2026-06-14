from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
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

    def get_account_balance(self):
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
    def __init__(self, data, status_ok=True):
        self.data = data
        self.status_ok = status_ok

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

    def test_kill_monkey_liquidates_all_holdings(self):
        services.set_trading_enabled(True)

        monkey = Monkey.objects.create(name="A", balance=1000, initial_balance=1000)
        other_stock = Stock.objects.create(
            market="KOSPI", ticker="000660", name="SK Hynix"
        )
        Holding.objects.create(monkey=monkey, stock=self.stock, quantity=2)
        Holding.objects.create(monkey=monkey, stock=other_stock, quantity=3)

        with mock.patch(
            "monkey.services.KisClient", return_value=FakeKisClient(price=100)
        ):
            services.kill_monkey(monkey)

        monkey.refresh_from_db()
        self.assertFalse(monkey.is_active)
        self.assertIsNotNone(monkey.killed_at)
        self.assertFalse(
            Holding.objects.filter(monkey=monkey, stock=self.stock).exists()
        )
        self.assertFalse(
            Holding.objects.filter(monkey=monkey, stock=other_stock).exists()
        )
        sell_orders = Order.objects.filter(
            monkey=monkey,
            order_type=Order.OrderTypeChoices.SELL,
            status=Order.StatusChoices.SUCCEEDED,
        )
        self.assertEqual(sell_orders.count(), 2)

    def test_kill_monkey_rejected_when_trading_disabled(self):
        monkey = Monkey.objects.create(name="A", balance=1000, initial_balance=1000)
        Holding.objects.create(monkey=monkey, stock=self.stock, quantity=2)

        with self.assertRaises(services.KillNotAllowedError):
            services.kill_monkey(monkey)

        monkey.refresh_from_db()
        self.assertTrue(monkey.is_active)
        self.assertIsNone(monkey.killed_at)
        self.assertEqual(
            Holding.objects.get(monkey=monkey, stock=self.stock).quantity, 2
        )

    def test_auto_create_monkeys_uses_kis_cash_balance(self):
        Monkey.objects.create(name="A", balance=1_000_000, initial_balance=1_000_000)
        fake_client = FakeKisClient(balance=3_000_000)

        monkeys = services.auto_create_monkeys(kis_client=fake_client)

        self.assertEqual(len(monkeys), 2)
        self.assertEqual(Monkey.objects.filter(is_system=False).count(), 3)
        for monkey in monkeys:
            self.assertEqual(monkey.balance, services.AUTO_CREATE_STARTING_BALANCE)


class OrphanedHoldingsTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(
            market="KOSPI", ticker="005930", name="Samsung Electronics"
        )

    def test_reconcile_holdings_absorbs_excess_into_system_monkey_and_sells(self):
        fake_client = FakeKisClient(price=100, holdings={"005930": 5})

        result = services.reconcile_holdings(kis_client=fake_client)

        self.assertEqual(len(result["absorbed"]), 1)
        absorbed = result["absorbed"][0]
        self.assertEqual(absorbed["ticker"], "005930")
        self.assertEqual(absorbed["quantity"], 5)
        self.assertEqual(result["clamped"], [])

        system_monkey = Monkey.objects.get(is_system=True)
        self.assertFalse(
            Holding.objects.filter(monkey=system_monkey, stock=self.stock).exists()
        )

        order = Order.objects.get(id=absorbed["order_ids"][0])
        self.assertEqual(order.order_type, Order.OrderTypeChoices.SELL)
        self.assertEqual(order.status, Order.StatusChoices.SUCCEEDED)
        self.assertEqual(order.executed_quantity, 5)

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

    def test_liquidate_orphaned_holdings_only_targets_delisted_stocks(self):
        services.set_trading_enabled(True)

        delisted_stock = Stock.objects.create(
            market="KOSPI", ticker="999999", name="Delisted Co", is_active=False
        )
        monkey = Monkey.objects.create(name="A", balance=1000, initial_balance=1000)
        Holding.objects.create(monkey=monkey, stock=self.stock, quantity=2)
        Holding.objects.create(monkey=monkey, stock=delisted_stock, quantity=1)

        fake_client = FakeKisClient(price=100, holdings={"005930": 2, "999999": 1})

        with mock.patch("monkey.services.KisClient", return_value=fake_client):
            result = services.liquidate_orphaned_holdings()

        self.assertTrue(result["enabled"])
        self.assertEqual(result["delisted_orders"], 1)
        self.assertEqual(
            Holding.objects.get(monkey=monkey, stock=self.stock).quantity, 2
        )
        self.assertFalse(
            Holding.objects.filter(monkey=monkey, stock=delisted_stock).exists()
        )

    def test_liquidate_orphaned_holdings_sells_killed_monkey_holdings(self):
        services.set_trading_enabled(True)

        killed = Monkey.objects.create(
            name="Z", balance=0, initial_balance=1000, is_active=False
        )
        Holding.objects.create(monkey=killed, stock=self.stock, quantity=3)

        fake_client = FakeKisClient(price=100, holdings={"005930": 3})

        with mock.patch("monkey.services.KisClient", return_value=fake_client):
            result = services.liquidate_orphaned_holdings()

        self.assertEqual(result["killed_orders"], 1)
        self.assertFalse(
            Holding.objects.filter(monkey=killed, stock=self.stock).exists()
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

    def test_force_kill_endpoint_rejected_when_trading_disabled(self):
        monkey = Monkey.objects.create(name="A", balance=1000, initial_balance=1000)

        user = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user)

        response = self.client.post(reverse("monkey-force-kill", args=[monkey.id]))

        self.assertEqual(response.status_code, 409)
        self.assertIn("detail", response.data)
        monkey.refresh_from_db()
        self.assertTrue(monkey.is_active)
        self.assertIsNone(monkey.killed_at)

    def test_system_monkey_excluded_from_dashboard_and_monkey_list(self):
        system_monkey = services.get_or_create_system_monkey()
        Monkey.objects.create(name="A", balance=1000, initial_balance=1000)

        list_response = self.client.get(reverse("monkey-list"))
        self.assertNotIn(system_monkey.id, [item["id"] for item in list_response.data])

        self.assertEqual(len(services._earning_ratios()), 1)

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
        self.assertEqual(response.data["average_earning_ratio"], 0.0)
        self.assertEqual(response.data["best_earning_ratio"], 0.0)
        self.assertEqual(response.data["latest_orders"], [])
        self.assertEqual(response.data["daily_earning_ratio_series"], [])

    def test_dashboard_summary_shape_and_values(self):
        active_monkey = Monkey.objects.create(
            name="A", balance=6000, initial_balance=5000, is_active=True
        )
        paused_monkey = Monkey.objects.create(
            name="B", balance=4000, initial_balance=5000, is_active=False
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

        MonkeyDailySnapshot.objects.create(
            monkey=active_monkey,
            date=date(2026, 6, 8),
            cash_balance=6000,
            holdings_value=0,
            total_equity=6000,
            total_pl=1000,
            realized_pl=0,
            unrealized_pl=0,
            earning_ratio=0.2,
        )
        MonkeyDailySnapshot.objects.create(
            monkey=paused_monkey,
            date=date(2026, 6, 8),
            cash_balance=4000,
            holdings_value=0,
            total_equity=4000,
            total_pl=-1000,
            realized_pl=0,
            unrealized_pl=0,
            earning_ratio=-0.2,
        )
        MonkeyDailySnapshot.objects.create(
            monkey=active_monkey,
            date=date(2026, 6, 9),
            cash_balance=6500,
            holdings_value=0,
            total_equity=6500,
            total_pl=1500,
            realized_pl=0,
            unrealized_pl=0,
            earning_ratio=0.3,
        )

        response = self.client.get(reverse("dashboard-summary"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["active_monkey_count"], 1)

        ratios = [
            build_monkey_metrics(m)["earning_ratio"]
            for m in (active_monkey, paused_monkey)
        ]
        self.assertAlmostEqual(
            response.data["average_earning_ratio"], sum(ratios) / len(ratios)
        )
        self.assertAlmostEqual(response.data["best_earning_ratio"], max(ratios))

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

        series = response.data["daily_earning_ratio_series"]
        self.assertEqual(
            [point["date"] for point in series], ["2026-06-08", "2026-06-09"]
        )
        self.assertAlmostEqual(series[0]["average_earning_ratio"], 0.0)
        self.assertAlmostEqual(series[0]["best_earning_ratio"], 0.2)
        self.assertAlmostEqual(series[1]["average_earning_ratio"], 0.3)
        self.assertAlmostEqual(series[1]["best_earning_ratio"], 0.3)


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
        self.assertEqual(result, {"enabled": False})


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
        services.set_trading_enabled(False)
        result = services.reconcile_order_executions(kis_client=FakeKisClient())
        self.assertEqual(result, {"enabled": False})


class CandlestickApiTests(APITestCase):
    def test_candlesticks_endpoint_is_public_and_buckets_by_unit(self):
        from monkey.models import MonkeyEarningRatioTick

        ticks = [
            MonkeyEarningRatioTick.objects.create(average_earning_ratio=ratio)
            for ratio in (0.1, 0.3, 0.2)
        ]
        base = timezone.now()
        for offset, tick in enumerate(ticks):
            MonkeyEarningRatioTick.objects.filter(pk=tick.pk).update(
                recorded_at=base - timedelta(minutes=2 - offset)
            )

        response = self.client.get(reverse("candlesticks"), {"unit": "1d"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        candle = response.data[0]
        self.assertEqual(candle["open"], 0.1)
        self.assertEqual(candle["high"], 0.3)
        self.assertEqual(candle["low"], 0.1)
        self.assertEqual(candle["close"], 0.2)
        self.assertIn("time", candle)
