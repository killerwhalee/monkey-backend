from django.db import migrations

# Beat-scheduled KIS maintenance tasks fire every 60s but share the per-account
# rate limiter that market-hours monkey orders saturate, so they stall and pile
# up. Expiring a queued instance after ~55s means a stale one is discarded by the
# worker instead of flooding through once the order queue frees at market close.
EXPIRE_SECONDS = 55
TASK_NAMES = [
    "monkey.update_held_stock_prices",
    "monkey.finalize_filled_orders",
]


def set_expiry(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name__in=TASK_NAMES).update(
        expire_seconds=EXPIRE_SECONDS,
    )


def clear_expiry(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name__in=TASK_NAMES).update(expire_seconds=None)


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0032_merge_price_and_tick_tasks"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(set_expiry, clear_expiry),
    ]
