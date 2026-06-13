from celery import shared_task

from monkey import services
from monkey.kis import KisClient


@shared_task
def update_token():
    token = KisClient().refresh_access_token()
    return {
        "environment": token.environment,
        "expires_at": token.expires_at.isoformat(),
    }


@shared_task
def get_stock_price(ticker):
    return KisClient().get_stock_price(ticker)


@shared_task
def run_monkeys():
    return services.run_active_monkeys()


@shared_task
def run_monkey(monkey_id):
    if not services.get_global_control().enabled:
        return {"enabled": False, "monkey_id": monkey_id}
    order = services.run_random_monkey_order(monkey_id)
    return {"order_id": order.id, "status": order.status}


@shared_task
def check_holiday():
    is_holiday = KisClient().is_holiday()
    note = "휴장일 (자동)" if is_holiday else "영업일 (자동)"
    services.set_holiday_closed(is_holiday, note=note)
    return {"is_holiday": is_holiday}


@shared_task
def market_open():
    services.set_trading_enabled(True, note="장 시작 (자동)")
    return {"enabled": True}


@shared_task
def market_close():
    services.set_trading_enabled(False, note="장 마감 (자동)")
    return {"enabled": False}


@shared_task
def snapshot_monkeys():
    return services.snapshot_all_monkeys()


@shared_task
def liquidate_orphaned_holdings():
    return services.liquidate_orphaned_holdings()


@shared_task
def auto_create_monkeys():
    monkeys = services.auto_create_monkeys()
    return {"created": [monkey.id for monkey in monkeys]}


@shared_task
def record_earning_ratio_tick():
    return services.record_earning_ratio_tick()


@shared_task
def update_held_stock_prices():
    return services.update_held_stock_prices()


@shared_task
def reconcile_executions():
    return services.reconcile_order_executions()
