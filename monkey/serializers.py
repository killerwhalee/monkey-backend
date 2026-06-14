from rest_framework import serializers

from market.models import Holding, Order
from market.serializers import HoldingSerializer, OrderSerializer
from monkey import services
from monkey.models import GlobalMonkeyControl, KisAccessToken, Monkey


class MonkeySerializer(serializers.ModelSerializer):
    holdings = serializers.SerializerMethodField()
    recent_orders = serializers.SerializerMethodField()
    metrics = serializers.SerializerMethodField()

    class Meta:
        model = Monkey
        fields = [
            "id",
            "name",
            "is_active",
            "balance",
            "initial_balance",
            "order_interval_seconds",
            "killed_at",
            "holdings",
            "recent_orders",
            "metrics",
        ]
        read_only_fields = ["killed_at"]

    def get_holdings(self, obj):
        holdings = Holding.objects.filter(monkey=obj, quantity__gt=0).select_related(
            "stock"
        )
        breakdown = build_holdings_breakdown(obj)
        data = HoldingSerializer(holdings, many=True).data
        for row in data:
            extra = breakdown.get(row["stock"]["id"], {})
            row["average_price"] = extra.get("average_price", 0)
            row["current_price"] = extra.get("current_price", 0)
            row["evaluation"] = extra.get("evaluation", 0)
            row["profit"] = extra.get("profit", 0)
            row["profit_rate"] = extra.get("profit_rate", 0.0)
        return data

    def get_recent_orders(self, obj):
        orders = (
            Order.objects.filter(monkey=obj)
            .select_related("stock")
            .order_by("-created_at")[:10]
        )
        return OrderSerializer(orders, many=True).data

    def get_metrics(self, obj):
        return build_monkey_metrics(obj)


class MonkeyBulkCreateSerializer(serializers.Serializer):
    count = serializers.IntegerField(min_value=1, max_value=1000)
    starting_balance = serializers.IntegerField(min_value=0)

    def create(self, validated_data):
        return services.create_monkeys(**validated_data)


class GlobalMonkeyControlSerializer(serializers.ModelSerializer):
    enabled = serializers.BooleanField(read_only=True)

    class Meta:
        model = GlobalMonkeyControl
        fields = [
            "id",
            "enabled",
            "time_enabled",
            "holiday_enabled",
            "manual_enabled",
            "kill_threshold",
            "note",
            "created_at",
            "updated_at",
        ]
        # The time/holiday gates are owned by scheduled tasks; only the manual
        # gate (and threshold/note) may be changed through the API.
        read_only_fields = ["time_enabled", "holiday_enabled"]


class KisAccessTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = KisAccessToken
        fields = [
            "id",
            "environment",
            "expires_at",
            "created_at",
            "updated_at",
        ]


class DailyEarningRatioPointSerializer(serializers.Serializer):
    date = serializers.DateField()
    average_earning_ratio = serializers.FloatField()
    best_earning_ratio = serializers.FloatField()


class AccountSummarySerializer(serializers.Serializer):
    kis_cash_balance = serializers.IntegerField()
    unallocated_cash = serializers.IntegerField()
    monkey_count = serializers.IntegerField()
    active_monkey_count = serializers.IntegerField()
    total_monkey_balance = serializers.IntegerField()
    total_holdings_value = serializers.IntegerField()
    total_equity = serializers.IntegerField()
    average_earning_ratio = serializers.FloatField()
    best_earning_ratio = serializers.FloatField()
    system_balance = serializers.IntegerField()
    system_holdings_value = serializers.IntegerField()


class CandlestickSerializer(serializers.Serializer):
    time = serializers.IntegerField()
    open = serializers.FloatField()
    high = serializers.FloatField()
    low = serializers.FloatField()
    close = serializers.FloatField()


class DashboardSummarySerializer(serializers.Serializer):
    active_monkey_count = serializers.IntegerField()
    average_earning_ratio = serializers.FloatField()
    best_earning_ratio = serializers.FloatField()
    total_initial_balance = serializers.IntegerField()
    total_cash_balance = serializers.IntegerField()
    total_holdings_value = serializers.IntegerField()
    total_equity = serializers.IntegerField()
    total_pl = serializers.IntegerField()
    earning_ratio = serializers.FloatField()
    average_order_interval_seconds = serializers.IntegerField()
    latest_orders = OrderSerializer(many=True)
    daily_earning_ratio_series = DailyEarningRatioPointSerializer(many=True)


def build_monkey_metrics(monkey):
    holdings_value = 0
    unrealized_pl = 0
    realized_pl = 0

    for holding in Holding.objects.filter(monkey=monkey).select_related("stock"):
        price = _current_price(monkey, holding.stock)
        holdings_value += holding.quantity * price
        basis, realized_for_stock = _stock_profit(monkey, holding.stock_id, price)
        unrealized_pl += holding.quantity * price - basis
        realized_pl += realized_for_stock

    total_equity = monkey.balance + holdings_value
    total_pl = total_equity - monkey.initial_balance
    earning_ratio = (
        (total_pl / monkey.initial_balance) if monkey.initial_balance else 0.0
    )
    return {
        "cash_balance": monkey.balance,
        "holdings_value": holdings_value,
        "total_equity": total_equity,
        "total_pl": total_pl,
        "realized_pl": realized_pl,
        "unrealized_pl": unrealized_pl,
        "earning_ratio": earning_ratio,
    }


def build_holdings_breakdown(monkey):
    """Per-held-stock average price, live price, evaluation and earning rate.

    Keyed by ``stock_id``. Average price comes from FIFO-walking the monkey's
    succeeded orders (handles repeated buys/sells); current price is the live
    Stock price (falling back to the last trade price when not yet polled).
    """
    breakdown = {}
    for holding in Holding.objects.filter(monkey=monkey, quantity__gt=0).select_related(
        "stock"
    ):
        current_price = _current_price(monkey, holding.stock)
        cost_basis, _ = _stock_profit(monkey, holding.stock_id, current_price)
        average_price = round(cost_basis / holding.quantity) if holding.quantity else 0
        evaluation = holding.quantity * current_price
        profit = evaluation - cost_basis
        profit_rate = (profit / cost_basis) if cost_basis else 0.0
        breakdown[holding.stock_id] = {
            "average_price": average_price,
            "current_price": current_price,
            "evaluation": evaluation,
            "profit": profit,
            "profit_rate": profit_rate,
        }
    return breakdown


def _current_price(monkey, stock):
    """Live price for a stock, falling back to the monkey's last trade price."""
    if stock.current_price:
        return stock.current_price
    return _latest_stock_price(monkey, stock.id)


def _latest_stock_price(monkey, stock_id):
    order = (
        Order.objects.filter(
            monkey=monkey,
            stock_id=stock_id,
            status=Order.StatusChoices.SUCCEEDED,
        )
        .exclude(estimated_price__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if not order:
        return 0
    return order.executed_price or order.estimated_price or 0


def _stock_profit(monkey, stock_id, current_price):
    quantity = 0
    cost_basis = 0
    realized_pl = 0
    orders = Order.objects.filter(
        monkey=monkey,
        stock_id=stock_id,
        status=Order.StatusChoices.SUCCEEDED,
    ).order_by("created_at", "id")

    for order in orders:
        price = order.executed_price or order.estimated_price or current_price
        if order.order_type == Order.OrderTypeChoices.BUY:
            quantity += order.executed_quantity
            cost_basis += order.executed_quantity * price
        else:
            if quantity:
                average_cost = cost_basis / quantity
                sold_basis = average_cost * order.executed_quantity
                cost_basis -= sold_basis
                quantity -= order.executed_quantity
                realized_pl += order.executed_quantity * price - sold_basis

    return round(cost_basis), round(realized_pl)
