from django.db import migrations

# With all KIS tasks on a single queue the kis_orders worker can be busy for
# longer stretches, so the previous 120-second (or interval-length) expiry
# revokes order tasks that are still valid. Raise the ceiling to 5 minutes.


def set_expiry(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(task="monkey.tasks.run_monkey").update(
        expire_seconds=300
    )


def restore_expiry(apps, schema_editor):
    # Best-effort rollback: re-apply the old min(interval, 120) per task.
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    for task in PeriodicTask.objects.filter(
        task="monkey.tasks.run_monkey"
    ).select_related("interval"):
        every = task.interval.every if task.interval else 120
        task.expire_seconds = min(every, 120)
        task.save(update_fields=["expire_seconds"])


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0034_merge_kis_queues"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(set_expiry, restore_expiry),
    ]
