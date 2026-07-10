from rest_framework import pagination, permissions, viewsets

from market import models, serializers


class OrderPagination(pagination.PageNumberPagination):
    """Cap the append-only Order ledger, which grows without bound.

    Without this, an unfiltered ``GET /api/orders/`` serializes the entire
    ledger (tens of MB) in a single response. Clients that need a full
    per-monkey history page through via ``?page=`` / ``?monkey=``.
    """

    page_size = 100
    page_size_query_param = "page_size"
    max_page_size = 1000


class IsAdminOrReadOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_staff)


class StockViewSet(viewsets.ModelViewSet):
    queryset = models.Stock.objects.all().order_by("market", "ticker")
    serializer_class = serializers.StockSerializer
    permission_classes = [IsAdminOrReadOnly]
    search_fields = ["market", "ticker", "name"]
    filterset_fields = ["market"]


class HoldingViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = (
        models.Holding.objects.select_related("monkey", "stock").all().order_by("id")
    )
    serializer_class = serializers.HoldingSerializer
    permission_classes = [permissions.AllowAny]
    filterset_fields = ["monkey", "stock"]


class OrderViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = (
        models.Order.objects.select_related("monkey", "stock")
        .all()
        .order_by("-created_at", "-id")
    )
    serializer_class = serializers.OrderSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = OrderPagination
    filterset_fields = ["monkey", "stock", "status", "order_type"]
