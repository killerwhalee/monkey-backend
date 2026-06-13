from django.db import migrations

PRICE_TASK_NAME = "monkey.update_held_stock_prices"
RECONCILE_TASK_NAME = "monkey.reconcile_executions"


def create_schedules(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    price_interval, _ = IntervalSchedule.objects.get_or_create(
        every=120,
        period="seconds",
    )
    reconcile_interval, _ = IntervalSchedule.objects.get_or_create(
        every=180,
        period="seconds",
    )
    PeriodicTask.objects.get_or_create(
        name=PRICE_TASK_NAME,
        defaults={
            "task": "monkey.tasks.update_held_stock_prices",
            "interval": price_interval,
            "enabled": True,
        },
    )
    PeriodicTask.objects.get_or_create(
        name=RECONCILE_TASK_NAME,
        defaults={
            "task": "monkey.tasks.reconcile_executions",
            "interval": reconcile_interval,
            "enabled": True,
        },
    )


def remove_schedules(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(
        name__in=[PRICE_TASK_NAME, RECONCILE_TASK_NAME]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0015_schedule_holiday_check"),
    ]

    operations = [
        migrations.RunPython(create_schedules, remove_schedules),
    ]
