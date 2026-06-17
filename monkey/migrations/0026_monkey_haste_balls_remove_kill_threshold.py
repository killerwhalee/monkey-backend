import random

import django.core.validators
from django.db import migrations, models

TRAIT_FLOOR = 0.05


def backfill_traits(apps, schema_editor):
    """Seed traits for monkeys that predate them: invert the existing cadence into
    haste (haste=1 → fastest/min, haste=0 → slowest/max) and pick a random balls."""
    Monkey = apps.get_model("monkey", "Monkey")
    Control = apps.get_model("monkey", "GlobalMonkeyControl")
    control = Control.objects.filter(pk=1).first()
    low = control.auto_create_min_interval_seconds if control else 60
    high = control.auto_create_max_interval_seconds if control else 1800
    span = (high - low) or 1
    for monkey in Monkey.objects.all():
        haste = 1 - (monkey.order_interval_seconds - low) / span
        monkey.haste = max(TRAIT_FLOOR, min(1.0, haste))
        monkey.balls = random.uniform(TRAIT_FLOOR, 1.0)
        monkey.save(update_fields=["haste", "balls"])


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0025_kisaccountcache"),
    ]

    operations = [
        migrations.AddField(
            model_name="monkey",
            name="haste",
            field=models.FloatField(
                default=0.5,
                help_text="Trading frequency trait (0..1). Higher = more frequent (shorter interval).",
                validators=[
                    django.core.validators.MinValueValidator(0.0),
                    django.core.validators.MaxValueValidator(1.0),
                ],
                verbose_name="성급함 (haste)",
            ),
        ),
        migrations.AddField(
            model_name="monkey",
            name="balls",
            field=models.FloatField(
                default=0.5,
                help_text="Boldness trait (0..1). Higher = larger orders (bigger fraction of affordable/held).",
                validators=[
                    django.core.validators.MinValueValidator(0.0),
                    django.core.validators.MaxValueValidator(1.0),
                ],
                verbose_name="배짱 (balls)",
            ),
        ),
        migrations.AlterField(
            model_name="monkey",
            name="order_interval_seconds",
            field=models.PositiveIntegerField(
                default=60,
                help_text="How often this monkey places a random order. Derived from `haste` at birth.",
                validators=[
                    django.core.validators.MinValueValidator(60),
                    django.core.validators.MaxValueValidator(7200),
                ],
                verbose_name="Order interval (seconds)",
            ),
        ),
        migrations.RemoveField(
            model_name="globalmonkeycontrol",
            name="kill_threshold",
        ),
        migrations.AlterField(
            model_name="globalmonkeycontrol",
            name="auto_create_min_interval_seconds",
            field=models.PositiveIntegerField(
                default=60,
                help_text="Fastest possible cadence (haste=1). The haste trait interpolates the order interval across this min..max range.",
                validators=[
                    django.core.validators.MinValueValidator(60),
                    django.core.validators.MaxValueValidator(7200),
                ],
                verbose_name="Min order interval (seconds)",
            ),
        ),
        migrations.AlterField(
            model_name="globalmonkeycontrol",
            name="auto_create_max_interval_seconds",
            field=models.PositiveIntegerField(
                default=1800,
                help_text="Slowest possible cadence (haste=0). The haste trait interpolates the order interval across this min..max range.",
                validators=[
                    django.core.validators.MinValueValidator(60),
                    django.core.validators.MaxValueValidator(7200),
                ],
                verbose_name="Max order interval (seconds)",
            ),
        ),
        migrations.RunPython(backfill_traits, migrations.RunPython.noop),
    ]
