from django.db import migrations, models

STATE_CHOICES = [
    ("active", "Active"),
    ("inactive", "Inactive"),
    ("dead", "Dead"),
]


def set_state_from_is_active(apps, schema_editor):
    Monkey = apps.get_model("monkey", "Monkey")
    # Today a monkey only becomes is_active=False via kill_monkey, so map it to DEAD.
    Monkey.objects.filter(is_active=True).update(state="active")
    Monkey.objects.filter(is_active=False).update(state="dead")
    # The hidden system monkey is not "dead", it simply never trades.
    Monkey.objects.filter(is_system=True).update(state="inactive")


def restore_is_active_from_state(apps, schema_editor):
    Monkey = apps.get_model("monkey", "Monkey")
    Monkey.objects.update(is_active=True)
    Monkey.objects.filter(state="dead").update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0017_monkey_created_at_and_monkey_name_pool"),
    ]

    operations = [
        migrations.AddField(
            model_name="monkey",
            name="state",
            field=models.CharField(
                choices=STATE_CHOICES,
                default="active",
                help_text="ACTIVE = trading, INACTIVE = paused, DEAD = killed (never revives).",
                max_length=16,
                verbose_name="State",
            ),
        ),
        migrations.RunPython(set_state_from_is_active, restore_is_active_from_state),
        migrations.RemoveField(
            model_name="monkey",
            name="is_active",
        ),
    ]
