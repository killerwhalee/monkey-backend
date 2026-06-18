from django.db import migrations


def succeeded_to_executed(apps, schema_editor):
    """Legacy SUCCEEDED orders were applied under the old "accepted == filled"
    scheme — their ledger is already reflected, so they ARE the executed orders.
    Remap them to EXECUTED so the new metrics filter (status=EXECUTED) keeps the
    same FIFO/holdings math."""
    Order = apps.get_model("market", "Order")
    Order.objects.filter(status="succeeded").update(status="executed")


def executed_to_succeeded(apps, schema_editor):
    Order = apps.get_model("market", "Order")
    Order.objects.filter(status="executed").update(status="succeeded")


class Migration(migrations.Migration):
    dependencies = [
        ("market", "0008_alter_order_status"),
    ]

    operations = [
        migrations.RunPython(succeeded_to_executed, executed_to_succeeded),
    ]
