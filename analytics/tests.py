from django.test import TestCase
from django.urls import reverse

from analytics.models import DailyVisit, VisitorDay


class VisitCounterTests(TestCase):
    def _post(self, ip="1.2.3.4"):
        return self.client.post(reverse("visits"), HTTP_X_REAL_IP=ip)

    def test_first_visit_counts_one(self):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"today": 1, "total": 1})
        self.assertEqual(DailyVisit.objects.count(), 1)
        self.assertEqual(VisitorDay.objects.count(), 1)

    def test_same_ip_same_day_not_double_counted(self):
        self._post(ip="1.2.3.4")
        response = self._post(ip="1.2.3.4")
        self.assertEqual(response.json(), {"today": 1, "total": 1})
        self.assertEqual(VisitorDay.objects.count(), 1)

    def test_distinct_ips_each_count(self):
        self._post(ip="1.1.1.1")
        response = self._post(ip="2.2.2.2")
        self.assertEqual(response.json(), {"today": 2, "total": 2})

    def test_get_reads_without_recording(self):
        self._post(ip="1.1.1.1")
        response = self.client.get(reverse("visits"))
        self.assertEqual(response.json(), {"today": 1, "total": 1})
        self.assertEqual(VisitorDay.objects.count(), 1)

    def test_ip_is_not_stored_in_cleartext(self):
        self._post(ip="9.9.9.9")
        stored = VisitorDay.objects.get().visitor_hash
        self.assertNotIn("9.9.9.9", stored)
        self.assertEqual(len(stored), 64)
