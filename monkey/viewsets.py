from rest_framework import permissions, status, views, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from monkey import serializers, services
from monkey.models import GlobalMonkeyControl, KisAccessToken, Monkey
from monkey.task_catalog import TASK_CATALOG, TASK_MAP


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
        try:
            monkeys = serializer.save()
        except services.InsufficientCashError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
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

    @action(
        detail=False,
        methods=["get"],
        url_path="tasks",
        permission_classes=[permissions.IsAdminUser],
    )
    def tasks(self, request):
        """List the Celery tasks an admin may trigger manually."""
        return Response(TASK_CATALOG)

    @action(
        detail=False,
        methods=["post"],
        url_path="run-task",
        permission_classes=[permissions.IsAdminUser],
    )
    def run_task(self, request):
        """Enqueue one runnable task by name (see ``tasks`` for the catalog)."""
        name = request.data.get("task")
        task = TASK_MAP.get(name)
        if task is None:
            return Response(
                {"detail": f"알 수 없는 작업: {name}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = task.delay()
        except Exception as exc:  # broker unreachable, etc.
            return Response(
                {"detail": f"작업 실행에 실패했습니다: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"task": name, "id": result.id})

    @action(
        detail=False,
        methods=["get"],
        url_path="schedules",
        permission_classes=[permissions.IsAdminUser],
    )
    def schedules(self, request):
        """Crontab (daily) task schedules, ascending by time of day."""
        return Response(services.list_task_schedules())

    @action(
        detail=False,
        methods=["post"],
        url_path="update-schedule",
        permission_classes=[permissions.IsAdminUser],
    )
    def update_schedule(self, request):
        """Change a daily task's scheduled time of day."""
        serializer = serializers.TaskScheduleUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            row = services.set_task_schedule(data["id"], data["hour"], data["minute"])
        except services.ScheduleNotFoundError:
            return Response(
                {"detail": "해당 스케줄을 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except services.NotATimeScheduleError:
            return Response(
                {"detail": "이 작업은 시간 기반 스케줄이 아닙니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(row)

    @action(
        detail=False,
        methods=["get"],
        url_path="interval-schedules",
        permission_classes=[permissions.IsAdminUser],
    )
    def interval_schedules(self, request):
        """System interval tasks (price polling, earning-ratio ticks)."""
        return Response(services.list_interval_schedules())

    @action(
        detail=False,
        methods=["post"],
        url_path="update-interval",
        permission_classes=[permissions.IsAdminUser],
    )
    def update_interval(self, request):
        """Change a system interval task's cadence (seconds)."""
        serializer = serializers.IntervalScheduleUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            row = services.set_interval_schedule(data["id"], data["every"])
        except services.ScheduleNotFoundError:
            return Response(
                {"detail": "해당 스케줄을 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )
        except services.NotATimeScheduleError:
            return Response(
                {"detail": "이 작업은 주기 기반 스케줄이 아닙니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(row)


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
