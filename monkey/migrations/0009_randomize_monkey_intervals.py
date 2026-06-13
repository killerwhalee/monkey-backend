import random

from django.db import migrations


def randomize_monkey_intervals(apps, schema_editor):
    Monkey = apps.get_model("monkey", "Monkey")
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    for monkey in Monkey.objects.all():
        monkey.order_interval_seconds = random.randint(60, 1800)
        monkey.save(update_fields=["order_interval_seconds"])

        interval, _ = IntervalSchedule.objects.get_or_create(
            every=monkey.order_interval_seconds,
            period="seconds",
        )
        PeriodicTask.objects.filter(name=f"monkey.run.{monkey.id}").update(
            interval=interval
        )


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0008_monkey_traits"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(randomize_monkey_intervals, migrations.RunPython.noop),
    ]
