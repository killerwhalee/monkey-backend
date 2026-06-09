from django.core.validators import MinValueValidator
from django.db import models


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
    name = models.CharField(
        "Stock name",
        max_length=256,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["ticker", "market"],
                name="unique_ticker_per_market",
            )
        ]

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
        SUBMITTED = "submitted", "Submitted"
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
