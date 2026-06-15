import logging
import time
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

from monkey.models import KisAccessToken

logger = logging.getLogger(__name__)


class KisClientError(Exception):
    pass


# Reserve the next slot and return how long (seconds) the caller must wait. Run
# as a Lua script so the read-modify-write is atomic across workers/processes.
_RATE_LIMIT_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local nxt = tonumber(redis.call('get', key) or '0')
local wait
if now >= nxt then
  redis.call('set', key, now + interval, 'PX', 10000)
  wait = 0
else
  redis.call('set', key, nxt + interval, 'PX', 10000)
  wait = nxt - now
end
return tostring(wait)
"""

_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is None:
        import redis

        _redis_client = redis.Redis.from_url(settings.CELERY_BROKER_URL)
    return _redis_client


def kis_throttle(environment):
    """Block until at least ``KIS_MIN_REQUEST_INTERVAL`` has passed since the last
    KIS request (shared across all workers/processes via Redis).

    KIS paper trading caps requests at ~1/sec per account, so this is the single
    chokepoint that keeps the whole fleet within budget. If Redis is unreachable
    we proceed best-effort rather than block trading.
    """
    interval = getattr(settings, "KIS_MIN_REQUEST_INTERVAL", 0)
    if not interval:
        return
    try:
        client = _get_redis()
        key = f"kis:ratelimit:{environment}"
        wait = float(
            client.eval(_RATE_LIMIT_LUA, 1, key, str(time.time()), str(interval))
        )
    except Exception:  # noqa: BLE001 — never let a limiter glitch halt trading
        return
    if wait > 0:
        time.sleep(wait)


class KisClient:
    BUY_TR_ID = "VTTC0012U"
    SELL_TR_ID = "VTTC0011U"
    PRICE_TR_ID = "FHKST01010100"
    BALANCE_TR_ID = "VTTC8434R"
    HOLIDAY_TR_ID = "CTCA0903R"
    DAILY_CCLD_TR_ID = "VTTC0081R"

    # The holiday endpoint is not served on the virtual/paper domain
    # (모의투자 미지원), so it is always queried against production.
    HOLIDAY_BASE_URL = "https://openapi.koreainvestment.com:9443"

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
        response = self._execute(
            "POST",
            f"{self.base_url}/oauth2/tokenP",
            headers={"Content-Type": "application/json; charset=UTF-8"},
            json={
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            },
        )
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
        # An empty paper account ("모의투자 잔고내역이 없습니다") is a normal,
        # benign response — surface it as zero cash / no holdings, never an error.
        output2 = data.get("output2") or [{}]
        return {
            "cash_balance": int((output2[0] or {}).get("dnca_tot_amt") or 0),
            "holdings": {
                item["pdno"]: int(item.get("hldg_qty") or 0)
                for item in (data.get("output1") or [])
                if int(item.get("hldg_qty") or 0) > 0
            },
        }

    def is_holiday(self, date=None):
        """Return True if the KRX market is closed on ``date`` (default: today).

        Uses the domestic holiday endpoint (production-only). On any error we
        assume the market is *open* (return False) so a transient failure never
        silently halts trading — a wrong "open" just produces harmless rejected
        orders, whereas a wrong "holiday" would freeze a real trading day.
        """
        target = date or timezone.localdate()
        bass_dt = target.strftime("%Y%m%d")
        try:
            data = self._get(
                "/uapi/domestic-stock/v1/quotations/chk-holiday",
                self.HOLIDAY_TR_ID,
                {"BASS_DT": bass_dt, "CTX_AREA_NK": "", "CTX_AREA_FK": ""},
                base_url=self.HOLIDAY_BASE_URL,
            )
        except (KisClientError, ValueError):
            return False
        for entry in data.get("output") or []:
            if str(entry.get("bass_dt")) == bass_dt:
                return str(entry.get("opnd_yn")).upper() != "Y"
        return False

    def get_daily_order_executions(self, start_date=None, end_date=None):
        """Return executed quantity/avg price per KIS order number (ODNO).

        Walks the 주식일별주문체결조회 endpoint (paginating with the continuation
        keys) and returns ``{odno: {"executed_quantity": int, "avg_price": int}}``
        for orders with at least one fill. ODNO keys are stripped of leading
        zeros so they match however the order was originally recorded.
        """
        today = timezone.localdate()
        start = (start_date or today).strftime("%Y%m%d")
        end = (end_date or today).strftime("%Y%m%d")

        executions = {}
        ctx_fk = ""
        ctx_nk = ""
        tr_cont = ""
        for _ in range(50):  # hard cap so a malformed continuation can't loop forever
            response = self._execute(
                "GET",
                f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                headers={**self._headers(self.DAILY_CCLD_TR_ID), "tr_cont": tr_cont},
                params={
                    "CANO": self.account_number,
                    "ACNT_PRDT_CD": self.account_product_code,
                    "INQR_STRT_DT": start,
                    "INQR_END_DT": end,
                    "SLL_BUY_DVSN_CD": "00",
                    "INQR_DVSN": "00",
                    "PDNO": "",
                    "CCLD_DVSN": "00",
                    "ORD_GNO_BRNO": "",
                    "ODNO": "",
                    "INQR_DVSN_3": "00",
                    "INQR_DVSN_1": "",
                    "EXCG_ID_DVSN_CD": "KRX",
                    "CTX_AREA_FK100": ctx_fk,
                    "CTX_AREA_NK100": ctx_nk,
                },
            )
            data = response.json()
            for item in data.get("output1") or []:
                odno = str(item.get("odno") or "").lstrip("0")
                executed = int(item.get("tot_ccld_qty") or 0)
                if not odno or executed <= 0:
                    continue
                executions[odno] = {
                    "executed_quantity": executed,
                    "avg_price": round(float(item.get("avg_prvs") or 0)),
                }

            tr_cont = response.headers.get("tr_cont", "")
            if tr_cont not in ("F", "M"):
                break
            ctx_fk = data.get("ctx_area_fk100", "")
            ctx_nk = data.get("ctx_area_nk100", "")

        return executions

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

    def _get(self, path, tr_id, params, base_url=None):
        response = self._execute(
            "GET",
            f"{base_url or self.base_url}{path}",
            headers=self._headers(tr_id),
            params=params,
        )
        return response.json()

    def _post(self, path, tr_id, payload):
        response = self._execute(
            "POST",
            f"{self.base_url}{path}",
            headers=self._headers(tr_id),
            json=payload,
        )
        return response.json()

    def _execute(self, method, url, **kwargs):
        """Single chokepoint for KIS HTTP: global rate limit + timeout + retry.

        Retries transient failures (network errors, HTTP 5xx, and KIS rate-limit
        responses) up to ``KIS_MAX_RETRIES`` times — KIS advises an immediate
        re-call with a short term — then raises ``KisClientError``. Non-transient
        4xx errors raise immediately.
        """
        kwargs.setdefault("timeout", settings.KIS_REQUEST_TIMEOUT)
        send = requests.post if method == "POST" else requests.get
        attempts = max(1, getattr(settings, "KIS_MAX_RETRIES", 0) + 1)
        last_error = None
        for attempt in range(attempts):
            kis_throttle(self.environment)
            try:
                response = send(url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = KisClientError(f"KIS request network error: {exc}")
                logger.warning(
                    "KIS %s %s network error (attempt %d/%d): %s",
                    method,
                    url,
                    attempt + 1,
                    attempts,
                    exc,
                )
            else:
                status = getattr(response, "status_code", 200)
                if status < 500 and not self._is_rate_limited(response):
                    self._raise_for_response(response)
                    return response
                reason = f"HTTP {status}" if status >= 500 else "rate limited"
                last_error = KisClientError(f"KIS request {reason}: {url}")
                logger.warning(
                    "KIS %s %s %s (attempt %d/%d)",
                    method,
                    url,
                    reason,
                    attempt + 1,
                    attempts,
                )
            if attempt + 1 < attempts:
                time.sleep(0.15)  # KIS guidance: brief term, then re-call
        raise last_error or KisClientError(f"KIS request failed: {url}")

    @staticmethod
    def _is_rate_limited(response):
        try:
            data = response.json()
        except (ValueError, AttributeError):
            return False
        if not isinstance(data, dict):
            return False
        blob = " ".join(
            str(data.get(key, ""))
            for key in ("msg1", "msg_cd", "error_code", "error_description")
        )
        return "초당" in blob or "EGW00201" in blob or "EGW00133" in blob

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
