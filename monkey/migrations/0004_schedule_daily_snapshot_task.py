from django.db import migrations

PERIODIC_TASK_NAME = "Snapshot monkey daily metrics"
TASK_PATH = "monkey.tasks.snapshot_monkeys"


def create_snapshot_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="0",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Seoul",
    )
    PeriodicTask.objects.get_or_create(
        name=PERIODIC_TASK_NAME,
        defaults={
            "task": TASK_PATH,
            "crontab": schedule,
            "enabled": True,
        },
    )


def remove_snapshot_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=PERIODIC_TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0003_monkeydailysnapshot"),
        (
            "django_celery_beat",
            "0015_alter_clockedschedule_id_alter_crontabschedule_id_and_more",
        ),
    ]

    operations = [
        migrations.RunPython(create_snapshot_schedule, remove_snapshot_schedule),
    ]
