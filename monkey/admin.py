from django.contrib import admin

from . import models


@admin.register(models.Monkey)
class MonkeyAdmin(admin.ModelAdmin):
    pass
