from rest_framework import permissions, status, views, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from monkey import serializers, services
from monkey.models import GlobalMonkeyControl, KisAccessToken, Monkey


class IsAdminOrReadOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_staff)


class MonkeyViewSet(viewsets.ModelViewSet):
    queryset = Monkey.objects.filter(is_system=False).order_by("id")
    serializer_class = serializers.MonkeySerializer
    permission_classes = [IsAdminOrReadOnly]

    @action(
        detail=False,
        methods=["post"],
        permission_classes=[permissions.IsAdminUser],
        url_path="bulk-create",
    )
    def bulk_create(self, request):
        serializer = serializers.MonkeyBulkCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        monkeys = serializer.save()
        return Response(
            serializers.MonkeySerializer(monkeys, many=True).data,
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[permissions.IsAdminUser],
        url_path="force-kill",
    )
    def force_kill(self, request, pk=None):
        monkey = self.get_object()
        try:
            services.kill_monkey(monkey)
        except services.KillNotAllowedError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(self.get_serializer(monkey).data)

    @action(
        detail=False,
        methods=["post"],
        permission_classes=[permissions.IsAdminUser],
        url_path="auto-create",
    )
    def auto_create(self, request):
        monkeys = services.auto_create_monkeys()
        return Response(serializers.MonkeySerializer(monkeys, many=True).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        monkeys = self.get_queryset()
        data = [
            {
                "id": monkey.id,
                "name": monkey.name,
                "is_active": monkey.is_active,
                "metrics": serializers.build_monkey_metrics(monkey),
            }
            for monkey in monkeys
        ]
        return Response(data)


class GlobalMonkeyControlViewSet(viewsets.ModelViewSet):
    serializer_class = serializers.GlobalMonkeyControlSerializer
    permission_classes = [IsAdminOrReadOnly]

    def get_queryset(self):
        services.get_global_control()
        return GlobalMonkeyControl.objects.all().order_by("id")

    @action(
        detail=False,
        methods=["get", "patch"],
        url_path="current",
        permission_classes=[IsAdminOrReadOnly],
    )
    def current(self, request):
        control = services.get_global_control()
        if request.method == "PATCH":
            self.check_object_permissions(request, control)
            serializer = self.get_serializer(control, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)
        return Response(self.get_serializer(control).data)


class KisAccessTokenViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = KisAccessToken.objects.all().order_by("environment")
    serializer_class = serializers.KisAccessTokenSerializer
    permission_classes = [permissions.IsAdminUser]


class DashboardSummaryView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        data = services.build_dashboard_summary()
        return Response(serializers.DashboardSummarySerializer(data).data)


class AccountSummaryView(views.APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        data = services.build_account_summary()
        return Response(serializers.AccountSummarySerializer(data).data)


class CandlestickView(views.APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        unit = request.query_params.get("unit", "1d")
        if unit not in services.CANDLE_UNIT_SECONDS:
            unit = "1d"
        try:
            limit = min(int(request.query_params.get("limit", 120)), 1000)
        except (TypeError, ValueError):
            limit = 120
        data = services.build_earning_ratio_candlesticks(unit=unit, limit=limit)
        return Response(serializers.CandlestickSerializer(data, many=True).data)
