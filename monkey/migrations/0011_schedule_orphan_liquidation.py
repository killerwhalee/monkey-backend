from django.db import migrations

TASK_NAME = "monkey.liquidate_orphaned_holdings"


def create_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="10",
        day_of_week="1-5",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Seoul",
    )
    PeriodicTask.objects.get_or_create(
        name=TASK_NAME,
        defaults={
            "task": "monkey.tasks.liquidate_orphaned_holdings",
            "crontab": schedule,
            "enabled": True,
        },
    )


def remove_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0010_monkey_is_system"),
    ]

    operations = [
        migrations.RunPython(create_schedule, remove_schedule),
    ]
