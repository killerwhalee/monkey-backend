from django.contrib import admin

from . import models


@admin.register(models.Monkey)
class MonkeyAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "is_active",
        "balance",
        "initial_balance",
        "min_quantity",
        "max_quantity",
        "killed_at",
    ]
    list_filter = ["is_active"]
    search_fields = ["name"]
    readonly_fields = ["killed_at"]


@admin.register(models.GlobalMonkeyControl)
class GlobalMonkeyControlAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "enabled",
        "kill_threshold",
        "order_interval_seconds",
        "note",
        "updated_at",
    ]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(models.KisAccessToken)
class KisAccessTokenAdmin(admin.ModelAdmin):
    list_display = ["environment", "expires_at", "updated_at"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(models.MonkeyDailySnapshot)
class MonkeyDailySnapshotAdmin(admin.ModelAdmin):
    list_display = ["monkey", "date", "total_equity", "earning_ratio"]
    list_filter = ["date"]
    search_fields = ["monkey__name"]
    readonly_fields = ["created_at"]
