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
    order = services.run_random_monkey_order(monkey_id)
    return {
        "order_id": order.id,
        "status": order.status,
    }


@shared_task
def snapshot_monkeys():
    return services.snapshot_all_monkeys()
