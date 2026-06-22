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
