from rest_framework import serializers

from feedback.models import Feedback


class FeedbackCreateSerializer(serializers.ModelSerializer):
    """Public submission — only the fields an anonymous visitor can set."""

    class Meta:
        model = Feedback
        fields = ["email", "category", "subject", "message"]


class FeedbackSerializer(serializers.ModelSerializer):
    """Staff list/detail representation."""

    category_label = serializers.CharField(
        source="get_category_display", read_only=True
    )
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    replied_by_username = serializers.CharField(
        source="replied_by.username", read_only=True, default=""
    )

    class Meta:
        model = Feedback
        fields = [
            "id",
            "email",
            "category",
            "category_label",
            "subject",
            "message",
            "status",
            "status_label",
            "reply_message",
            "replied_at",
            "replied_by_username",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class FeedbackReplySerializer(serializers.Serializer):
    reply_message = serializers.CharField(allow_blank=False)
