from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import path

from market import tasks as market_tasks
from monkey import tasks as monkey_tasks

from . import models

# Tasks an admin can fire manually from the GlobalMonkeyControl page.
TASK_BUTTONS = [
    ("run_monkeys", monkey_tasks.run_monkeys),
    ("update_market", market_tasks.update_market),
    ("update_token", monkey_tasks.update_token),
    ("snapshot_monkeys", monkey_tasks.snapshot_monkeys),
    ("record_earning_ratio_tick", monkey_tasks.record_earning_ratio_tick),
    ("update_held_stock_prices", monkey_tasks.update_held_stock_prices),
    ("reconcile_executions", monkey_tasks.reconcile_executions),
    ("check_holiday", monkey_tasks.check_holiday),
    ("auto_create_monkeys", monkey_tasks.auto_create_monkeys),
    ("liquidate_orphaned_holdings", monkey_tasks.liquidate_orphaned_holdings),
    ("market_open", monkey_tasks.market_open),
    ("market_close", monkey_tasks.market_close),
]
TASK_MAP = dict(TASK_BUTTONS)


@admin.register(models.Monkey)
class MonkeyAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "state",
        "balance",
        "initial_balance",
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
        "kill_threshold",
        "note",
        "updated_at",
    ]
    readonly_fields = ["created_at", "updated_at"]
    change_list_template = "admin/monkey/globalmonkeycontrol/change_list.html"

    def get_urls(self):
        custom = [
            path(
                "run-task/<str:task_name>/",
                self.admin_site.admin_view(self.run_task_view),
                name="monkey_run_task",
            ),
        ]
        return custom + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["task_buttons"] = [name for name, _ in TASK_BUTTONS]
        return super().changelist_view(request, extra_context=extra_context)

    def run_task_view(self, request, task_name):
        changelist = "admin:monkey_globalmonkeycontrol_changelist"
        if request.method != "POST":
            return redirect(changelist)
        task = TASK_MAP.get(task_name)
        if task is None:
            self.message_user(
                request, f"알 수 없는 작업: {task_name}", level=messages.ERROR
            )
            return redirect(changelist)
        try:
            result = task.delay()
        except Exception as exc:  # broker unreachable, etc.
            self.message_user(
                request, f"작업 '{task_name}' 실행 실패: {exc}", level=messages.ERROR
            )
        else:
            self.message_user(
                request,
                f"작업 '{task_name}' 실행 요청됨 (id={result.id}).",
                level=messages.SUCCESS,
            )
        return redirect(changelist)


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


@admin.register(models.MonkeyEarningRatioTick)
class MonkeyEarningRatioTickAdmin(admin.ModelAdmin):
    list_display = ["id", "recorded_at", "average_earning_ratio"]
    readonly_fields = ["recorded_at"]
    date_hierarchy = "recorded_at"
