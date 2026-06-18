"""Discard account-less data left over from the single-account era.

IRREVERSIBLE. After the multi-account migration, every pre-existing monkey is
account-less (the old single account lived in env vars, not the DB). Per the
project's deploy decision we do NOT auto-migrate those credentials: instead the
admin re-registers a fresh account via the UI and reconciliation absorbs the real
KIS positions into that account's system monkey.

So here we:
- set every account-less, non-system monkey to DEAD and drop its periodic task,
- delete those monkeys' holdings,
- retire the old global (account-less) system monkey the same way,
- keep all Order rows for history.
"""

from django.db import migrations
from django.utils import timezone


def discard_orphans(apps, schema_editor):
    Monkey = apps.get_model("monkey", "Monkey")
    Holding = apps.get_model("market", "Holding")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    orphans = Monkey.objects.filter(account__isnull=True).exclude(state="dead")
    orphan_ids = list(orphans.values_list("id", flat=True))
    if not orphan_ids:
        return

    Holding.objects.filter(monkey_id__in=orphan_ids).delete()
    PeriodicTask.objects.filter(
        name__in=[f"monkey.run.{pk}" for pk in orphan_ids]
    ).delete()
    orphans.update(state="dead", killed_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [
        ("monkey", "0028_account_and_more"),
        ("django_celery_beat", "0001_initial"),
        ("market", "0007_alter_stock_short_code"),
    ]

    operations = [
        migrations.RunPython(discard_orphans, migrations.RunPython.noop),
    ]
