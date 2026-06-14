from django.utils import timezone
from rest_framework import mixins, permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from feedback import serializers, tasks
from feedback.models import Feedback


class IsStaffOrCreate(permissions.BasePermission):
    """Anyone may submit feedback; viewing/replying requires staff (the list contains emails)."""

    def has_permission(self, request, view):
        if view.action == "create":
            return True
        return bool(request.user and request.user.is_staff)


class FeedbackViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Feedback.objects.all().order_by("-created_at")
    permission_classes = [IsStaffOrCreate]

    def get_serializer_class(self):
        if self.action == "create":
            return serializers.FeedbackCreateSerializer
        if self.action == "reply":
            return serializers.FeedbackReplySerializer
        return serializers.FeedbackSerializer

    def get_throttles(self):
        if self.action == "create":
            self.throttle_scope = "feedback-create"
            return [ScopedRateThrottle()]
        return super().get_throttles()

    def perform_create(self, serializer):
        feedback = serializer.save()
        tasks.notify_admin_new_feedback.delay(feedback.id)

    @action(detail=True, methods=["post"], permission_classes=[permissions.IsAdminUser])
    def reply(self, request, pk=None):
        feedback = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        feedback.reply_message = serializer.validated_data["reply_message"]
        feedback.status = Feedback.StatusChoices.ANSWERED
        feedback.replied_at = timezone.now()
        feedback.replied_by = request.user
        feedback.save(
            update_fields=[
                "reply_message",
                "status",
                "replied_at",
                "replied_by",
                "updated_at",
            ]
        )

        tasks.send_feedback_reply_email.delay(feedback.id)

        return Response(serializers.FeedbackSerializer(feedback).data)
