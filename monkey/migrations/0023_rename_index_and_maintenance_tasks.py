from django.db import migrations


def rename_tasks(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # Earning-ratio tick -> Monkey Index tick.
    PeriodicTask.objects.filter(name="monkey.earning_ratio_tick").update(
        name="monkey.index_tick",
        task="monkey.tasks.record_index_tick",
    )

    # Orphan liquidation -> daily maintenance (now also culls underperformers).
    # Retarget to a pre-market slot (08:40 KST) since the in-task guard makes a
    # market-hours run a no-op; runs after the holiday check (08:00) and before
    # auto-create (08:50) so freed cash can fund new monkeys.
    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="40",
        hour="8",
        day_of_week="1-5",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Seoul",
    )
    PeriodicTask.objects.filter(name="monkey.liquidate_orphaned_holdings").update(
        name="monkey.daily_maintenance",
        task="monkey.tasks.daily_maintenance",
        crontab=schedule,
    )


def revert_tasks(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    PeriodicTask.objects.filter(name="monkey.index_tick").update(
        name="monkey.earning_ratio_tick",
        task="monkey.tasks.record_earning_ratio_tick",
    )

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="10",
        day_of_week="1-5",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Seoul",
    )
    PeriodicTask.objects.filter(name="monkey.daily_maintenance").update(
        name="monkey.liquidate_orphaned_holdings",
        task="monkey.tasks.liquidate_orphaned_holdings",
        crontab=schedule,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0022_monkeyindexbaseline_monkeyindextick_and_more"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(rename_tasks, revert_tasks),
    ]
