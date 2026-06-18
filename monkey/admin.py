from django.contrib import admin

from . import models


@admin.register(models.Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = [
        "display_id",
        "account_type",
        "is_active",
        "updated_at",
    ]
    list_filter = ["account_type", "is_active"]
    # Never surface the encrypted secrets in the admin.
    exclude = ["app_key", "app_secret"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(models.Monkey)
class MonkeyAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "state",
        "balance",
        "initial_balance",
        "haste",
        "balls",
        "order_interval_seconds",
        "killed_at",
        "created_at",
    ]
    list_filter = ["state", "is_system"]
    search_fields = ["name"]
    readonly_fields = ["killed_at", "created_at"]


@admin.register(models.MonkeyName)
class MonkeyNameAdmin(admin.ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]


@admin.register(models.GlobalMonkeyControl)
class GlobalMonkeyControlAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "enabled",
        "note",
        "updated_at",
    ]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(models.KisAccessToken)
class KisAccessTokenAdmin(admin.ModelAdmin):
    list_display = ["account", "expires_at", "updated_at"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(models.MonkeyDailySnapshot)
class MonkeyDailySnapshotAdmin(admin.ModelAdmin):
    list_display = ["monkey", "date", "total_equity", "earning_ratio"]
    list_filter = ["date"]
    search_fields = ["monkey__name"]
    readonly_fields = ["created_at"]


@admin.register(models.MonkeyIndexTick)
class MonkeyIndexTickAdmin(admin.ModelAdmin):
    list_display = ["id", "recorded_at", "value"]
    readonly_fields = ["recorded_at"]
    date_hierarchy = "recorded_at"


@admin.register(models.MonkeyIndexBaseline)
class MonkeyIndexBaselineAdmin(admin.ModelAdmin):
    list_display = ["id", "date", "base_index", "base_equity"]
    readonly_fields = ["created_at"]
    date_hierarchy = "date"
