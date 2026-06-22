from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from feedback.models import Feedback


def _send_html_email(subject, template, context, recipient_list):
    """Render the .txt/.html pair for ``template`` and send a multipart email.

    The plain-text part is the body; the styled HTML is attached as an
    alternative so clients that support it show the branded layout.
    """
    context = {**context, "site_url": settings.SITE_URL}
    text_body = render_to_string(f"feedback/email/{template}.txt", context)
    html_body = render_to_string(f"feedback/email/{template}.html", context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipient_list,
    )
    message.attach_alternative(html_body, "text/html")
    message.send()


@shared_task
def notify_admin_new_feedback(feedback_id):
    if not settings.FEEDBACK_ADMIN_EMAIL:
        return {
            "feedback_id": feedback_id,
            "skipped": "FEEDBACK_ADMIN_EMAIL not configured",
        }

    feedback = Feedback.objects.get(pk=feedback_id)
    _send_html_email(
        subject=f"[Monkey 피드백] {feedback.get_category_display()}: {feedback.subject}",
        template="new_feedback",
        context={"feedback": feedback},
        recipient_list=[settings.FEEDBACK_ADMIN_EMAIL],
    )
    return {"feedback_id": feedback_id, "notified": settings.FEEDBACK_ADMIN_EMAIL}


@shared_task
def send_feedback_reply_email(feedback_id):
    feedback = Feedback.objects.get(pk=feedback_id)
    _send_html_email(
        subject=f"[Monkey] 문의하신 '{feedback.subject}'에 대한 답변입니다",
        template="reply",
        context={"feedback": feedback},
        recipient_list=[feedback.email],
    )
    return {"feedback_id": feedback_id, "sent_to": feedback.email}
