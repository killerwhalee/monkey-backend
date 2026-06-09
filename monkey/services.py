import random

from django.db import transaction
from django.db.models import Avg, Max
from django.utils import timezone

from market.models import Holding, Order, Stock
from monkey.kis import KisClient, KisClientError
from monkey.models import GlobalMonkeyControl, Monkey, MonkeyDailySnapshot


def get_global_control():
    control, _ = GlobalMonkeyControl.objects.get_or_create(
        pk=1,
        defaults={"enabled": False},
    )
    return control


def snapshot_all_monkeys(target_date=None):
    # Deferred import: serializers.py does `from monkey import services`, so importing
    # build_monkey_metrics at module load time would create a circular import (same
    # pattern as the deferred `from market.models import Order` in monkey/kis.py).
    from monkey.serializers import build_monkey_metrics

    target_date = target_date or timezone.localdate()
    count = 0
    for monkey in Monkey.objects.all().order_by("id"):
        MonkeyDailySnapshot.objects.update_or_create(
            monkey=monkey,
            date=target_date,
            defaults=build_monkey_metrics(monkey),
        )
        count += 1
    return {"date": target_date.isoformat(), "snapshots": count}


def build_dashboard_summary():
    from monkey.serializers import build_monkey_metrics

    ratios = [
        build_monkey_metrics(monkey)["earning_ratio"] for monkey in Monkey.objects.all()
    ]

    daily_series = list(
        MonkeyDailySnapshot.objects.values("date")
        .annotate(
            average_earning_ratio=Avg("earning_ratio"),
            best_earning_ratio=Max("earning_ratio"),
        )
        .order_by("date")
    )

    return {
        "active_monkey_count": Monkey.objects.filter(is_active=True).count(),
        "average_earning_ratio": (sum(ratios) / len(ratios)) if ratios else 0.0,
        "best_earning_ratio": max(ratios) if ratios else 0.0,
        "latest_orders": (
            Order.objects.select_related("monkey", "stock").order_by("-created_at")[:5]
        ),
        "daily_earning_ratio_series": daily_series,
    }


def create_monkeys(count, starting_balance, min_quantity=1, max_quantity=1):
    monkeys = [
        Monkey(
            name=f"Monkey {Monkey.objects.count() + index + 1}",
            balance=starting_balance,
            initial_balance=starting_balance,
            min_quantity=min_quantity,
            max_quantity=max_quantity,
        )
        for index in range(count)
    ]
    return Monkey.objects.bulk_create(monkeys)


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
    quantity = rng.randint(monkey.min_quantity, monkey.max_quantity)

    if order_type == Order.OrderTypeChoices.BUY:
        stock = Stock.objects.order_by("?").first()
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

    return submit_monkey_order(
        monkey_id=monkey.id,
        stock_id=stock.id,
        order_type=order_type,
        quantity=quantity,
        kis_client=kis_client,
    )


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


def _placeholder_stock():
    stock, _ = Stock.objects.get_or_create(
        market="UNKNOWN",
        ticker="UNKNOWN",
        defaults={"name": "Unknown stock"},
    )
    return stock
