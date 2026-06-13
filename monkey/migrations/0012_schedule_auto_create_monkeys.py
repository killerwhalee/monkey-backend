from django.db import migrations

TASK_NAME = "monkey.auto_create_monkeys"


def create_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="50",
        hour="8",
        day_of_week="1-5",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Seoul",
    )
    PeriodicTask.objects.get_or_create(
        name=TASK_NAME,
        defaults={
            "task": "monkey.tasks.auto_create_monkeys",
            "crontab": schedule,
            "enabled": True,
        },
    )


def remove_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0011_schedule_orphan_liquidation"),
    ]

    operations = [
        migrations.RunPython(create_schedule, remove_schedule),
    ]
