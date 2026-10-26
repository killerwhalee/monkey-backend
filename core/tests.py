from unittest import mock

from django.db import DatabaseError
from django.test import TestCase


class HealthTests(TestCase):
    def test_ok(self):
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"service": "monkey", "status": "ok", "checks": {"db": "ok"}},
        )
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_db_failure_returns_503_without_details(self):
        with mock.patch(
            "django.db.backends.utils.CursorWrapper.execute",
            side_effect=DatabaseError("boom"),
        ):
            response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"service": "monkey", "status": "error", "checks": {"db": "error"}},
        )
        self.assertNotIn(b"boom", response.content)

    def test_post_not_allowed(self):
        self.assertEqual(self.client.post("/api/health/").status_code, 405)
