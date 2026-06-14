"""Seed the local database with realistic dummy data.

Populates everything the Celery beat tasks and the KIS API would normally
produce, so the frontend has data to fetch while Celery is **not** running:

- a "trading" GlobalMonkeyControl (all three gates open),
- stocks with live ``current_price`` (normally set by ``update_held_stock_prices``),
- monkeys with holdings and a succeeded order history (normally produced by trading),
- daily snapshots (normally ``snapshot_monkeys``) for the line chart,
- per-minute earning-ratio ticks (normally ``record_earning_ratio_tick``) spanning
  several trading days so the candle chart renders at every unit.

Usage::

    uv run python manage.py seed_dummy_data            # add data
    uv run python manage.py seed_dummy_data --clear     # wipe + reseed
    uv run python manage.py seed_dummy_data --monkeys 40
"""

import random
from datetime import datetime, time, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from market.models import Holding, Order, Stock
from monkey import services
from monkey.models import Monkey, MonkeyDailySnapshot, MonkeyEarningRatioTick
from monkey.names import generate_monkey_name

INITIAL_BALANCE = 1_000_000

# (market, ticker, name, approximate current price)
STOCKS = [
    ("KOSPI", "005930", "삼성전자", 71_500),
    ("KOSPI", "000660", "SK하이닉스", 135_000),
    ("KOSPI", "035420", "NAVER", 185_000),
    ("KOSPI", "005380", "현대차", 248_000),
    ("KOSPI", "051910", "LG화학", 382_000),
    ("KOSPI", "035720", "카카오", 45_300),
    ("KOSPI", "207940", "삼성바이오로직스", 781_000),
    ("KOSPI", "005490", "POSCO홀딩스", 421_000),
    ("KOSDAQ", "247540", "에코프로비엠", 182_000),
    ("KOSDAQ", "086520", "에코프로", 521_000),
    ("KOSDAQ", "091990", "셀트리온헬스케어", 69_800),
    ("KOSDAQ", "263750", "펄어비스", 35_200),
]


class Command(BaseCommand):
    help = (
        "Seed the local DB with dummy data so the frontend has data without Celery/KIS."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--monkeys", type=int, default=24, help="How many monkeys to create."
        )
        parser.add_argument(
            "--seed", type=int, default=42, help="RNG seed for reproducibility."
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete existing monkeys/stocks/orders/snapshots/ticks before seeding.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        rng = random.Random(options["seed"])

        if options["clear"]:
            self._clear()

        self._seed_control()
        stocks = self._seed_stocks()
        monkeys = self._seed_monkeys(rng, stocks, options["monkeys"])
        order_count = self._seed_holdings_and_orders(rng, monkeys, stocks)
        snapshot_count = self._seed_snapshots(rng, monkeys)
        tick_count = self._seed_ticks(rng)

        self.stdout.write(
            self.style.SUCCESS(
                "Seeded "
                f"{len(monkeys)} monkeys, {len(stocks)} stocks, {order_count} orders, "
                f"{snapshot_count} daily snapshots, {tick_count} earning-ratio ticks."
            )
        )

    # -- steps -------------------------------------------------------------

    def _clear(self):
        Order.objects.all().delete()
        Holding.objects.all().delete()
        MonkeyDailySnapshot.objects.all().delete()
        MonkeyEarningRatioTick.objects.all().delete()
        Monkey.objects.all().delete()
        Stock.objects.all().delete()
        self.stdout.write(
            "Cleared existing monkeys, stocks, orders, snapshots and ticks."
        )

    def _seed_control(self):
        control = services.get_global_control()
        control.time_enabled = True
        control.holiday_enabled = True
        control.manual_enabled = True
        control.note = "더미 데이터 (수동 시드)"
        control.save(
            update_fields=[
                "time_enabled",
                "holiday_enabled",
                "manual_enabled",
                "note",
                "updated_at",
            ]
        )

    def _seed_stocks(self):
        now = timezone.now()
        stocks = []
        for market, ticker, name, price in STOCKS:
            stock, _ = Stock.objects.update_or_create(
                ticker=ticker,
                market=market,
                defaults={
                    "name": name,
                    "is_active": True,
                    "current_price": price,
                    "price_updated_at": now,
                },
            )
            stocks.append(stock)
        return stocks

    def _seed_monkeys(self, rng, stocks, count):
        monkeys = []
        for _ in range(count):
            active = rng.random() > 0.15
            monkey = Monkey(
                name=generate_monkey_name(),
                balance=INITIAL_BALANCE,
                initial_balance=INITIAL_BALANCE,
                order_interval_seconds=rng.randint(60, 1800),
                is_active=active,
            )
            if not active:
                monkey.killed_at = timezone.now() - timedelta(days=rng.randint(1, 10))
            monkey.save()
            monkeys.append(monkey)
        return monkeys

    def _seed_holdings_and_orders(self, rng, monkeys, stocks):
        holdings = []
        orders = []
        order_times = []

        for monkey in monkeys:
            cash = INITIAL_BALANCE

            # Current positions: a few held stocks, each backed by 1-3 buy orders
            # whose quantities sum to the held quantity (so the FIFO average price
            # and holdings breakdown stay consistent).
            for stock in rng.sample(stocks, rng.randint(0, 4)):
                base = stock.current_price
                quantity = 0
                for _ in range(rng.randint(1, 3)):
                    price = round(base * rng.uniform(0.75, 1.2))
                    affordable = (cash // price) if price else 0
                    if affordable < 1:
                        break
                    lot = rng.randint(1, min(8, affordable))
                    cash -= lot * price
                    quantity += lot
                    orders.append(
                        self._order(
                            monkey, stock, Order.OrderTypeChoices.BUY, lot, price
                        )
                    )
                    order_times.append(self._market_dt(rng))
                if quantity > 0:
                    holdings.append(
                        Holding(monkey=monkey, stock=stock, quantity=quantity)
                    )

            # A few fully-closed round trips (buy then sell the same quantity) so the
            # order feed and history also contain sells and realized trades.
            for stock in rng.sample(stocks, rng.randint(0, 3)):
                base = stock.current_price
                buy_price = round(base * rng.uniform(0.8, 1.1))
                affordable = (cash // buy_price) if buy_price else 0
                if affordable < 1:
                    continue
                lot = rng.randint(1, min(5, affordable))
                sell_price = round(buy_price * rng.uniform(0.85, 1.25))
                cash -= lot * buy_price
                cash += lot * sell_price
                buy_dt = self._market_dt(rng)
                orders.append(
                    self._order(
                        monkey, stock, Order.OrderTypeChoices.BUY, lot, buy_price
                    )
                )
                order_times.append(buy_dt)
                orders.append(
                    self._order(
                        monkey, stock, Order.OrderTypeChoices.SELL, lot, sell_price
                    )
                )
                order_times.append(buy_dt + timedelta(hours=rng.randint(1, 48)))

            monkey.balance = max(0, round(cash))
            monkey.save(update_fields=["balance"])

        Holding.objects.bulk_create(holdings)

        created = Order.objects.bulk_create(orders)
        for order, dt in zip(created, order_times):
            order.created_at = dt
        Order.objects.bulk_update(created, ["created_at"])
        return len(created)

    def _seed_snapshots(self, rng, monkeys, days=20):
        snapshots = []
        today = timezone.localdate()
        for monkey in monkeys:
            ratio = rng.uniform(-0.1, 0.1)
            for offset in range(days, -1, -1):
                ratio = max(-0.45, min(0.6, ratio + rng.uniform(-0.04, 0.05)))
                total_pl = round(INITIAL_BALANCE * ratio)
                total_equity = INITIAL_BALANCE + total_pl
                cash_balance = round(total_equity * rng.uniform(0.3, 0.7))
                snapshots.append(
                    MonkeyDailySnapshot(
                        monkey=monkey,
                        date=today - timedelta(days=offset),
                        cash_balance=cash_balance,
                        holdings_value=total_equity - cash_balance,
                        total_equity=total_equity,
                        total_pl=total_pl,
                        realized_pl=round(total_pl * rng.uniform(0, 0.5)),
                        unrealized_pl=round(total_pl * rng.uniform(0, 0.5)),
                        earning_ratio=round(ratio, 4),
                    )
                )
        MonkeyDailySnapshot.objects.bulk_create(snapshots)
        return len(snapshots)

    def _seed_ticks(self, rng, days=6, step_minutes=5):
        tz = timezone.get_current_timezone()
        today = timezone.localdate()
        walk = rng.uniform(-0.05, 0.05)
        values = []
        for offset in range(days, -1, -1):
            day = today - timedelta(days=offset)
            if day.weekday() >= 5:  # skip weekends
                continue
            cursor = datetime.combine(day, time(9, 0))
            end = datetime.combine(day, time(15, 30))
            while cursor <= end:
                walk = max(-0.4, min(0.6, walk + rng.uniform(-0.008, 0.01)))
                values.append((timezone.make_aware(cursor, tz), round(walk, 4)))
                cursor += timedelta(minutes=step_minutes)

        ticks = MonkeyEarningRatioTick.objects.bulk_create(
            [MonkeyEarningRatioTick(average_earning_ratio=value) for _, value in values]
        )
        for tick, (recorded_at, _) in zip(ticks, values):
            tick.recorded_at = recorded_at
        MonkeyEarningRatioTick.objects.bulk_update(ticks, ["recorded_at"])
        return len(ticks)

    # -- helpers -----------------------------------------------------------

    def _order(self, monkey, stock, order_type, quantity, price):
        return Order(
            monkey=monkey,
            stock=stock,
            order_type=order_type,
            status=Order.StatusChoices.SUCCEEDED,
            requested_quantity=quantity,
            executed_quantity=quantity,
            estimated_price=price,
            executed_price=price,
            kis_order_status="모의 체결 (더미)",
        )

    def _market_dt(self, rng, days_back=20):
        now = timezone.localtime()
        day = now.date() - timedelta(days=rng.randint(0, days_back))
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        naive = datetime.combine(day, time(rng.randint(9, 14), rng.randint(0, 59)))
        return timezone.make_aware(naive, timezone.get_current_timezone())
