from django.db import migrations

RECONCILE_TASK_NAME = "monkey.reconcile_executions"


def reschedule_reconcile_and_set_expiry(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # Reconcile reads the *daily* execution inquiry, which KIS recommends calling
    # after the market closes. Move it from a 3-minute interval to once a day at
    # 15:40 KST (just after the 15:30 close), so it no longer hammers the API or
    # crowds the queue during trading hours.
    close_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="40",
        hour="15",
        day_of_week="1-5",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Seoul",
    )
    PeriodicTask.objects.filter(name=RECONCILE_TASK_NAME).update(
        crontab=close_schedule,
        interval=None,
    )

    # Backfill skip-the-tick expiry on pre-existing per-monkey order tasks.
    for task in PeriodicTask.objects.filter(task="monkey.tasks.run_monkey"):
        every = task.interval.every if task.interval else 120
        task.expire_seconds = min(every, 120)
        task.save(update_fields=["expire_seconds"])


def noop(apps, schema_editor):
    # Reverting the schedule precisely isn't important; leave as-is.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0018_monkey_state"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(reschedule_reconcile_and_set_expiry, noop),
    ]
