from django.db import migrations

OPEN_TASK_NAME = "market.auto.open"
CLOSE_TASK_NAME = "market.auto.close"


def create_market_schedules(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    open_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="9",
        day_of_week="1-5",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Seoul",
    )
    close_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="30",
        hour="15",
        day_of_week="1-5",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Seoul",
    )
    PeriodicTask.objects.get_or_create(
        name=OPEN_TASK_NAME,
        defaults={
            "task": "monkey.tasks.market_open",
            "crontab": open_schedule,
            "enabled": True,
        },
    )
    PeriodicTask.objects.get_or_create(
        name=CLOSE_TASK_NAME,
        defaults={
            "task": "monkey.tasks.market_close",
            "crontab": close_schedule,
            "enabled": True,
        },
    )


def remove_market_schedules(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name__in=[OPEN_TASK_NAME, CLOSE_TASK_NAME]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0006_create_per_monkey_periodic_tasks"),
        ("django_celery_beat", "0020_merge_20260609_2109"),
    ]

    operations = [
        migrations.RunPython(create_market_schedules, remove_market_schedules),
    ]
