from django.contrib import admin

from . import models


@admin.register(models.Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = ["market", "ticker", "name"]
    list_filter = ["market"]
    search_fields = ["ticker", "name"]


@admin.register(models.Holding)
class HoldingAdmin(admin.ModelAdmin):
    list_display = ["monkey", "stock", "quantity"]
    list_filter = ["stock__market"]
    search_fields = ["monkey__name", "stock__ticker", "stock__name"]


@admin.register(models.Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "monkey",
        "stock",
        "order_type",
        "status",
        "requested_quantity",
        "executed_quantity",
        "estimated_price",
        "executed_price",
        "created_at",
    ]
    list_filter = ["order_type", "status", "stock__market", "created_at"]
    search_fields = [
        "monkey__name",
        "stock__ticker",
        "stock__name",
        "kis_order_id",
        "failure_reason",
    ]
    readonly_fields = ["created_at", "updated_at"]
