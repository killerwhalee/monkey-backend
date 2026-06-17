from django.db import migrations


def wipe_index_history(apps, schema_editor):
    """Clear recorded index data so the Monkey Index cold-starts at the new
    base (1,000.00). The previous history was recorded on the 10,000 scale and
    each day chains off the prior close, so it cannot be left in place."""
    apps.get_model("monkey", "MonkeyIndexTick").objects.all().delete()
    apps.get_model("monkey", "MonkeyIndexBaseline").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0026_monkey_haste_balls_remove_kill_threshold"),
    ]

    operations = [
        migrations.RunPython(wipe_index_history, migrations.RunPython.noop),
    ]
