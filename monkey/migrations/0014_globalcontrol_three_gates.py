from django.db import migrations, models


def split_enabled_into_gates(apps, schema_editor):
    """Carry the old single ``enabled`` flag over into ``manual_enabled``.

    The time/holiday gates are managed by scheduled tasks, so the only state
    worth preserving is whatever the admin had set manually.
    """
    GlobalMonkeyControl = apps.get_model("monkey", "GlobalMonkeyControl")
    for control in GlobalMonkeyControl.objects.all():
        control.manual_enabled = control.enabled
        control.save(update_fields=["manual_enabled"])


def restore_enabled(apps, schema_editor):
    GlobalMonkeyControl = apps.get_model("monkey", "GlobalMonkeyControl")
    for control in GlobalMonkeyControl.objects.all():
        control.enabled = (
            control.time_enabled and control.holiday_enabled and control.manual_enabled
        )
        control.save(update_fields=["enabled"])


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0013_monkey_earning_ratio_tick"),
    ]

    operations = [
        migrations.AddField(
            model_name="globalmonkeycontrol",
            name="time_enabled",
            field=models.BooleanField(
                default=False,
                help_text="Open between market open (09:00) and close (15:30); set by scheduled tasks.",
                verbose_name="Time gate open?",
            ),
        ),
        migrations.AddField(
            model_name="globalmonkeycontrol",
            name="holiday_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Closed on KRX holidays; set daily by the holiday-check task.",
                verbose_name="Holiday gate open?",
            ),
        ),
        migrations.AddField(
            model_name="globalmonkeycontrol",
            name="manual_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Admin kill-switch. Trading runs only when this and the other gates are all open.",
                verbose_name="Manual gate open?",
            ),
        ),
        migrations.RunPython(split_enabled_into_gates, restore_enabled),
        migrations.RemoveField(
            model_name="globalmonkeycontrol",
            name="enabled",
        ),
    ]
