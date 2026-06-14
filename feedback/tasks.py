from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

from feedback.models import Feedback


@shared_task
def notify_admin_new_feedback(feedback_id):
    if not settings.FEEDBACK_ADMIN_EMAIL:
        return {
            "feedback_id": feedback_id,
            "skipped": "FEEDBACK_ADMIN_EMAIL not configured",
        }

    feedback = Feedback.objects.get(pk=feedback_id)
    send_mail(
        subject=f"[Monkey 피드백] {feedback.get_category_display()}: {feedback.subject}",
        message=(
            f"보낸 사람: {feedback.email}\n"
            f"분류: {feedback.get_category_display()}\n\n"
            f"{feedback.message}"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[settings.FEEDBACK_ADMIN_EMAIL],
    )
    return {"feedback_id": feedback_id, "notified": settings.FEEDBACK_ADMIN_EMAIL}


@shared_task
def send_feedback_reply_email(feedback_id):
    feedback = Feedback.objects.get(pk=feedback_id)
    send_mail(
        subject=f"[Monkey] 문의하신 '{feedback.subject}'에 대한 답변입니다",
        message=feedback.reply_message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[feedback.email],
    )
    return {"feedback_id": feedback_id, "sent_to": feedback.email}
