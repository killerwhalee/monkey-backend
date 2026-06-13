from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

from monkey.models import KisAccessToken


class KisClientError(Exception):
    pass


class KisClient:
    BUY_TR_ID = "VTTC0012U"
    SELL_TR_ID = "VTTC0011U"
    PRICE_TR_ID = "FHKST01010100"
    BALANCE_TR_ID = "VTTC8434R"

    def __init__(self):
        self.base_url = settings.KIS_API_BASE_URL.rstrip("/")
        self.environment = settings.KIS_ENVIRONMENT
        self.app_key = settings.KIS_APP_KEY
        self.app_secret = settings.KIS_APP_SECRET
        self.account_number = settings.KIS_CANO
        self.account_product_code = settings.KIS_ACNT_PRDT_CD

    def get_access_token(self):
        token = KisAccessToken.objects.filter(environment=self.environment).first()
        refresh_at = timezone.now() + timedelta(
            seconds=settings.KIS_TOKEN_REFRESH_MARGIN_SECONDS
        )
        if token and token.expires_at > refresh_at:
            return token.token
        return self.refresh_access_token().token

    def refresh_access_token(self):
        response = requests.post(
            f"{self.base_url}/oauth2/tokenP",
            headers={"Content-Type": "application/json; charset=UTF-8"},
            json={
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            },
        )
        self._raise_for_response(response)
        data = response.json()
        token = data.get("access_token")
        if not token:
            raise KisClientError("KIS token response did not include access_token.")

        expires_at = self._parse_token_expiry(data)
        obj, _ = KisAccessToken.objects.update_or_create(
            environment=self.environment,
            defaults={
                "token": token,
                "expires_at": expires_at,
            },
        )
        return obj

    def get_stock_price(self, ticker):
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            self.PRICE_TR_ID,
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
            },
        )
        output = data.get("output") or {}
        raw_price = output.get("stck_prpr") or output.get("STCK_PRPR")
        if raw_price in (None, ""):
            raise KisClientError("KIS price response did not include current price.")
        return int(raw_price)

    def get_account_balance(self):
        data = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            self.BALANCE_TR_ID,
            {
                "CANO": self.account_number,
                "ACNT_PRDT_CD": self.account_product_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        return {
            "cash_balance": int(
                (data.get("output2") or [{}])[0].get("dnca_tot_amt") or 0
            ),
            "holdings": {
                item["pdno"]: int(item.get("hldg_qty") or 0)
                for item in (data.get("output1") or [])
                if int(item.get("hldg_qty") or 0) > 0
            },
        }

    def order_stock(self, order_type, ticker, quantity):
        from market.models import Order

        tr_id = self.BUY_TR_ID
        if order_type == Order.OrderTypeChoices.SELL:
            tr_id = self.SELL_TR_ID

        payload = {
            "CANO": self.account_number,
            "ACNT_PRDT_CD": self.account_product_code,
            "PDNO": ticker,
            "ORD_DVSN": "01",
            "ORD_QTY": str(quantity),
            "ORD_UNPR": "0",
        }
        data = self._post(
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id,
            payload,
        )
        return payload, data

    def _headers(self, tr_id):
        return {
            "content-type": "application/json; charset=UTF-8",
            "authorization": f"Bearer {self.get_access_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def _get(self, path, tr_id, params):
        response = requests.get(
            f"{self.base_url}{path}",
            headers=self._headers(tr_id),
            params=params,
        )
        self._raise_for_response(response)
        return response.json()

    def _post(self, path, tr_id, payload):
        response = requests.post(
            f"{self.base_url}{path}",
            headers=self._headers(tr_id),
            json=payload,
        )
        self._raise_for_response(response)
        return response.json()

    def _raise_for_response(self, response):
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise KisClientError(str(exc)) from exc

    def _parse_token_expiry(self, data):
        raw_expiry = data.get("access_token_token_expired")
        if raw_expiry:
            try:
                parsed = timezone.datetime.strptime(raw_expiry, "%Y-%m-%d %H:%M:%S")
                return timezone.make_aware(parsed, timezone.get_current_timezone())
            except ValueError:
                pass

        expires_in = int(data.get("expires_in") or 86400)
        return timezone.now() + timedelta(seconds=expires_in)
