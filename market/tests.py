from unittest import mock

from django.test import TestCase

from market import tasks
from market.models import Stock


def _fake_download(base_dir, market):
    data = {
        "kospi": [
            {"market": "KOSPI", "ticker": "005930", "name": "Samsung Electronics"},
        ],
        "kosdaq": [
            {"market": "KOSDAQ", "ticker": "123456", "name": "Some Kosdaq Co"},
        ],
    }
    return data[market]


class UpdateMarketTests(TestCase):
    def test_update_market_marks_delisted_stocks_inactive(self):
        delisted = Stock.objects.create(
            market="KOSPI", ticker="999999", name="Old Co", is_active=True
        )

        with mock.patch(
            "market.tasks.download_and_parse_market", side_effect=_fake_download
        ):
            result = tasks.update_market()["output"]

        delisted.refresh_from_db()
        self.assertFalse(delisted.is_active)
        self.assertEqual(result["stocks"], 2)
        self.assertEqual(result["deactivated"], 1)

        self.assertTrue(Stock.objects.get(ticker="005930", market="KOSPI").is_active)
        self.assertTrue(Stock.objects.get(ticker="123456", market="KOSDAQ").is_active)

    def test_update_market_reactivates_relisted_stocks(self):
        Stock.objects.create(
            market="KOSPI",
            ticker="005930",
            name="Samsung Electronics",
            is_active=False,
        )

        with mock.patch(
            "market.tasks.download_and_parse_market", side_effect=_fake_download
        ):
            tasks.update_market()

        self.assertTrue(Stock.objects.get(ticker="005930", market="KOSPI").is_active)
