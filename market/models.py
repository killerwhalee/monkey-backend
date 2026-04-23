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
    )


class Order(models.Model):
    """Stock orders"""

    class OrderTypeChoices(models.IntegerChoices):
        BUY = 0, "Buy"
        SELL = 1, "Sell"

    stock = models.ForeignKey(
        "market.Stock",
        verbose_name="stock",
        on_delete=models.CASCADE,
    )
    order_type = models.IntegerField(
        "Order type",
        choices=OrderTypeChoices,
    )
    price = models.IntegerField(
        "Execution price",
    )
    quantity = models.IntegerField(
        "Executed quantity",
    )
