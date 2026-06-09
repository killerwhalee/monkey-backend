from rest_framework import permissions, viewsets

from market import models, serializers


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
    filterset_fields = ["monkey", "stock", "status", "order_type"]
