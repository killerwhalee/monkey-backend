from celery import shared_task

from monkey import services
from monkey.kis import KisClient, KisClientError
from monkey.models import Account


@shared_task
def update_token():
    """Refresh the KIS OAuth token for every active account."""
    refreshed = []
    for account in Account.objects.filter(is_active=True).order_by("id"):
        try:
            token = KisClient(account).refresh_access_token()
        except KisClientError:
            continue
        refreshed.append(
            {"account_id": account.id, "expires_at": token.expires_at.isoformat()}
        )
    return {"refreshed": refreshed}


@shared_task
def get_stock_price(ticker):
    return services.get_account_free_client().get_stock_price(ticker)


@shared_task
def run_monkeys():
    return services.run_active_monkeys()


@shared_task
def run_monkey(monkey_id):
    if not services.get_global_control().enabled:
        return {"enabled": False, "monkey_id": monkey_id}
    order = services.run_random_monkey_order(monkey_id)
    if order is None:
        return {"enabled": True, "monkey_id": monkey_id, "order_id": None}
    return {"order_id": order.id, "status": order.status}


@shared_task
def run_system_monkey():
    if not services.get_global_control().market_open:
        return {"market_open": False}
    orders = services.run_system_monkey_order()
    return {"enabled": True, "order_ids": [order.id for order in orders]}


@shared_task
def check_holiday():
    try:
        client = services.get_account_free_client()
    except services.NoAccountAvailableError:
        return {"skipped": "no_account"}
    is_holiday = client.is_holiday()
    note = "휴장일 (자동)" if is_holiday else "영업일 (자동)"
    services.set_holiday_closed(is_holiday, note=note)
    services.sync_monkey_periodic_tasks()
    return {"is_holiday": is_holiday}


@shared_task
def market_open():
    services.set_trading_enabled(True, note="장 시작 (자동)")
    services.sync_monkey_periodic_tasks()
    services.capture_index_baseline()
    return {"enabled": True}


@shared_task
def market_close():
    services.set_trading_enabled(False, note="장 마감 (자동)")
    services.sync_monkey_periodic_tasks()
    return {"enabled": False}


@shared_task
def snapshot_monkeys():
    return services.snapshot_all_monkeys()


@shared_task
def daily_maintenance():
    return services.run_daily_maintenance()


@shared_task
def auto_create_monkeys():
    if services.get_global_control().market_open:
        return {"skipped": "market_open"}
    monkeys = services.auto_create_monkeys()
    return {"created": [monkey.id for monkey in monkeys]}


@shared_task
def record_index_tick():
    return services.record_index_tick()


@shared_task
def update_held_stock_prices():
    return services.update_held_stock_prices()


@shared_task
def update_all_stock_prices():
    return services.update_all_stock_prices()


@shared_task
def reconcile_executions():
    return services.reconcile_order_executions()
