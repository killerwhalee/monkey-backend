import json

from django.core.validators import MinValueValidator
from django.db import OperationalError, ProgrammingError, models
from django.db.models import F, Q


class GlobalMonkeyControl(models.Model):
    """Global switch for autonomous monkey trading."""

    enabled = models.BooleanField(
        "Is monkey trading enabled?",
        default=False,
    )
    note = models.CharField(
        "Note",
        max_length=256,
        blank=True,
    )
    kill_threshold = models.FloatField(
        "Kill threshold (earning ratio)",
        default=-0.5,
        help_text="Monkeys whose earning_ratio drops below this value are automatically deactivated.",
    )
    order_interval_seconds = models.PositiveIntegerField(
        "Order interval (seconds)",
        default=60,
        help_text="How often each monkey places a random order.",
    )
    created_at = models.DateTimeField(
        "Created at",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        "Updated at",
        auto_now=True,
    )

    def save(self, *args, **kwargs):
        # Resync all per-monkey PeriodicTask intervals when order_interval_seconds changes.
        update_fields = kwargs.get("update_fields")
        interval_changed = False
        if update_fields is None or "order_interval_seconds" in update_fields:
            try:
                old = GlobalMonkeyControl.objects.values_list(
                    "order_interval_seconds", flat=True
                ).get(pk=self.pk)
                interval_changed = old != self.order_interval_seconds
            except GlobalMonkeyControl.DoesNotExist:
                pass
        super().save(*args, **kwargs)
        if interval_changed:
            try:
                from django_celery_beat.models import IntervalSchedule, PeriodicTask

                interval, _ = IntervalSchedule.objects.get_or_create(
                    every=self.order_interval_seconds,
                    period=IntervalSchedule.SECONDS,
                )
                PeriodicTask.objects.filter(task="monkey.tasks.run_monkey").update(
                    interval=interval
                )
            except (OperationalError, ProgrammingError):
                pass

    def __str__(self):
        return f"[{self.__class__.__name__} #{self.pk:04d}] enabled={self.enabled}"


class KisAccessToken(models.Model):
    """Shared KIS API token for Celery workers."""

    environment = models.CharField(
        "Environment",
        max_length=32,
        unique=True,
    )
    token = models.TextField(
        "Access token",
    )
    expires_at = models.DateTimeField(
        "Expires at",
    )
    created_at = models.DateTimeField(
        "Created at",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        "Updated at",
        auto_now=True,
    )

    def __str__(self):
        return (
            f"[{self.__class__.__name__} #{self.pk:04d}] "
            f"{self.environment} expires_at={self.expires_at}"
        )


class Monkey(models.Model):
    name = models.CharField(
        "Name",
        max_length=32,
    )
    is_active = models.BooleanField(
        "Is active?",
        default=True,
    )
    balance = models.IntegerField(
        "Cash balance",
        validators=[MinValueValidator(0)],
    )
    initial_balance = models.IntegerField(
        "Initial cash balance",
        default=0,
        validators=[MinValueValidator(0)],
    )
    min_quantity = models.PositiveIntegerField(
        "Minimum order quantity",
        default=1,
        validators=[MinValueValidator(1)],
    )
    max_quantity = models.PositiveIntegerField(
        "Maximum order quantity",
        default=1,
        validators=[MinValueValidator(1)],
    )
    killed_at = models.DateTimeField(
        "Killed at",
        null=True,
        blank=True,
    )

    def _periodic_task_name(self):
        return f"monkey.run.{self.pk}"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        try:
            from django_celery_beat.models import IntervalSchedule, PeriodicTask

            if is_new:
                # Deferred import: services.py imports from models.py at module level.
                from monkey.services import get_global_control

                control = get_global_control()
                interval, _ = IntervalSchedule.objects.get_or_create(
                    every=control.order_interval_seconds,
                    period=IntervalSchedule.SECONDS,
                )
                PeriodicTask.objects.get_or_create(
                    name=self._periodic_task_name(),
                    defaults={
                        "task": "monkey.tasks.run_monkey",
                        "interval": interval,
                        "kwargs": json.dumps({"monkey_id": self.pk}),
                        "enabled": self.is_active,
                    },
                )
            else:
                PeriodicTask.objects.filter(name=self._periodic_task_name()).update(
                    enabled=self.is_active,
                )
        except (OperationalError, ProgrammingError):
            pass  # tables not yet created during initial migrate or test DB setup

    def delete(self, *args, **kwargs):
        try:
            from django_celery_beat.models import PeriodicTask

            PeriodicTask.objects.filter(name=self._periodic_task_name()).delete()
        except (OperationalError, ProgrammingError):
            pass
        super().delete(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.max_quantity < self.min_quantity:
            from django.core.exceptions import ValidationError

            raise ValidationError(
                {
                    "max_quantity": "Maximum quantity must be greater than or equal to minimum quantity."
                }
            )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(min_quantity__gte=1),
                name="monkey_min_quantity_gte_1",
            ),
            models.CheckConstraint(
                condition=Q(max_quantity__gte=F("min_quantity")),
                name="monkey_max_quantity_gte_min_quantity",
            ),
        ]

    def __str__(self):
        return f"[{self.__class__.__name__} #{self.pk:04d}] {self.name}"


class MonkeyDailySnapshot(models.Model):
    """Daily point-in-time copy of a monkey's performance metrics."""

    monkey = models.ForeignKey(
        "monkey.Monkey",
        verbose_name="monkey",
        on_delete=models.CASCADE,
        related_name="daily_snapshots",
    )
    date = models.DateField(
        "Snapshot date",
    )
    cash_balance = models.IntegerField(
        "Cash balance",
    )
    holdings_value = models.IntegerField(
        "Holdings value",
    )
    total_equity = models.IntegerField(
        "Total equity",
    )
    total_pl = models.IntegerField(
        "Total P&L",
    )
    realized_pl = models.IntegerField(
        "Realized P&L",
    )
    unrealized_pl = models.IntegerField(
        "Unrealized P&L",
    )
    earning_ratio = models.FloatField(
        "Earning ratio",
    )
    created_at = models.DateTimeField(
        "Created at",
        auto_now_add=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["monkey", "date"],
                name="unique_snapshot_per_monkey_date",
            )
        ]
        ordering = ["date", "monkey_id"]

    def __str__(self):
        return (
            f"[{self.__class__.__name__} #{self.pk:04d}] "
            f"{self.monkey_id} {self.date} ratio={self.earning_ratio}"
        )
