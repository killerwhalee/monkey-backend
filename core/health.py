"""Common-format health endpoint polled by external monitoring (ops-docs policies/operations.md).

Only the DB is checked: the public read API works without Redis (it is needed only by
Celery-dispatching staff/feedback writes), and Celery/KIS are excluded by policy.
"""

from django.db import connection, transaction
from django.http import JsonResponse
from django.views.decorators.http import require_GET

# Hard-coded on purpose: if a request is routed to another service, its name shows up instead.
SERVICE = "monkey"
DB_TIMEOUT_MS = 3000


def _check_db():
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute(f"SET LOCAL statement_timeout = {DB_TIMEOUT_MS}")
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return "error"
    return "ok"


@require_GET
def health(request):
    checks = {"db": _check_db()}
    ok = all(value == "ok" for value in checks.values())
    response = JsonResponse(
        {"service": SERVICE, "status": "ok" if ok else "error", "checks": checks},
        status=200 if ok else 503,
    )
    response["Cache-Control"] = "no-store"
    return response
