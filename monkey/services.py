import random

from django.db import transaction
from django.db.models import Avg, Max, Sum
from django.utils import timezone

from market.models import Holding, Order, Stock
from monkey.kis import KisClient, KisClientError
from monkey.models import (
    GlobalMonkeyControl,
    Monkey,
    MonkeyDailySnapshot,
    MonkeyEarningRatioTick,
)
from monkey.names import generate_monkey_name

AUTO_CREATE_STARTING_BALANCE = 1_000_000


def get_global_control():
    control, _ = GlobalMonkeyControl.objects.get_or_create(
        pk=1,
        defaults={"enabled": False},
    )
    return control


def set_trading_enabled(enabled: bool, note: str = "") -> GlobalMonkeyControl:
    """Set GlobalMonkeyControl.enabled. Used by market_open / market_close tasks."""
    control = get_global_control()
    control.enabled = enabled
    if note:
        control.note = note
    control.save(update_fields=["enabled", "note", "updated_at"])
    return control


def kill_monkey(monkey: Monkey) -> Monkey:
    """
    Deactivate a monkey and liquidate all of its holdings. Single path for both
    auto-kill (maybe_kill_monkey) and admin force-kill.
    The Monkey.save() override will automatically sync PeriodicTask.enabled=False.
    """
    liquidate_holdings_for_monkey(monkey)
    monkey.is_active = False
    monkey.killed_at = timezone.now()
    monkey.save(update_fields=["is_active", "killed_at"])
    return monkey


def maybe_kill_monkey(monkey: Monkey) -> bool:
    """Kill monkey if earning_ratio < kill_threshold. Returns True if killed."""
    # Deferred import: serializers.py does `from monkey import services`.
    from monkey.serializers import build_monkey_metrics

    control = get_global_control()
    if build_monkey_metrics(monkey)["earning_ratio"] < control.kill_threshold:
        kill_monkey(monkey)
        return True
    return False


def snapshot_all_monkeys(target_date=None):
    # Deferred import: serializers.py does `from monkey import services`, so importing
    # build_monkey_metrics at module load time would create a circular import (same
    # pattern as the deferred `from market.models import Order` in monkey/kis.py).
    from monkey.serializers import build_monkey_metrics

    target_date = target_date or timezone.localdate()
    count = 0
    for monkey in Monkey.objects.filter(is_system=False).order_by("id"):
        MonkeyDailySnapshot.objects.update_or_create(
            monkey=monkey,
            date=target_date,
            defaults=build_monkey_metrics(monkey),
        )
        count += 1
    return {"date": target_date.isoformat(), "snapshots": count}


def build_dashboard_summary():
    ratios = _earning_ratios()

    daily_series = list(
        MonkeyDailySnapshot.objects.values("date")
        .annotate(
            average_earning_ratio=Avg("earning_ratio"),
            best_earning_ratio=Max("earning_ratio"),
        )
        .order_by("date")
    )

    return {
        "active_monkey_count": Monkey.objects.filter(
            is_active=True, is_system=False
        ).count(),
        "average_earning_ratio": (sum(ratios) / len(ratios)) if ratios else 0.0,
        "best_earning_ratio": max(ratios) if ratios else 0.0,
        "latest_orders": (
            Order.objects.select_related("monkey", "stock")
            .exclude(monkey__is_system=True)
            .order_by("-created_at")[:5]
        ),
        "daily_earning_ratio_series": daily_series,
        "candlestick_series": build_earning_ratio_candlesticks(),
    }


def _earning_ratios():
    from monkey.serializers import build_monkey_metrics

    return [
        build_monkey_metrics(monkey)["earning_ratio"]
        for monkey in Monkey.objects.filter(is_system=False)
    ]


def record_earning_ratio_tick():
    """Sample the current average earning ratio. Gated on the global kill
    switch (mirrors run_monkey) so each day's ticks form a clean trading-
    session candle."""
    if not get_global_control().enabled:
        return {"enabled": False}

    ratios = _earning_ratios()
    average = (sum(ratios) / len(ratios)) if ratios else 0.0
    tick = MonkeyEarningRatioTick.objects.create(average_earning_ratio=average)
    return {"enabled": True, "tick_id": tick.id, "average_earning_ratio": average}


def build_earning_ratio_candlesticks(days=30):
    """Group per-minute earning-ratio ticks by day into OHLC candlesticks."""
    by_date = {}
    for recorded_at, ratio in MonkeyEarningRatioTick.objects.order_by(
        "recorded_at"
    ).values_list("recorded_at", "average_earning_ratio"):
        date = timezone.localtime(recorded_at).date()
        by_date.setdefault(date, []).append(ratio)

    candlesticks = [
        {
            "date": date,
            "open": values[0],
            "high": max(values),
            "low": min(values),
            "close": values[-1],
        }
        for date, values in sorted(by_date.items())
    ]
    return candlesticks[-days:]


def build_account_summary(kis_client=None):
    from monkey.serializers import build_monkey_metrics

    kis_client = kis_client or KisClient()
    cash_balance = kis_client.get_account_balance()["cash_balance"]

    monkeys = list(Monkey.objects.filter(is_system=False))
    metrics = [build_monkey_metrics(monkey) for monkey in monkeys]
    total_monkey_balance = sum(monkey.balance for monkey in monkeys)
    ratios = [item["earning_ratio"] for item in metrics]

    system_monkey = Monkey.objects.filter(is_system=True).first()
    system_metrics = build_monkey_metrics(system_monkey) if system_monkey else None

    return {
        "kis_cash_balance": cash_balance,
        "unallocated_cash": cash_balance - total_monkey_balance,
        "monkey_count": len(monkeys),
        "active_monkey_count": sum(monkey.is_active for monkey in monkeys),
        "total_monkey_balance": total_monkey_balance,
        "total_holdings_value": sum(item["holdings_value"] for item in metrics),
        "total_equity": sum(item["total_equity"] for item in metrics),
        "average_earning_ratio": (sum(ratios) / len(ratios)) if ratios else 0.0,
        "best_earning_ratio": max(ratios) if ratios else 0.0,
        "system_balance": system_monkey.balance if system_monkey else 0,
        "system_holdings_value": (
            system_metrics["holdings_value"] if system_metrics else 0
        ),
    }


def create_monkeys(count, starting_balance):
    # Individual saves (not bulk_create) so Monkey.save() fires and creates PeriodicTasks.
    monkeys = []
    for _ in range(count):
        monkey = Monkey(
            name=generate_monkey_name(),
            balance=starting_balance,
            initial_balance=starting_balance,
            order_interval_seconds=random.randint(60, 1800),
        )
        monkey.save()
        monkeys.append(monkey)
    return monkeys


def auto_create_monkeys(kis_client=None):
    """Create as many new monkeys as the KIS account's unallocated cash affords."""
    kis_client = kis_client or KisClient()
    kis_cash = kis_client.get_account_balance()["cash_balance"]
    allocated = (
        Monkey.objects.filter(is_system=False).aggregate(total=Sum("balance"))["total"]
        or 0
    )
    unallocated = kis_cash - allocated
    count = unallocated // AUTO_CREATE_STARTING_BALANCE
    if count <= 0:
        return []
    return create_monkeys(count=count, starting_balance=AUTO_CREATE_STARTING_BALANCE)


def run_active_monkeys():
    if not get_global_control().enabled:
        return {"enabled": False, "orders": 0}

    orders = []
    for monkey in Monkey.objects.filter(is_active=True).order_by("id"):
        orders.append(run_random_monkey_order(monkey.id))
    return {
        "enabled": True,
        "orders": len(orders),
        "order_ids": [order.id for order in orders],
    }


def run_random_monkey_order(monkey_id, kis_client=None, rng=None):
    rng = rng or random
    monkey = Monkey.objects.get(pk=monkey_id)
    order_type = rng.choice([Order.OrderTypeChoices.BUY, Order.OrderTypeChoices.SELL])
    quantity = 1

    if order_type == Order.OrderTypeChoices.BUY:
        stock = Stock.objects.filter(is_active=True).order_by("?").first()
        if not stock:
            return Order.objects.create(
                monkey=monkey,
                stock=_placeholder_stock(),
                order_type=order_type,
                requested_quantity=quantity,
                status=Order.StatusChoices.SKIPPED,
                failure_reason="No stock is available.",
            )
    else:
        holding = (
            Holding.objects.filter(monkey=monkey, quantity__gt=0)
            .select_related("stock")
            .order_by("?")
            .first()
        )
        if not holding:
            stock = Stock.objects.order_by("?").first()
            if stock is None:
                stock = _placeholder_stock()
            return Order.objects.create(
                monkey=monkey,
                stock=stock,
                order_type=order_type,
                requested_quantity=quantity,
                status=Order.StatusChoices.SKIPPED,
                failure_reason="Monkey has no holdings to sell.",
            )
        stock = holding.stock

    order = submit_monkey_order(
        monkey_id=monkey.id,
        stock_id=stock.id,
        order_type=order_type,
        quantity=quantity,
        kis_client=kis_client,
    )
    # Kill check outside the atomic block: refresh balance first since submit_monkey_order
    # updated it in its own transaction.
    monkey.refresh_from_db()
    maybe_kill_monkey(monkey)
    return order


@transaction.atomic
def submit_monkey_order(monkey_id, stock_id, order_type, quantity, kis_client=None):
    monkey = Monkey.objects.select_for_update().get(pk=monkey_id)
    stock = Stock.objects.get(pk=stock_id)
    kis_client = kis_client or KisClient()

    order = Order.objects.create(
        monkey=monkey,
        stock=stock,
        order_type=order_type,
        requested_quantity=quantity,
    )

    try:
        estimated_price = kis_client.get_stock_price(stock.ticker)
    except (KisClientError, ValueError) as exc:
        return _fail_order(order, f"Could not fetch stock price: {exc}")

    order.estimated_price = estimated_price
    order.save(update_fields=["estimated_price", "updated_at"])

    total_price = estimated_price * quantity
    if order_type == Order.OrderTypeChoices.BUY and monkey.balance < total_price:
        return _fail_order(order, "Insufficient monkey balance.")

    holding = (
        Holding.objects.select_for_update().filter(monkey=monkey, stock=stock).first()
    )
    if order_type == Order.OrderTypeChoices.SELL:
        held_quantity = holding.quantity if holding else 0
        if held_quantity < quantity:
            return _fail_order(order, "Insufficient monkey holdings.")

    try:
        request_payload, response_data = kis_client.order_stock(
            order_type=order_type,
            ticker=stock.ticker,
            quantity=quantity,
        )
    except KisClientError as exc:
        order.kis_request = {
            "ticker": stock.ticker,
            "quantity": quantity,
            "order_type": int(order_type),
        }
        order.save(update_fields=["kis_request", "updated_at"])
        return _fail_order(order, f"KIS order request failed: {exc}")

    order.kis_request = request_payload
    order.kis_response = response_data
    order.kis_order_status = str(response_data.get("msg1") or "")
    output = response_data.get("output") or {}
    order.kis_order_id = str(
        output.get("ODNO")
        or output.get("odno")
        or output.get("KRX_FWDG_ORD_ORGNO")
        or ""
    )

    if str(response_data.get("rt_cd")) != "0":
        order.save(
            update_fields=[
                "kis_request",
                "kis_response",
                "kis_order_status",
                "kis_order_id",
                "updated_at",
            ]
        )
        return _fail_order(order, response_data.get("msg1") or "KIS rejected order.")

    if order_type == Order.OrderTypeChoices.BUY:
        monkey.balance -= total_price
        monkey.save(update_fields=["balance"])
        holding, _ = Holding.objects.select_for_update().get_or_create(
            monkey=monkey,
            stock=stock,
            defaults={"quantity": 0},
        )
        holding.quantity += quantity
        holding.save(update_fields=["quantity"])
    else:
        monkey.balance += total_price
        monkey.save(update_fields=["balance"])
        holding.quantity -= quantity
        holding.save(update_fields=["quantity"])

    order.executed_quantity = quantity
    order.executed_price = estimated_price
    order.status = Order.StatusChoices.SUCCEEDED
    order.save(
        update_fields=[
            "status",
            "executed_quantity",
            "executed_price",
            "kis_request",
            "kis_response",
            "kis_order_status",
            "kis_order_id",
            "updated_at",
        ]
    )
    return order


def _fail_order(order, reason):
    order.status = Order.StatusChoices.FAILED
    order.failure_reason = str(reason)
    order.save(update_fields=["status", "failure_reason", "updated_at"])
    return order


def _placeholder_stock(ticker="UNKNOWN"):
    stock, _ = Stock.objects.get_or_create(
        market="UNKNOWN",
        ticker=ticker,
        defaults={
            "name": "Unknown stock"
            if ticker == "UNKNOWN"
            else f"Unknown stock ({ticker})",
        },
    )
    return stock


def get_or_create_system_monkey():
    """Hidden monkey that absorbs/liquidates orphaned real-account positions."""
    monkey, _ = Monkey.objects.get_or_create(
        is_system=True,
        defaults={
            "name": "(시스템)",
            "is_active": False,
            "balance": 0,
            "initial_balance": 0,
            "order_interval_seconds": 60,
        },
    )
    return monkey


def liquidate_holdings_for_monkey(monkey, stock_ids=None, kis_client=None):
    """Sell off a monkey's holdings via the normal order pipeline.

    Shared by system-monkey reconciliation, delisted-stock liquidation, and
    kill_monkey() so every liquidation leaves the same Order audit trail.
    """
    kis_client = kis_client or KisClient()
    holdings = Holding.objects.filter(monkey=monkey, quantity__gt=0)
    if stock_ids is not None:
        holdings = holdings.filter(stock_id__in=stock_ids)

    orders = []
    for holding in list(holdings):
        orders.append(
            submit_monkey_order(
                monkey_id=monkey.id,
                stock_id=holding.stock_id,
                order_type=Order.OrderTypeChoices.SELL,
                quantity=holding.quantity,
                kis_client=kis_client,
            )
        )
    return orders


def _absorb_excess(ticker, excess_qty, kis_client):
    """A ticker is held in the real KIS account but not owned by any monkey locally."""
    stock = Stock.objects.filter(ticker=ticker).order_by(
        "id"
    ).first() or _placeholder_stock(ticker)
    system_monkey = get_or_create_system_monkey()

    holding, _ = Holding.objects.get_or_create(
        monkey=system_monkey, stock=stock, defaults={"quantity": 0}
    )
    holding.quantity += excess_qty
    holding.save(update_fields=["quantity"])

    orders = liquidate_holdings_for_monkey(
        system_monkey, stock_ids=[stock.id], kis_client=kis_client
    )
    return {
        "ticker": ticker,
        "quantity": excess_qty,
        "order_ids": [order.id for order in orders],
    }


def _clamp_phantom_holdings(ticker, phantom_qty):
    """Local Holding totals for a ticker exceed the real KIS account quantity.

    Deliberate exception to "never mutate Holding.quantity outside
    submit_monkey_order" — this is a reconciliation sweep, not a trade.
    """
    remaining = phantom_qty
    affected = []
    for holding in Holding.objects.filter(
        stock__ticker=ticker, quantity__gt=0
    ).order_by("-quantity"):
        if remaining <= 0:
            break
        reduction = min(remaining, holding.quantity)
        holding.quantity -= reduction
        holding.save(update_fields=["quantity"])
        remaining -= reduction
        affected.append({"holding_id": holding.id, "reduced_by": reduction})

    return {
        "ticker": ticker,
        "quantity": phantom_qty,
        "holdings": affected,
    }


def reconcile_holdings(kis_client=None):
    """Compare real KIS account holdings against the local ledger and fix mismatches.

    Real > local ("leaked" stock untracked by any monkey) is absorbed into the
    hidden system monkey and sold off. Local > real ("phantom" holdings, the
    local ledger overcounts reality) is clamped down to match reality.
    """
    kis_client = kis_client or KisClient()
    real = kis_client.get_account_balance()["holdings"]
    local = dict(
        Holding.objects.filter(quantity__gt=0)
        .values("stock__ticker")
        .annotate(total=Sum("quantity"))
        .values_list("stock__ticker", "total")
    )

    absorbed = []
    clamped = []
    for ticker in set(real) | set(local):
        real_qty = real.get(ticker, 0)
        local_qty = local.get(ticker, 0)
        if real_qty > local_qty:
            absorbed.append(_absorb_excess(ticker, real_qty - local_qty, kis_client))
        elif local_qty > real_qty:
            clamped.append(_clamp_phantom_holdings(ticker, local_qty - real_qty))

    return {"absorbed": absorbed, "clamped": clamped}


def liquidate_orphaned_holdings():
    """Daily reconciliation: absorb/clamp real-vs-local mismatches and sell off
    holdings of delisted stocks. Gated on the global kill switch, same as
    run_active_monkeys()."""
    if not get_global_control().enabled:
        return {"enabled": False}

    kis_client = KisClient()
    reconciliation = reconcile_holdings(kis_client=kis_client)

    by_monkey = {}
    for holding in Holding.objects.filter(
        quantity__gt=0, stock__is_active=False
    ).select_related("monkey", "stock"):
        by_monkey.setdefault(holding.monkey, []).append(holding.stock_id)

    delisted_orders = 0
    for monkey, stock_ids in by_monkey.items():
        orders = liquidate_holdings_for_monkey(
            monkey, stock_ids=stock_ids, kis_client=kis_client
        )
        delisted_orders += len(orders)

    return {
        "enabled": True,
        "reconciliation": reconciliation,
        "delisted_orders": delisted_orders,
    }
