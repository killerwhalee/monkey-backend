from django.db import migrations

SYSTEM_TASK_NAME = "monkey.run_system"


def create_schedule(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    interval, _ = IntervalSchedule.objects.get_or_create(
        every=60,
        period="seconds",
    )
    PeriodicTask.objects.get_or_create(
        name=SYSTEM_TASK_NAME,
        defaults={
            "task": "monkey.tasks.run_system_monkey",
            "interval": interval,
            # Gate-controlled: sync_monkey_periodic_tasks() enables it only while
            # the market is open, like the other market-hours tasks.
            "enabled": False,
        },
    )


def remove_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=SYSTEM_TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        (
            "monkey",
            "0020_globalmonkeycontrol_auto_create_max_interval_seconds_and_more",
        ),
    ]

    operations = [
        migrations.RunPython(create_schedule, remove_schedule),
    ]
