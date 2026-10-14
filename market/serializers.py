from rest_framework import serializers

from market import models


class StockSerializer(serializers.ModelSerializer):
    class Meta:
        model = models.Stock
        fields = [
            "id",
            "market",
            "ticker",
            "name",
            "is_active",
        ]


class HoldingSerializer(serializers.ModelSerializer):
    stock = StockSerializer(read_only=True)
    # Read the FK column directly (`monkey_id`) rather than `monkey.id`, which
    # would load the related Monkey per row and defeat list-endpoint prefetching.
    monkey_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = models.Holding
        fields = [
            "id",
            "monkey_id",
            "stock",
            "quantity",
        ]


# Keys in the raw KIS payloads that identify the trading account or the
# operator (account number/product code on the order request; inquiry IP,
# contact number and employee number on the daily execution record). The order
# ledger is public, so these are masked for everyone but staff.
REDACTED_KIS_KEYS = frozenset(
    {
        "CANO",
        "ACNT_PRDT_CD",
        "inqr_ip_addr",
        "ctac_tlno",
        "ordr_empno",
    }
)
REDACTED_VALUE = "***"
KIS_PAYLOAD_FIELDS = ("kis_request", "kis_response", "execution_detail")


def redact_kis_payload(value):
    """Return ``value`` with every ``REDACTED_KIS_KEYS`` entry masked, recursing
    into nested dicts/lists (KIS nests records under ``output``/``output1``)."""
    if isinstance(value, dict):
        return {
            key: REDACTED_VALUE
            if key in REDACTED_KIS_KEYS
            else redact_kis_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_kis_payload(item) for item in value]
    return value


class OrderSerializer(serializers.ModelSerializer):
    stock = StockSerializer(read_only=True)
    monkey_id = serializers.IntegerField(read_only=True)
    monkey_name = serializers.CharField(source="monkey.name", read_only=True)
    order_type_label = serializers.CharField(
        source="get_order_type_display",
        read_only=True,
    )
    price = serializers.IntegerField(read_only=True)
    quantity = serializers.IntegerField(read_only=True)

    class Meta:
        model = models.Order
        fields = [
            "id",
            "monkey_id",
            "monkey_name",
            "stock",
            "order_type",
            "order_type_label",
            "status",
            "requested_quantity",
            "executed_quantity",
            "estimated_price",
            "executed_price",
            "price",
            "quantity",
            "failure_reason",
            "kis_order_id",
            "kis_order_status",
            "kis_request",
            "kis_response",
            "execution_detail",
            "created_at",
            "updated_at",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Unredacted payloads only for an authenticated staff request; anything
        # without a request in context (e.g. the public WebSocket push) is masked.
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if not (user and user.is_staff):
            for field in KIS_PAYLOAD_FIELDS:
                data[field] = redact_kis_payload(data[field])
        return data
