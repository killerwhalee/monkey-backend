import hashlib

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.utils import timezone

from analytics.models import DailyVisit, VisitorDay


def client_ip(request):
    """Best-effort client IP behind nginx.

    nginx sets ``X-Real-IP`` to the real connecting address (not spoofable by
    the client's own headers), so prefer it; fall back to the first
    ``X-Forwarded-For`` hop and finally to ``REMOTE_ADDR``.
    """
    real_ip = request.META.get("HTTP_X_REAL_IP")
    if real_ip:
        return real_ip.strip()
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _visitor_hash(ip):
    return hashlib.sha256(f"{settings.SECRET_KEY}:{ip}".encode()).hexdigest()


def current_stats():
    """Return ``{"today": int, "total": int}`` without recording a visit."""
    today = timezone.localdate()
    today_count = (
        DailyVisit.objects.filter(date=today).values_list("count", flat=True).first()
        or 0
    )
    total = DailyVisit.objects.aggregate(total=Sum("count"))["total"] or 0
    return {"today": today_count, "total": total}


def record_visit(request):
    """Record one visit (deduped per visitor per day) and return the stats.

    A repeat visit from the same IP on the same day does not increment either
    counter, so React Query refetches or page refreshes are safe.
    """
    today = timezone.localdate()
    visitor_hash = _visitor_hash(client_ip(request))

    try:
        with transaction.atomic():
            VisitorDay.objects.create(date=today, visitor_hash=visitor_hash)
        is_new = True
    except IntegrityError:
        is_new = False

    if is_new:
        daily, _ = DailyVisit.objects.get_or_create(date=today)
        DailyVisit.objects.filter(pk=daily.pk).update(count=F("count") + 1)

    return current_stats()
