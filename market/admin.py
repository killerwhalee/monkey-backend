from django.contrib import admin

from . import models


@admin.register(models.Stock)
class StockAdmin(admin.ModelAdmin):
    pass


@admin.register(models.Holding)
class HoldingAdmin(admin.ModelAdmin):
    pass


@admin.register(models.Order)
class OrderAdmin(admin.ModelAdmin):
    pass
