from django.contrib import admin

from . import models


@admin.register(models.Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ["id", "email", "category", "subject", "status", "created_at"]
    list_filter = ["category", "status"]
    search_fields = ["email", "subject", "message"]
    readonly_fields = [
        "email",
        "category",
        "subject",
        "message",
        "created_at",
        "updated_at",
    ]
