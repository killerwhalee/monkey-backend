from django.db import migrations


def merge_tasks(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # The index tick is now recorded inside update_held_stock_prices, so the
    # standalone beat entry is no longer needed.
    PeriodicTask.objects.filter(name="monkey.index_tick").delete()

    # Bring the price-update cadence down from 120 s to 60 s so each cycle
    # produces exactly one tick (previously tick=60 s, price=120 s were separate).
    interval_60, _ = IntervalSchedule.objects.get_or_create(every=60, period="seconds")
    PeriodicTask.objects.filter(name="monkey.update_held_stock_prices").update(
        interval=interval_60,
    )


def unmerge_tasks(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    interval_60, _ = IntervalSchedule.objects.get_or_create(every=60, period="seconds")
    PeriodicTask.objects.get_or_create(
        name="monkey.index_tick",
        defaults={
            "task": "monkey.tasks.record_index_tick",
            "interval": interval_60,
            "enabled": True,
        },
    )

    interval_120, _ = IntervalSchedule.objects.get_or_create(
        every=120, period="seconds"
    )
    PeriodicTask.objects.filter(name="monkey.update_held_stock_prices").update(
        interval=interval_120,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0031_remove_account_account_interval_max_gte_min_and_more"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(merge_tasks, unmerge_tasks),
    ]
