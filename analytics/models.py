from django.db import models


class DailyVisit(models.Model):
    """Per-day unique-visitor tally that backs the public dashboard counter.

    One row per calendar day (Asia/Seoul). ``count`` is the number of distinct
    visitors seen that day; the all-time total is the sum across rows.
    """

    date = models.DateField("Date", unique=True)
    count = models.PositiveIntegerField("Count", default=0)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.date}: {self.count}"


class VisitorDay(models.Model):
    """Dedup ledger: one row per (visitor, day) so a visitor is counted once a day.

    The visitor is identified by a salted hash of their IP address — raw
    addresses are never stored.
    """

    date = models.DateField("Date")
    visitor_hash = models.CharField("Visitor hash", max_length=64)
    created_at = models.DateTimeField("Created at", auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["date", "visitor_hash"], name="unique_visitor_per_day"
            )
        ]

    def __str__(self):
        return f"{self.date}: {self.visitor_hash[:12]}"
