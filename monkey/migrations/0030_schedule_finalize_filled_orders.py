from django.db import migrations

TASK_NAME = "monkey.finalize_filled_orders"


def create_schedule(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # Most market orders fill within minutes; poll every 60s so the resulting
    # holdings/cash land on the monkey's ledger promptly. The task itself no-ops
    # while the market is closed.
    interval, _ = IntervalSchedule.objects.get_or_create(every=60, period="seconds")
    PeriodicTask.objects.get_or_create(
        name=TASK_NAME,
        defaults={
            "task": "monkey.tasks.finalize_filled_orders",
            "interval": interval,
            "enabled": True,
        },
    )


def remove_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0029_discard_orphans"),
    ]

    operations = [
        migrations.RunPython(create_schedule, remove_schedule),
    ]
