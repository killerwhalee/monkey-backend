from django.core.validators import MinValueValidator
from django.db import models


def short_code_for(ticker):
    """The 6+ digit code KIS reports holdings (pdno) by.

    KIS strips the leading prefix character from tickers longer than 6 chars
    (e.g. ETN "Q610039" -> "610039", warrant "J0669721F" -> "0669721F"); 6-char
    tickers are returned unchanged.
    """
    ticker = ticker or ""
    return ticker[1:] if len(ticker) > 6 else ticker


class Stock(models.Model):
    """Stock info"""

    market = models.CharField(
        "Stock market",
        max_length=32,
    )
    ticker = models.CharField(
        "Stock ticker",
        max_length=16,
    )
    short_code = models.CharField(
        "Short code",
        max_length=16,
        blank=True,
        db_index=True,
        help_text=(
            "Ticker with its leading prefix stripped when longer than 6 chars. "
            "KIS balance inquiries return holdings (pdno) by this code, so "
            "reconciliation joins on it."
        ),
    )
    name = models.CharField(
        "Stock name",
        max_length=256,
    )
    is_active = models.BooleanField(
        "Is active?",
        default=True,
        help_text="Whether this stock is currently listed on its market.",
    )
    current_price = models.PositiveIntegerField(
        "Current price",
        null=True,
        blank=True,
        help_text="Latest live price from KIS; refreshed for held stocks during trading hours.",
    )
    price_updated_at = models.DateTimeField(
        "Price updated at",
        null=True,
        blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["ticker", "market"],
                name="unique_ticker_per_market",
            )
        ]

    def save(self, *args, **kwargs):
        # Keep short_code in sync with ticker for every ORM save (bulk_create,
        # which bypasses this, sets it explicitly in market.tasks.update_market).
        self.short_code = short_code_for(self.ticker)
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"[{self.__class__.__name__} #{self.pk:04d}] "
            f"{self.market} {self.ticker} {self.name}"
        )


class Holding(models.Model):
    monkey = models.ForeignKey(
        "monkey.Monkey",
        verbose_name="monkey",
        on_delete=models.CASCADE,
    )
    stock = models.ForeignKey(
        "market.Stock",
        verbose_name="stock",
        on_delete=models.CASCADE,
    )
    quantity = models.IntegerField(
        "Quantity held",
        validators=[MinValueValidator(0)],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["monkey", "stock"],
                name="unique_holding_per_monkey_stock",
            )
        ]

    def __str__(self):
        return (
            f"[{self.__class__.__name__} #{self.pk:04d}] "
            f"{self.monkey_id} {self.stock_id} x {self.quantity}"
        )


class Order(models.Model):
    """Single audit ledger for attempted and completed stock orders."""

    class OrderTypeChoices(models.IntegerChoices):
        BUY = 0, "Buy"
        SELL = 1, "Sell"

    class StatusChoices(models.TextChoices):
        CREATED = "created", "Created"
        SKIPPED = "skipped", "Skipped"
        # SUBMITTED = accepted by KIS, awaiting fill (reserves the monkey's
        # funds/shares); EXECUTED = real fills applied to the local ledger.
        SUBMITTED = "submitted", "Submitted"
        EXECUTED = "executed", "Executed"
        # Legacy: orders applied under the old "accepted == filled" scheme. Kept
        # for back-compat; migrated to EXECUTED by data migration. No longer set.
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    monkey = models.ForeignKey(
        "monkey.Monkey",
        verbose_name="monkey",
        on_delete=models.CASCADE,
        related_name="orders",
        null=True,
        blank=True,
    )
    stock = models.ForeignKey(
        "market.Stock",
        verbose_name="stock",
        on_delete=models.CASCADE,
        related_name="orders",
    )
    order_type = models.IntegerField(
        "Order type",
        choices=OrderTypeChoices,
    )
    status = models.CharField(
        "Status",
        max_length=32,
        choices=StatusChoices,
        default=StatusChoices.CREATED,
    )
    requested_quantity = models.PositiveIntegerField(
        "Requested quantity",
        default=1,
        validators=[MinValueValidator(1)],
    )
    executed_quantity = models.PositiveIntegerField(
        "Executed quantity",
        default=0,
    )
    estimated_price = models.PositiveIntegerField(
        "Estimated price",
        null=True,
        blank=True,
    )
    executed_price = models.PositiveIntegerField(
        "Execution price",
        null=True,
        blank=True,
    )
    failure_reason = models.CharField(
        "Failure reason",
        max_length=512,
        blank=True,
    )
    kis_order_id = models.CharField(
        "KIS order ID",
        max_length=128,
        blank=True,
    )
    kis_order_status = models.CharField(
        "KIS order status",
        max_length=128,
        blank=True,
    )
    kis_request = models.JSONField(
        "KIS request",
        default=dict,
        blank=True,
    )
    kis_response = models.JSONField(
        "KIS response",
        default=dict,
        blank=True,
    )
    execution_detail = models.JSONField(
        "Execution detail",
        default=dict,
        blank=True,
        help_text="Raw KIS daily-ccld output1 fill record captured at confirmation.",
    )
    last_finalize_check = models.DateTimeField(
        "Last finalize check",
        null=True,
        blank=True,
        help_text=(
            "When the mid-session finalize pass last queried this SUBMITTED order's "
            "fill. Used to round-robin so one perpetually-partial order can't starve "
            "the rest of the queue."
        ),
    )
    created_at = models.DateTimeField(
        "Created at",
        auto_now_add=True,
        null=True,
    )
    updated_at = models.DateTimeField(
        "Updated at",
        auto_now=True,
    )

    @property
    def price(self):
        return self.executed_price or self.estimated_price

    @property
    def quantity(self):
        return self.executed_quantity or self.requested_quantity

    def __str__(self):
        return (
            f"[{self.__class__.__name__} #{self.pk:04d}] "
            f"{self.monkey_id} {self.get_order_type_display()} "
            f"{self.stock_id} x {self.requested_quantity} {self.status}"
        )
