from django.core.validators import MinValueValidator
from django.db import models
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
    created_at = models.DateTimeField(
        "Created at",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        "Updated at",
        auto_now=True,
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
