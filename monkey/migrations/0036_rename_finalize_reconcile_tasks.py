from django.db import migrations


def rename_tasks(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="monkey.finalize_filled_orders").update(
        name="monkey.finalize_order",
        task="monkey.tasks.finalize_order",
    )
    PeriodicTask.objects.filter(name="monkey.reconcile_executions").update(
        name="monkey.finalize_orders",
        task="monkey.tasks.finalize_orders",
    )


def reverse_rename_tasks(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="monkey.finalize_order").update(
        name="monkey.finalize_filled_orders",
        task="monkey.tasks.finalize_filled_orders",
    )
    PeriodicTask.objects.filter(name="monkey.finalize_orders").update(
        name="monkey.reconcile_executions",
        task="monkey.tasks.reconcile_executions",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0035_order_task_expire_300s"),
    ]

    operations = [
        migrations.RunPython(rename_tasks, reverse_code=reverse_rename_tasks),
    ]
