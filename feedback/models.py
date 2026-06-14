from django.db import models


class Feedback(models.Model):
    """A piece of user feedback submitted from the dashboard, with an optional admin reply."""

    class CategoryChoices(models.TextChoices):
        BUG = "bug", "버그 신고"
        FEATURE = "feature", "기능 제안"
        GENERAL = "general", "일반 의견"
        OTHER = "other", "기타"

    class StatusChoices(models.TextChoices):
        NEW = "new", "신규"
        ANSWERED = "answered", "답변 완료"

    email = models.EmailField(
        "Email",
    )
    category = models.CharField(
        "Category",
        max_length=32,
        choices=CategoryChoices,
        default=CategoryChoices.GENERAL,
    )
    subject = models.CharField(
        "Subject",
        max_length=256,
    )
    message = models.TextField(
        "Message",
    )
    status = models.CharField(
        "Status",
        max_length=32,
        choices=StatusChoices,
        default=StatusChoices.NEW,
    )
    reply_message = models.TextField(
        "Reply message",
        blank=True,
    )
    replied_at = models.DateTimeField(
        "Replied at",
        null=True,
        blank=True,
    )
    replied_by = models.ForeignKey(
        "auth.User",
        verbose_name="Replied by",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feedback_replies",
    )
    created_at = models.DateTimeField(
        "Created at",
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        "Updated at",
        auto_now=True,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.__class__.__name__} #{self.pk:04d}] {self.email} {self.subject}"
