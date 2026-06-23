from django.db import migrations

# All KIS-touching tasks now share the kis_orders queue (single worker). The
# 55-second expiry set in 0033 was a workaround for finalize_filled_orders
# starving behind update_held_stock_prices on the separate kis-aux worker; with
# one queue single_instance() is sufficient to prevent pile-up, so the expiry
# can be cleared.
TASK_NAMES = [
    "monkey.update_held_stock_prices",
    "monkey.finalize_filled_orders",
]


def clear_expiry(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name__in=TASK_NAMES).update(expire_seconds=None)


def restore_expiry(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name__in=TASK_NAMES).update(expire_seconds=55)


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0033_expire_maintenance_tasks"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(clear_expiry, restore_expiry),
    ]
