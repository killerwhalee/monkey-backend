import json

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import OperationalError, ProgrammingError, models


class GlobalMonkeyControl(models.Model):
    """Global switch for autonomous monkey trading.

    Trading is allowed only when all three independent gates are open. The
    effective ``enabled`` is their logical AND:

    - ``time_enabled``  — managed by the ``market_open`` / ``market_close`` beat
      tasks (09:00 on, 15:30 off).
    - ``holiday_enabled`` — managed by the daily ``check_holiday`` task; turned
      off on KRX holidays.
    - ``manual_enabled`` — the admin kill-switch (the only admin-toggleable gate).
    """

    time_enabled = models.BooleanField(
        "Time gate open?",
        default=False,
        help_text="Open between market open (09:00) and close (15:30); set by scheduled tasks.",
    )
    holiday_enabled = models.BooleanField(
        "Holiday gate open?",
        default=True,
        help_text="Closed on KRX holidays; set daily by the holiday-check task.",
    )
    manual_enabled = models.BooleanField(
        "Manual gate open?",
        default=True,
        help_text="Admin kill-switch. Trading runs only when this and the other gates are all open.",
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
    auto_create_starting_balance = models.PositiveIntegerField(
        "Auto-create starting balance",
        default=1_000_000,
        validators=[MinValueValidator(1)],
        help_text="Cash each monkey is created with (auto-create divides unallocated cash by this).",
    )
    auto_create_min_interval_seconds = models.PositiveIntegerField(
        "New monkey min order interval (seconds)",
        default=60,
        validators=[MinValueValidator(60), MaxValueValidator(7200)],
        help_text="Lower bound of the random order interval assigned to newly created monkeys.",
    )
    auto_create_max_interval_seconds = models.PositiveIntegerField(
        "New monkey max order interval (seconds)",
        default=1800,
        validators=[MinValueValidator(60), MaxValueValidator(7200)],
        help_text="Upper bound of the random order interval assigned to newly created monkeys.",
    )
    created_at = models.DateTimeField(
        "Created at",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        "Updated at",
        auto_now=True,
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    auto_create_max_interval_seconds__gte=models.F(
                        "auto_create_min_interval_seconds"
                    )
                ),
                name="globalcontrol_interval_max_gte_min",
            ),
        ]

    @property
    def market_open(self) -> bool:
        """Physical market state: exchange is open regardless of manual gate."""
        return self.time_enabled and self.holiday_enabled

    @property
    def enabled(self) -> bool:
        """Effective monkey trading switch: every gate must be open."""
        return self.market_open and self.manual_enabled

    def clean(self):
        super().clean()
        if (
            self.auto_create_max_interval_seconds
            < self.auto_create_min_interval_seconds
        ):
            raise ValidationError(
                {
                    "auto_create_max_interval_seconds": "최대 거래 주기는 최소 거래 주기 이상이어야 합니다."
                }
            )

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
    class State(models.TextChoices):
        ACTIVE = "active", "Active"  # alive and trading
        INACTIVE = "inactive", "Inactive"  # alive but paused (schedule disabled)
        DEAD = "dead", "Dead"  # killed permanently; never revives

    name = models.CharField(
        "Name",
        max_length=32,
    )
    state = models.CharField(
        "State",
        max_length=16,
        choices=State.choices,
        default=State.ACTIVE,
        help_text="ACTIVE = trading, INACTIVE = paused, DEAD = killed (never revives).",
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
    order_interval_seconds = models.PositiveIntegerField(
        "Order interval (seconds)",
        default=60,
        validators=[MinValueValidator(60), MaxValueValidator(7200)],
        help_text="How often this monkey places a random order.",
    )
    is_system = models.BooleanField(
        "Is system monkey?",
        default=False,
        help_text="Hidden monkey used to absorb/liquidate orphaned real-account positions.",
    )
    killed_at = models.DateTimeField(
        "Killed at",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(
        "Created at",
        auto_now_add=True,
    )

    @property
    def is_active(self) -> bool:
        """Convenience read alias kept for API/dashboard compatibility."""
        return self.state == self.State.ACTIVE

    def _periodic_task_name(self):
        return f"monkey.run.{self.pk}"

    @staticmethod
    def _gate_open() -> bool:
        """Whether the global trading gate is currently open (time ∧ holiday ∧ manual)."""
        control = GlobalMonkeyControl.objects.filter(pk=1).first()
        return bool(control and control.enabled)

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        try:
            from django_celery_beat.models import (
                IntervalSchedule,
                PeriodicTask,
                PeriodicTasks,
            )

            # A dead monkey never revives — drop its schedule entirely.
            if self.state == self.State.DEAD:
                PeriodicTask.objects.filter(name=self._periodic_task_name()).delete()
                PeriodicTasks.update_changed()
                return

            # ACTIVE monkeys run only while the market gate is open; INACTIVE
            # (paused) monkeys keep their task but disabled, ready to resume.
            enabled = self.state == self.State.ACTIVE and self._gate_open()
            if is_new:
                interval, _ = IntervalSchedule.objects.get_or_create(
                    every=self.order_interval_seconds,
                    period=IntervalSchedule.SECONDS,
                )
                PeriodicTask.objects.get_or_create(
                    name=self._periodic_task_name(),
                    defaults={
                        "task": "monkey.tasks.run_monkey",
                        "interval": interval,
                        "kwargs": json.dumps({"monkey_id": self.pk}),
                        "enabled": enabled,
                        # Skip-the-tick: drop a queued order that waited too long
                        # rather than execute it stale and back up the queue.
                        "expire_seconds": min(self.order_interval_seconds, 120),
                    },
                )
            else:
                PeriodicTask.objects.filter(name=self._periodic_task_name()).update(
                    enabled=enabled,
                )
            PeriodicTasks.update_changed()
        except (OperationalError, ProgrammingError):
            pass  # tables not yet created during initial migrate or test DB setup

    def delete(self, *args, **kwargs):
        try:
            from django_celery_beat.models import PeriodicTask

            PeriodicTask.objects.filter(name=self._periodic_task_name()).delete()
        except (OperationalError, ProgrammingError):
            pass
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"[{self.__class__.__name__} #{self.pk:04d}] {self.name}"


class MonkeyName(models.Model):
    """A name in the pool used to name newly created monkeys."""

    name = models.CharField(
        "Name",
        max_length=32,
        unique=True,
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


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


class MonkeyIndexBaseline(models.Model):
    """Per-trading-day reference point for the chained Monkey Index.

    ``base_index`` is yesterday's closing index value (``i``); ``base_equity`` is
    the summed equity of alive monkeys captured right before market open (``a``).
    Each minute's index value is ``base_index * (current_equity / base_equity)``.
    """

    date = models.DateField(
        "Trading date",
        unique=True,
    )
    base_index = models.FloatField(
        "Base index (yesterday's close)",
    )
    base_equity = models.BigIntegerField(
        "Base equity (alive monkeys at open)",
    )
    created_at = models.DateTimeField(
        "Created at",
        auto_now_add=True,
    )

    class Meta:
        ordering = ["date"]

    def __str__(self):
        return (
            f"[{self.__class__.__name__} #{self.pk:04d}] "
            f"{self.date} base_index={self.base_index} base_equity={self.base_equity}"
        )


class MonkeyIndexTick(models.Model):
    """Per-minute sample of the Monkey Index value (base 10,000).

    Used to build the candlestick chart (open/high/low/close per bucket).
    """

    recorded_at = models.DateTimeField(
        "Recorded at",
        auto_now_add=True,
        db_index=True,
    )
    value = models.FloatField(
        "Index value",
    )

    class Meta:
        ordering = ["recorded_at"]

    def __str__(self):
        return (
            f"[{self.__class__.__name__} #{self.pk:04d}] "
            f"{self.recorded_at} value={self.value}"
        )
