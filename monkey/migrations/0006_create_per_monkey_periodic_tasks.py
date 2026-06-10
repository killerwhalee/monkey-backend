import json

from django.db import migrations

TASK_PATH = "monkey.tasks.run_monkey"
DEFAULT_INTERVAL_SECONDS = 60


def create_per_monkey_tasks(apps, schema_editor):
    Monkey = apps.get_model("monkey", "Monkey")
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    interval, _ = IntervalSchedule.objects.get_or_create(
        every=DEFAULT_INTERVAL_SECONDS,
        period="seconds",
    )
    for monkey in Monkey.objects.all():
        PeriodicTask.objects.get_or_create(
            name=f"monkey.run.{monkey.id}",
            defaults={
                "task": TASK_PATH,
                "interval": interval,
                "kwargs": json.dumps({"monkey_id": monkey.id}),
                "enabled": monkey.is_active,
            },
        )


def remove_per_monkey_tasks(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(task=TASK_PATH).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0005_add_globalcontrol_fields_and_killed_at"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(create_per_monkey_tasks, remove_per_monkey_tasks),
    ]
