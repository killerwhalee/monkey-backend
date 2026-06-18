from rest_framework import serializers

from market.models import Holding, Order
from market.serializers import HoldingSerializer, OrderSerializer
from monkey import services
from monkey.models import GlobalMonkeyControl, KisAccessToken, Monkey


class MonkeySerializer(serializers.ModelSerializer):
    holdings = serializers.SerializerMethodField()
    recent_orders = serializers.SerializerMethodField()
    metrics = serializers.SerializerMethodField()
    # `is_active` is now a model property (state == ACTIVE); kept for API compatibility.
    is_active = serializers.ReadOnlyField()

    class Meta:
        model = Monkey
        fields = [
            "id",
            "name",
            "state",
            "is_active",
            "is_system",
            "balance",
            "initial_balance",
            "haste",
            "balls",
            "order_interval_seconds",
            "killed_at",
            "created_at",
            "holdings",
            "recent_orders",
            "metrics",
        ]
        # order_interval_seconds is derived from haste at creation, not set directly.
        read_only_fields = ["order_interval_seconds", "killed_at", "created_at"]

    def create(self, validated_data):
        # Manual create passes explicit traits; clamp them and derive the cadence
        # (auto-created/mated monkeys go through services.create_monkeys instead).
        control = services.get_global_control()
        validated_data["haste"] = services.clamp_trait(validated_data.get("haste", 0.5))
        validated_data["balls"] = services.clamp_trait(validated_data.get("balls", 0.5))
        validated_data["order_interval_seconds"] = services.derive_interval(
            validated_data["haste"], control
        )
        return super().create(validated_data)

    def get_holdings(self, obj):
        holdings = _holdings_for(obj, only_positive=True)
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
            .select_related("monkey", "stock")
            .order_by("-created_at")[:10]
        )
        return OrderSerializer(orders, many=True).data

    def get_metrics(self, obj):
        return build_monkey_metrics(obj)


class MonkeyBulkCreateSerializer(serializers.Serializer):
    count = serializers.IntegerField(min_value=1, max_value=1000)
    starting_balance = serializers.IntegerField(min_value=0)

    def create(self, validated_data):
        return services.create_monkeys_checked(**validated_data)


class TaskScheduleUpdateSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    hour = serializers.IntegerField(min_value=0, max_value=23)
    minute = serializers.IntegerField(min_value=0, max_value=59)


class IntervalScheduleUpdateSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    every = serializers.IntegerField(min_value=5, max_value=3600)


class GlobalMonkeyControlSerializer(serializers.ModelSerializer):
    market_open = serializers.BooleanField(read_only=True)
    enabled = serializers.BooleanField(read_only=True)

    class Meta:
        model = GlobalMonkeyControl
        fields = [
            "id",
            "market_open",
            "enabled",
            "time_enabled",
            "holiday_enabled",
            "manual_enabled",
            "auto_create_starting_balance",
            "auto_create_min_interval_seconds",
            "auto_create_max_interval_seconds",
            "note",
            "created_at",
            "updated_at",
        ]
        # The time/holiday gates are owned by scheduled tasks; only the manual
        # gate (and the monkey-config fields/note) may be changed through the API.
        read_only_fields = ["time_enabled", "holiday_enabled"]

    def validate(self, attrs):
        # max >= min, accounting for partial (PATCH) updates that send only one.
        low = attrs.get(
            "auto_create_min_interval_seconds",
            getattr(self.instance, "auto_create_min_interval_seconds", None),
        )
        high = attrs.get(
            "auto_create_max_interval_seconds",
            getattr(self.instance, "auto_create_max_interval_seconds", None),
        )
        if low is not None and high is not None and high < low:
            raise serializers.ValidationError(
                {
                    "auto_create_max_interval_seconds": "최대 거래 주기는 최소 거래 주기 이상이어야 합니다."
                }
            )
        return attrs


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


class AccountSummarySerializer(serializers.Serializer):
    kis_cash_balance = serializers.IntegerField()
    kis_holdings_value = serializers.IntegerField()
    kis_total_assets = serializers.IntegerField()
    kis_total_pl = serializers.IntegerField()
    kis_earning_rate = serializers.FloatField()
    unallocated_cash = serializers.IntegerField()
    monkey_count = serializers.IntegerField()
    active_monkey_count = serializers.IntegerField()


class CandlestickSerializer(serializers.Serializer):
    time = serializers.IntegerField()
    open = serializers.FloatField()
    high = serializers.FloatField()
    low = serializers.FloatField()
    close = serializers.FloatField()


class DashboardSummarySerializer(serializers.Serializer):
    active_monkey_count = serializers.IntegerField()
    monkey_index = serializers.FloatField()
    monkey_index_open = serializers.FloatField()
    monkey_index_change = serializers.FloatField()
    latest_orders = OrderSerializer(many=True)


def _orders_by_stock(monkey):
    """Executed orders grouped by ``stock_id``, ascending by (created_at, id).

    Only EXECUTED orders moved the local ledger, so the FIFO/holdings math walks
    those (pending SUBMITTED orders haven't touched balance/Holding yet). Reuses
    the ``_executed_orders`` prefetch (set by ``MonkeyViewSet.get_queryset`` for
    list/retrieve) when present; otherwise queries once for this monkey.
    """
    prefetched = getattr(monkey, "_executed_orders", None)
    if prefetched is None:
        prefetched = (
            Order.objects.filter(monkey=monkey, status=Order.StatusChoices.EXECUTED)
            .select_related("stock")
            .order_by("created_at", "id")
        )
    grouped = {}
    for order in prefetched:
        grouped.setdefault(order.stock_id, []).append(order)
    return grouped


def _pending_orders_for(monkey):
    """Accepted-but-unfilled (SUBMITTED) orders, reusing the ``_pending_orders``
    prefetch when present so the metrics helper runs no extra per-monkey query."""
    prefetched = getattr(monkey, "_pending_orders", None)
    if prefetched is None:
        prefetched = list(
            Order.objects.filter(monkey=monkey, status=Order.StatusChoices.SUBMITTED)
        )
    return list(prefetched)


def _holdings_for(monkey, only_positive):
    """Monkey holdings, reusing the ``_holdings`` prefetch when present.

    ``build_monkey_metrics`` walks every holding (a zero-quantity row still
    contributes its realized P&L); ``build_holdings_breakdown`` only wants
    currently-held (>0) rows.
    """
    prefetched = getattr(monkey, "_holdings", None)
    if prefetched is None:
        qs = Holding.objects.filter(monkey=monkey).select_related("stock")
        if only_positive:
            qs = qs.filter(quantity__gt=0)
        return list(qs)
    if only_positive:
        return [holding for holding in prefetched if holding.quantity > 0]
    return list(prefetched)


def build_monkey_metrics(monkey):
    orders_by_stock = _orders_by_stock(monkey)
    holdings_value = 0
    unrealized_pl = 0
    realized_pl = 0

    for holding in _holdings_for(monkey, only_positive=False):
        orders = orders_by_stock.get(holding.stock_id, [])
        price = _current_price(holding.stock, orders)
        holdings_value += holding.quantity * price
        basis, realized_for_stock = _stock_profit(orders, price)
        unrealized_pl += holding.quantity * price - basis
        realized_pl += realized_for_stock

    total_equity = monkey.balance + holdings_value
    total_pl = total_equity - monkey.initial_balance
    earning_ratio = (
        (total_pl / monkey.initial_balance) if monkey.initial_balance else 0.0
    )

    # 주문가능금액: settled cash minus what pending (SUBMITTED) buy orders reserve.
    pending = _pending_orders_for(monkey)
    buy_reserve = sum(
        (order.estimated_price or 0) * (order.requested_quantity or 0)
        for order in pending
        if order.order_type == Order.OrderTypeChoices.BUY
    )
    return {
        "cash_balance": monkey.balance,
        "available_cash": monkey.balance - buy_reserve,
        "pending_orders": len(pending),
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
    orders_by_stock = _orders_by_stock(monkey)
    breakdown = {}
    for holding in _holdings_for(monkey, only_positive=True):
        orders = orders_by_stock.get(holding.stock_id, [])
        current_price = _current_price(holding.stock, orders)
        cost_basis, _ = _stock_profit(orders, current_price)
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


def _current_price(stock, orders):
    """Live price for a stock, falling back to the last trade price among
    ``orders`` (this stock's succeeded orders, ascending by created_at)."""
    if stock.current_price:
        return stock.current_price
    return _latest_stock_price(orders)


def _latest_stock_price(orders):
    # `orders` is ascending; the most recent order with a usable price wins.
    for order in reversed(orders):
        if order.estimated_price is not None:
            return order.executed_price or order.estimated_price or 0
    return 0


def _stock_profit(orders, current_price):
    quantity = 0
    cost_basis = 0
    realized_pl = 0

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
