from django.db import models


class Monkey(models.Model):
    name = models.CharField(
        "Name",
        max_length=32,
    )
    is_active = models.BooleanField(
        "Is active?",
    )
    balance = models.IntegerField(
        "Cash balance",
    )
