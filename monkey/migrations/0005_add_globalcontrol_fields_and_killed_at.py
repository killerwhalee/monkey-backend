from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0004_schedule_daily_snapshot_task"),
    ]

    operations = [
        migrations.AddField(
            model_name="globalmonkeycontrol",
            name="kill_threshold",
            field=models.FloatField(
                default=-0.5,
                help_text="Monkeys whose earning_ratio drops below this value are automatically deactivated.",
                verbose_name="Kill threshold (earning ratio)",
            ),
        ),
        migrations.AddField(
            model_name="globalmonkeycontrol",
            name="order_interval_seconds",
            field=models.PositiveIntegerField(
                default=60,
                help_text="How often each monkey places a random order.",
                verbose_name="Order interval (seconds)",
            ),
        ),
        migrations.AddField(
            model_name="monkey",
            name="killed_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="Killed at",
            ),
        ),
    ]
