import logging
import random
import time
from datetime import timedelta

from django.db import OperationalError, transaction
from django.db.models import Sum
from django.utils import timezone

from market.models import Holding, Order, Stock
from monkey.kis import KisClient, KisClientError
from monkey.models import (
    GlobalMonkeyControl,
    Monkey,
    MonkeyDailySnapshot,
    MonkeyIndexBaseline,
    MonkeyIndexTick,
)
from monkey.names import generate_monkey_name

logger = logging.getLogger(__name__)

# Cold-start value for the Monkey Index, used only when no index has ever been
# recorded; afterwards each day chains off the previous day's closing value.
MONKEY_INDEX_BASE = 10000.0


class InsufficientCashError(Exception):
    """Raised when there isn't enough unallocated KIS cash to create monkeys."""


def get_global_control():
    control, _ = GlobalMonkeyControl.objects.get_or_create(pk=1)
    return control


def set_trading_enabled(enabled: bool, note: str = "") -> GlobalMonkeyControl:
    """Open/close the *time* gate. Used by market_open / market_close tasks."""
    control = get_global_control()
    control.time_enabled = enabled
    if note:
        control.note = note
    control.save(update_fields=["time_enabled", "note", "updated_at"])
    return control


def set_holiday_closed(is_holiday: bool, note: str = "") -> GlobalMonkeyControl:
    """Open/close the *holiday* gate. Used by the daily check_holiday task."""
    control = get_global_control()
    control.holiday_enabled = not is_holiday
    if note:
        control.note = note
    control.save(update_fields=["holiday_enabled", "note", "updated_at"])
    return control


def sync_monkey_periodic_tasks():
    """Enable/disable scheduled tasks to match the trading gate.

    Called at market open/close and after the holiday check so beat stops
    enqueuing per-monkey orders (and market-hours price polling) outside trading
    hours — which is what crowded the queue post-market. Only ACTIVE monkeys'
    tasks are (re-)enabled; INACTIVE stay paused and DEAD have no task.
    """
    from django_celery_beat.models import PeriodicTask, PeriodicTasks

    control = get_global_control()
    monkey_active = control.enabled  # all three gates — governs actual monkey trading
    market_open = (
        control.market_open
    )  # time + holiday only — governs market-hours tasks

    active_names = {
        f"monkey.run.{pk}"
        for pk in Monkey.objects.filter(state=Monkey.State.ACTIVE).values_list(
            "pk", flat=True
        )
    }

    enabled_count = 0
    for task in PeriodicTask.objects.filter(task="monkey.tasks.run_monkey"):
        desired = monkey_active and task.name in active_names
        if task.enabled != desired:
            PeriodicTask.objects.filter(pk=task.pk).update(enabled=desired)
        enabled_count += int(desired)

    # These tasks are market-hours tasks: they run whenever the exchange is open,
    # regardless of whether the manual kill-switch is set.
    PeriodicTask.objects.filter(name="monkey.update_held_stock_prices").update(
        enabled=market_open
    )
    PeriodicTask.objects.filter(name="monkey.run_system").update(enabled=market_open)
    PeriodicTask.objects.filter(name="monkey.index_tick").update(enabled=market_open)

    # Bulk .update() bypasses the post_save signal beat listens on — nudge it.
    PeriodicTasks.update_changed()
    return {"gate_open": monkey_active, "active_monkey_tasks": enabled_count}


class ScheduleNotFoundError(Exception):
    """Raised when a requested PeriodicTask schedule doesn't exist."""


class NotATimeScheduleError(Exception):
    """Raised when trying to set a time on a non-crontab (interval) task."""


def _crontab_hour_minute(crontab):
    """Concrete (hour, minute) for a crontab, or (None, None) for ranges/wildcards."""
    try:
        return int(crontab.hour), int(crontab.minute)
    except (TypeError, ValueError):
        return None, None


def _serialize_task_schedule(task):
    from monkey.task_catalog import DESCRIPTION_BY_TASK_PATH, LABEL_BY_TASK_PATH

    hour, minute = _crontab_hour_minute(task.crontab)
    return {
        "id": task.id,
        "name": task.name,
        "label": LABEL_BY_TASK_PATH.get(task.task, task.name),
        "description": DESCRIPTION_BY_TASK_PATH.get(task.task, ""),
        "task": task.task,
        "hour": hour,
        "minute": minute,
        "enabled": task.enabled,
    }


def list_task_schedules():
    """Crontab-scheduled (daily) tasks, ascending by time of day.

    Interval-based tasks (per-monkey orders, market-hours price polling) have no
    time of day, so they're excluded — only ``crontab`` schedules show here.
    """
    from django_celery_beat.models import PeriodicTask

    rows = [
        _serialize_task_schedule(task)
        for task in PeriodicTask.objects.filter(crontab__isnull=False)
        .exclude(task="celery.backend_cleanup")
        .select_related("crontab")
    ]
    # Ascending by scheduled time; any non-concrete time sorts last.
    rows.sort(key=lambda r: (r["hour"] is None, r["hour"] or 0, r["minute"] or 0))
    return rows


def get_market_hours():
    """Public-facing market schedule times derived from the crontab beat tasks.

    Returns the concrete (hour, minute) for market open/close and the daily
    holiday check so the dashboard can show live values instead of hard-coding
    09:00/15:30/08:00. A value is ``None`` if the schedule is a range/wildcard.
    """
    from django_celery_beat.models import PeriodicTask

    names = {
        "open": "market.auto.open",
        "close": "market.auto.close",
        "holiday_check": "monkey.check_holiday",
    }
    tasks = {
        task.name: task
        for task in PeriodicTask.objects.filter(
            name__in=names.values(), crontab__isnull=False
        ).select_related("crontab")
    }

    result = {}
    for key, name in names.items():
        task = tasks.get(name)
        hour, minute = _crontab_hour_minute(task.crontab) if task else (None, None)
        result[key] = {"hour": hour, "minute": minute}
    return result


def set_task_schedule(task_id, hour, minute):
    """Repoint a crontab task to a new time of day (day-of-week etc. unchanged)."""
    from django_celery_beat.models import CrontabSchedule, PeriodicTask

    try:
        task = PeriodicTask.objects.select_related("crontab").get(pk=task_id)
    except PeriodicTask.DoesNotExist as exc:
        raise ScheduleNotFoundError(str(task_id)) from exc
    if task.crontab is None:
        raise NotATimeScheduleError(task.name)

    old = task.crontab
    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute=str(minute),
        hour=str(hour),
        day_of_week=old.day_of_week,
        day_of_month=old.day_of_month,
        month_of_year=old.month_of_year,
        timezone=old.timezone,
    )
    if schedule.pk != old.pk:
        task.crontab = schedule
        # save() fires the post_save signal beat watches, so it reloads.
        task.save(update_fields=["crontab"])
        # Drop the old schedule if nothing else references it.
        if not PeriodicTask.objects.filter(crontab=old).exists():
            old.delete()
        logger.info(
            "rescheduled %s to %02d:%02d KST", task.name, int(hour), int(minute)
        )
    return _serialize_task_schedule(task)


# Per-monkey order tasks are interval-scheduled too, but they're managed per
# monkey (not by an admin), so the interval table hides them.
_PER_MONKEY_TASK = "monkey.tasks.run_monkey"


def _serialize_interval_schedule(task):
    from monkey.task_catalog import DESCRIPTION_BY_TASK_PATH, LABEL_BY_TASK_PATH

    return {
        "id": task.id,
        "name": task.name,
        "label": LABEL_BY_TASK_PATH.get(task.task, task.name),
        "description": DESCRIPTION_BY_TASK_PATH.get(task.task, ""),
        "task": task.task,
        "every": task.interval.every if task.interval else None,
        "period": task.interval.period if task.interval else None,
        "enabled": task.enabled,
    }


def list_interval_schedules():
    """System interval tasks (e.g. price polling, earning-ratio ticks).

    Per-monkey order tasks are excluded — they're configured per monkey, not here.
    """
    from django_celery_beat.models import PeriodicTask

    rows = [
        _serialize_interval_schedule(task)
        for task in PeriodicTask.objects.filter(interval__isnull=False)
        .exclude(task=_PER_MONKEY_TASK)
        .select_related("interval")
    ]
    rows.sort(key=lambda r: (r["every"] is None, r["every"] or 0))
    return rows


def set_interval_schedule(task_id, every):
    """Change a system interval task's cadence (seconds), keeping its period."""
    from django_celery_beat.models import IntervalSchedule, PeriodicTask

    try:
        task = PeriodicTask.objects.select_related("interval").get(pk=task_id)
    except PeriodicTask.DoesNotExist as exc:
        raise ScheduleNotFoundError(str(task_id)) from exc
    if task.interval is None:
        raise NotATimeScheduleError(task.name)
    if task.task == _PER_MONKEY_TASK:
        raise NotATimeScheduleError(task.name)

    old = task.interval
    interval, _ = IntervalSchedule.objects.get_or_create(
        every=every,
        period=old.period,
    )
    if interval.pk != old.pk:
        task.interval = interval
        # save() fires the post_save signal beat watches, so it reloads.
        task.save(update_fields=["interval"])
        # Drop the old interval if nothing else references it.
        if not PeriodicTask.objects.filter(interval=old).exists():
            old.delete()
        logger.info("rescheduled %s to every %ss", task.name, every)
    return _serialize_interval_schedule(task)


class KillNotAllowedError(Exception):
    """Raised when a monkey can't be killed because its holdings can't be liquidated."""


def kill_monkey(monkey: Monkey) -> Monkey:
    """
    Kill a monkey: hand its holdings to the system monkey and mark it DEAD. Single
    path for both auto-kill (maybe_kill_monkey) and admin force-kill.

    Holdings are *transferred* (DB-only) to the system monkey, which liquidates
    them later via its own periodic task — so killing no longer requires the market
    to be open. The Monkey.save() override drops the dead monkey's PeriodicTask.
    """
    transfer_holdings_to_system_monkey(monkey)
    monkey.state = Monkey.State.DEAD
    monkey.killed_at = timezone.now()
    monkey.save(update_fields=["state", "killed_at"])
    return monkey


def kill_underperforming_monkeys() -> int:
    """Kill every alive monkey whose earning_ratio is below the kill threshold.

    Run as a daily off-market task (see run_daily_maintenance) rather than per
    trade, so the alive set stays fixed during a trading session and the Monkey
    Index baseline/live equity remain comparable. Returns the number killed.
    """
    # Deferred import: serializers.py does `from monkey import services`.
    from monkey.serializers import build_monkey_metrics

    threshold = get_global_control().kill_threshold
    killed = 0
    for monkey in _alive_monkeys():
        if build_monkey_metrics(monkey)["earning_ratio"] < threshold:
            kill_monkey(monkey)
            killed += 1
    return killed


def snapshot_all_monkeys(target_date=None):
    if get_global_control().market_open:
        return {"skipped": "market_open"}
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
    monkeys = list(Monkey.objects.filter(is_system=False))
    active_monkeys = [monkey for monkey in monkeys if monkey.is_active]
    average_interval = (
        round(
            sum(monkey.order_interval_seconds for monkey in active_monkeys)
            / len(active_monkeys)
        )
        if active_monkeys
        else 0
    )

    monkey_index = current_index_value()
    baseline = MonkeyIndexBaseline.objects.filter(date=timezone.localdate()).first()
    base_index = baseline.base_index if baseline else monkey_index
    monkey_index_change = (monkey_index / base_index - 1) if base_index else 0.0

    return {
        "active_monkey_count": len(active_monkeys),
        "monkey_index": monkey_index,
        "monkey_index_open": base_index,
        "monkey_index_change": monkey_index_change,
        "average_order_interval_seconds": average_interval,
        "latest_orders": (
            Order.objects.filter(status=Order.StatusChoices.SUCCEEDED)
            .select_related("monkey", "stock")
            .exclude(monkey__is_system=True)
            .order_by("-created_at")[:10]
        ),
    }


def _alive_monkeys():
    """Monkeys whose equity counts toward the index: alive (ACTIVE or INACTIVE),
    non-system. DEAD monkeys are excluded so the alive set stays fixed during a
    session (killing only happens off-market)."""
    return Monkey.objects.filter(is_system=False).exclude(state=Monkey.State.DEAD)


def _alive_equity():
    """Summed total equity of alive monkeys (``a`` at open / ``b`` while open)."""
    from monkey.serializers import build_monkey_metrics

    return sum(
        build_monkey_metrics(monkey)["total_equity"] for monkey in _alive_monkeys()
    )


def capture_index_baseline(target_date=None):
    """Record today's index baseline: ``base_equity`` (alive equity now) and
    ``base_index`` (yesterday's closing index, carried forward). Called at market
    open before any ticks are sampled."""
    target_date = target_date or timezone.localdate()

    last_tick = (
        MonkeyIndexTick.objects.filter(recorded_at__date__lt=target_date)
        .order_by("recorded_at")
        .last()
    )
    if last_tick is not None:
        base_index = last_tick.value
    else:
        prior_baseline = (
            MonkeyIndexBaseline.objects.filter(date__lt=target_date)
            .order_by("date")
            .last()
        )
        base_index = prior_baseline.base_index if prior_baseline else MONKEY_INDEX_BASE

    baseline, _ = MonkeyIndexBaseline.objects.update_or_create(
        date=target_date,
        defaults={"base_index": base_index, "base_equity": _alive_equity()},
    )
    return baseline


def record_index_tick():
    """Sample the current Monkey Index value: ``base_index * (b / a)``. Gated on
    the global kill switch (mirrors run_monkey) so each day's ticks form a clean
    trading-session candle. Skips if today's baseline hasn't been captured yet."""
    if not get_global_control().market_open:
        return {"market_open": False}

    baseline = MonkeyIndexBaseline.objects.filter(date=timezone.localdate()).first()
    if baseline is None:
        return {"enabled": True, "baseline": False}

    if baseline.base_equity:
        value = baseline.base_index * (_alive_equity() / baseline.base_equity)
    else:
        value = baseline.base_index
    tick = MonkeyIndexTick.objects.create(value=value)
    return {"enabled": True, "tick_id": tick.id, "value": value}


def current_index_value():
    """Latest recorded index value: today's last tick, else today's base_index,
    else the cold-start base."""
    today = timezone.localdate()
    last_tick = (
        MonkeyIndexTick.objects.filter(recorded_at__date=today)
        .order_by("recorded_at")
        .last()
    )
    if last_tick is not None:
        return last_tick.value
    baseline = MonkeyIndexBaseline.objects.filter(date=today).first()
    if baseline is not None:
        return baseline.base_index
    last_baseline = MonkeyIndexBaseline.objects.order_by("date").last()
    return last_baseline.base_index if last_baseline else MONKEY_INDEX_BASE


CANDLE_UNIT_SECONDS = {
    "1m": 60,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

# lightweight-charts renders UTCTimestamp in UTC; KRX runs in KST (a constant
# +9h, no DST), so we offset bucket times to make the chart show Seoul
# wall-clock (e.g. the 09:00 open appears at 09:00, not 00:00).
KST_OFFSET_SECONDS = 9 * 3600


def build_index_candlesticks(unit="1d", limit=120):
    """Bucket per-minute Monkey Index ticks into OHLC candlesticks.

    ``unit`` is one of ``CANDLE_UNIT_SECONDS``. Each candle's ``time`` is the
    bucket-start as epoch seconds (what lightweight-charts expects). ``1d``
    buckets align to the local trading day; intraday units floor the absolute
    timestamp to the unit width.
    """
    seconds = CANDLE_UNIT_SECONDS.get(unit, CANDLE_UNIT_SECONDS["1d"])
    buckets = {}
    for recorded_at, value in MonkeyIndexTick.objects.order_by(
        "recorded_at"
    ).values_list("recorded_at", "value"):
        if unit == "1d":
            local = timezone.localtime(recorded_at)
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
            bucket = int(midnight.timestamp()) + KST_OFFSET_SECONDS
        else:
            ts = int(recorded_at.timestamp()) + KST_OFFSET_SECONDS
            bucket = ts - (ts % seconds)
        buckets.setdefault(bucket, []).append(value)

    candlesticks = [
        {
            "time": bucket,
            "open": values[0],
            "high": max(values),
            "low": min(values),
            "close": values[-1],
        }
        for bucket, values in sorted(buckets.items())
    ]
    return candlesticks[-limit:]


def update_held_stock_prices(kis_client=None):
    """Refresh live prices for every stock currently held by any monkey.

    Gated on the global switch like record_earning_ratio_tick so prices are only
    polled during a live trading session. These prices feed holdings valuation,
    average-price/earning-rate breakdowns, and equity metrics.
    """
    if not get_global_control().market_open:
        return {"market_open": False}

    kis_client = kis_client or KisClient()
    stock_ids = list(
        Holding.objects.filter(quantity__gt=0, stock__is_active=True)
        .values_list("stock_id", flat=True)
        .distinct()
    )

    now = timezone.now()
    updated = 0
    for stock in Stock.objects.filter(id__in=stock_ids):
        try:
            price = kis_client.get_stock_price(stock.ticker)
        except (KisClientError, ValueError):
            continue
        stock.current_price = price
        stock.price_updated_at = now
        stock.save(update_fields=["current_price", "price_updated_at"])
        updated += 1
    return {"enabled": True, "updated": updated}


def reconcile_order_executions(kis_client=None, lookback_days=1):
    """Correct recently-succeeded orders with their real KIS fills.

    Market orders fill at prices that differ from the pre-trade estimate we
    optimistically recorded. This polls the daily order-execution inquiry and,
    matching by KIS order number (ODNO), updates each order's executed quantity
    and price so the FIFO average-price/earning-rate math reflects reality. Each
    order maps to one ODNO, so multiple buys/sells reconcile independently.
    """
    if get_global_control().market_open:
        return {"skipped": "market_open"}

    kis_client = kis_client or KisClient()
    start = timezone.localdate() - timedelta(days=lookback_days)
    executions = kis_client.get_daily_order_executions(start_date=start)
    if not executions:
        return {"reconciled": 0}

    reconciled = 0
    cutoff = timezone.now() - timedelta(days=lookback_days + 1)
    orders = Order.objects.filter(
        status=Order.StatusChoices.SUCCEEDED,
        created_at__gte=cutoff,
    ).exclude(kis_order_id="")
    for order in orders:
        fill = executions.get(order.kis_order_id.lstrip("0"))
        if not fill:
            continue
        executed_quantity = fill["executed_quantity"]
        avg_price = fill["avg_price"] or order.executed_price
        if (
            order.executed_quantity == executed_quantity
            and order.executed_price == avg_price
        ):
            continue
        order.executed_quantity = executed_quantity
        order.executed_price = avg_price
        order.save(update_fields=["executed_quantity", "executed_price", "updated_at"])
        reconciled += 1
    return {"reconciled": reconciled}


def build_account_summary(kis_client=None):
    """Live KIS-account asset snapshot for the manage page.

    Asset figures (cash/holdings/total/P&L/earning rate) come straight from the
    KIS account inquiry — not the local DB — so they reflect the real paper
    account regardless of monkey bookkeeping. Monkey counts and unallocated cash
    are still derived locally since they have no KIS equivalent.
    """
    kis_client = kis_client or KisClient()
    balance = kis_client.get_account_balance(include_holdings=False)

    monkeys = list(Monkey.objects.filter(is_system=False))
    total_monkey_balance = sum(monkey.balance for monkey in monkeys)

    return {
        "kis_cash_balance": balance["cash_balance"],
        "kis_holdings_value": balance["securities_value"],
        "kis_total_assets": balance["total_assets"],
        "kis_total_pl": balance["total_pl"],
        "kis_earning_rate": balance["earning_rate"],
        "unallocated_cash": balance["cash_balance"] - total_monkey_balance,
        "monkey_count": len(monkeys),
        "active_monkey_count": sum(monkey.is_active for monkey in monkeys),
    }


def unallocated_cash(kis_client=None):
    """KIS account cash not yet allocated to a *living* monkey.

    Dead monkeys keep their ``balance`` (for the future gravestone view) but are
    excluded here, so their cash is freed back into circulation for new monkeys.
    """
    kis_client = kis_client or KisClient()
    kis_cash = kis_client.get_account_balance(include_holdings=False)["cash_balance"]
    allocated = (
        Monkey.objects.filter(is_system=False)
        .exclude(state=Monkey.State.DEAD)
        .aggregate(total=Sum("balance"))["total"]
        or 0
    )
    return kis_cash - allocated


def create_monkeys(count, starting_balance):
    # Individual saves (not bulk_create) so Monkey.save() fires and creates PeriodicTasks.
    # The random order-interval range is configurable via GlobalMonkeyControl.
    control = get_global_control()
    low = control.auto_create_min_interval_seconds
    high = control.auto_create_max_interval_seconds
    monkeys = []
    for _ in range(count):
        monkey = Monkey(
            name=generate_monkey_name(),
            balance=starting_balance,
            initial_balance=starting_balance,
            order_interval_seconds=random.randint(low, high),
        )
        monkey.save()
        monkeys.append(monkey)
    return monkeys


def create_monkeys_checked(count, starting_balance, kis_client=None):
    """Create monkeys only if the KIS account has enough unallocated cash.

    Used by the admin bulk-create path; raises ``InsufficientCashError`` instead
    of silently over-allocating beyond the real account balance.
    """
    available = unallocated_cash(kis_client=kis_client)
    needed = count * starting_balance
    if needed > available:
        raise InsufficientCashError(
            f"미배정 잔고가 부족합니다. 필요: {needed:,}원, 가용: {available:,}원."
        )
    return create_monkeys(count=count, starting_balance=starting_balance)


def auto_create_monkeys(kis_client=None):
    """Create as many new monkeys as the KIS account's unallocated cash affords."""
    kis_client = kis_client or KisClient()
    starting_balance = get_global_control().auto_create_starting_balance
    if starting_balance <= 0:
        return []
    available = unallocated_cash(kis_client=kis_client)
    count = available // starting_balance
    if count <= 0:
        return []
    return create_monkeys(count=count, starting_balance=starting_balance)


def run_active_monkeys():
    if not get_global_control().enabled:
        return {"enabled": False, "orders": 0}

    orders = []
    for monkey in Monkey.objects.filter(state=Monkey.State.ACTIVE).order_by("id"):
        try:
            orders.append(run_random_monkey_order(monkey.id))
        except Exception:
            # One monkey's failure must not abort the whole batch.
            logger.exception("run_random_monkey_order failed for monkey %s", monkey.id)
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
    # Underperformers are no longer culled here — killing is a daily off-market
    # task (run_daily_maintenance) so the alive set stays fixed during a session.
    return order


def run_system_monkey_order(kis_client=None, rng=None):
    """Sell off one random system-monkey holding (full quantity) through KIS.

    The system monkey gradually liquidates the orphaned/dead-monkey holdings handed
    to it. It never buys and never dies, and its sale proceeds are not retained (see
    _apply_confirmed_order) — freed cash returns to the unallocated pool. Returns
    None when it has nothing to sell.
    """
    rng = rng or random
    system_monkey = get_or_create_system_monkey()
    holdings = list(
        Holding.objects.filter(monkey=system_monkey, quantity__gt=0).select_related(
            "stock"
        )
    )
    if not holdings:
        return None

    holding = rng.choice(holdings)
    return submit_monkey_order(
        monkey_id=system_monkey.id,
        stock_id=holding.stock_id,
        order_type=Order.OrderTypeChoices.SELL,
        quantity=holding.quantity,
        kis_client=kis_client,
    )


def submit_monkey_order(monkey_id, stock_id, order_type, quantity, kis_client=None):
    """Place one order through KIS and keep the local ledger consistent.

    The slow KIS HTTP round-trip happens **outside** any DB transaction/row lock
    (holding a lock across it was the source of the "database is locked" storms
    and the "bought but no holding" money-loss bug). Only after KIS confirms the
    fill do we mutate balance/Holding inside a short, retried atomic block.
    """
    monkey = Monkey.objects.get(pk=monkey_id)
    stock = Stock.objects.get(pk=stock_id)
    kis_client = kis_client or KisClient()

    order = Order.objects.create(
        monkey=monkey,
        stock=stock,
        order_type=order_type,
        requested_quantity=quantity,
    )

    # Prefer the periodically-refreshed cached price (one fewer KIS call per
    # order); only fetch live when we have nothing cached.
    estimated_price = stock.current_price
    if not estimated_price:
        try:
            estimated_price = kis_client.get_stock_price(stock.ticker)
        except (KisClientError, ValueError) as exc:
            return _fail_order(order, f"Could not fetch stock price: {exc}")

    order.estimated_price = estimated_price
    order.save(update_fields=["estimated_price", "updated_at"])

    total_price = estimated_price * quantity

    # Pre-trade validation against the current ledger (no lock held).
    if order_type == Order.OrderTypeChoices.BUY and monkey.balance < total_price:
        return _fail_order(order, "Insufficient monkey balance.")
    if order_type == Order.OrderTypeChoices.SELL:
        held_quantity = (
            Holding.objects.filter(monkey=monkey, stock=stock)
            .values_list("quantity", flat=True)
            .first()
            or 0
        )
        if held_quantity < quantity:
            return _fail_order(order, "Insufficient monkey holdings.")

    # Place the order with NO DB transaction/lock held across the HTTP call.
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

    # KIS confirmed the fill — now apply the local ledger change atomically.
    _apply_confirmed_order(
        order, monkey_id, stock_id, order_type, quantity, estimated_price, total_price
    )
    return order


def _apply_confirmed_order(
    order, monkey_id, stock_id, order_type, quantity, price, total_price
):
    """Mutate balance/Holding for a KIS-confirmed order in a short locked txn.

    Retries on transient DB lock errors. Once KIS has executed the trade we must
    record it locally, so on the (Postgres-unlikely) exhaustion case we still
    mark the order SUCCEEDED and leave the holdings reconciliation as backstop.
    """
    update_fields = [
        "status",
        "executed_quantity",
        "executed_price",
        "kis_request",
        "kis_response",
        "kis_order_status",
        "kis_order_id",
        "updated_at",
    ]
    order.executed_quantity = quantity
    order.executed_price = price
    order.status = Order.StatusChoices.SUCCEEDED

    for attempt in range(3):
        try:
            with transaction.atomic():
                monkey = Monkey.objects.select_for_update().get(pk=monkey_id)
                if order_type == Order.OrderTypeChoices.BUY:
                    monkey.balance -= total_price
                    monkey.save(update_fields=["balance"])
                    holding, _ = Holding.objects.select_for_update().get_or_create(
                        monkey=monkey,
                        stock_id=stock_id,
                        defaults={"quantity": 0},
                    )
                    holding.quantity += quantity
                    holding.save(update_fields=["quantity"])
                else:
                    # The system monkey never retains cash: its sale proceeds are
                    # left in the account as unallocated funds, so skip the balance
                    # credit and keep its balance at 0.
                    if not monkey.is_system:
                        monkey.balance += total_price
                        monkey.save(update_fields=["balance"])
                    holding = (
                        Holding.objects.select_for_update()
                        .filter(monkey=monkey, stock_id=stock_id)
                        .first()
                    )
                    if holding:
                        holding.quantity -= quantity
                        _save_holding(holding)
                order.save(update_fields=update_fields)
            return order
        except OperationalError:
            logger.warning(
                "DB lock applying order %s (attempt %d/3)", order.id, attempt + 1
            )
            time.sleep(0.2 * (attempt + 1))

    logger.error(
        "Order %s confirmed by KIS but local ledger update failed after retries; "
        "holdings reconciliation will reconcile it.",
        order.id,
    )
    order.save(update_fields=update_fields)
    return order


def _save_holding(holding):
    """Persist a holding's quantity, removing the row entirely once it hits zero."""
    if holding.quantity <= 0:
        holding.delete()
    else:
        holding.save(update_fields=["quantity"])


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
            "state": Monkey.State.INACTIVE,
            "balance": 0,
            "initial_balance": 0,
            "order_interval_seconds": 60,
        },
    )
    return monkey


def transfer_holdings_to_system_monkey(monkey, stock_ids=None):
    """Reassign a monkey's holdings to the hidden system monkey (DB-only, no KIS).

    Used when a monkey dies and during daily reconciliation for delisted/orphaned
    stock: ownership of the holdings moves to the system monkey, which then sells
    them off gradually via its own periodic task. Quantities merge into the system
    monkey's existing (monkey, stock) holding (unique per pair).

    Deliberate exception to "never mutate Holding outside submit_monkey_order" —
    this is a bookkeeping move, not a trade, same as _clamp_phantom_holdings.
    """
    if monkey.is_system:
        return []

    system_monkey = get_or_create_system_monkey()
    transferred = []
    with transaction.atomic():
        holdings = Holding.objects.select_for_update().filter(
            monkey=monkey, quantity__gt=0
        )
        if stock_ids is not None:
            holdings = holdings.filter(stock_id__in=stock_ids)
        for holding in list(holdings):
            dest, _ = Holding.objects.select_for_update().get_or_create(
                monkey=system_monkey,
                stock_id=holding.stock_id,
                defaults={"quantity": 0},
            )
            dest.quantity += holding.quantity
            dest.save(update_fields=["quantity"])
            transferred.append(
                {"stock_id": holding.stock_id, "quantity": holding.quantity}
            )
            holding.delete()
    return transferred


def _absorb_excess(ticker, excess_qty):
    """A ticker is held in the real KIS account but not owned by any monkey locally.

    Assign the excess to the hidden system monkey; its periodic task sells it off
    later, so nothing is liquidated here.
    """
    stock = Stock.objects.filter(ticker=ticker).order_by(
        "id"
    ).first() or _placeholder_stock(ticker)
    system_monkey = get_or_create_system_monkey()

    holding, _ = Holding.objects.get_or_create(
        monkey=system_monkey, stock=stock, defaults={"quantity": 0}
    )
    holding.quantity += excess_qty
    holding.save(update_fields=["quantity"])

    return {
        "ticker": ticker,
        "quantity": excess_qty,
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
        _save_holding(holding)
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
            absorbed.append(_absorb_excess(ticker, real_qty - local_qty))
        elif local_qty > real_qty:
            clamped.append(_clamp_phantom_holdings(ticker, local_qty - real_qty))

    return {"absorbed": absorbed, "clamped": clamped}


def run_daily_maintenance():
    """Daily off-market upkeep: cull underperforming monkeys, then reconcile
    real-vs-local holdings and hand off orphaned/delisted/dead-monkey holdings to
    the system monkey for gradual liquidation.

    Skips entirely while the market is open so killing never happens during a
    trading session (which would break the Monkey Index baseline/live-equity
    comparison). DB-only moves plus a single KIS *read* (account balance) — no
    sell orders are placed here. The system monkey's own periodic task does the
    actual selling during market hours.
    """
    if get_global_control().market_open:
        return {"skipped": "market_open"}

    killed = kill_underperforming_monkeys()

    kis_client = KisClient()
    reconciliation = reconcile_holdings(kis_client=kis_client)

    by_monkey = {}
    for holding in Holding.objects.filter(
        quantity__gt=0, stock__is_active=False
    ).select_related("monkey", "stock"):
        if holding.monkey.is_system:
            continue  # already on the system monkey; it will try to sell it off
        by_monkey.setdefault(holding.monkey, []).append(holding.stock_id)

    delisted_transfers = 0
    for monkey, stock_ids in by_monkey.items():
        transferred = transfer_holdings_to_system_monkey(monkey, stock_ids=stock_ids)
        delisted_transfers += len(transferred)

    # Sweep killed monkeys that still hold stock (safety net; kill_monkey already
    # transfers, but reconciliation may have re-attributed leaked positions).
    killed_transfers = 0
    for monkey in Monkey.objects.filter(state=Monkey.State.DEAD, is_system=False):
        transferred = transfer_holdings_to_system_monkey(monkey)
        killed_transfers += len(transferred)

    return {
        "killed": killed,
        "reconciliation": reconciliation,
        "delisted_transfers": delisted_transfers,
        "killed_transfers": killed_transfers,
    }
