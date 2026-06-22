from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APITestCase

from feedback import tasks
from feedback.models import Feedback


class FeedbackApiTests(APITestCase):
    def setUp(self):
        cache.clear()

        notify_patcher = mock.patch.object(tasks.notify_admin_new_feedback, "delay")
        self.notify_delay = notify_patcher.start()
        self.addCleanup(notify_patcher.stop)

        confirm_patcher = mock.patch.object(
            tasks.send_feedback_confirmation_email, "delay"
        )
        self.confirm_delay = confirm_patcher.start()
        self.addCleanup(confirm_patcher.stop)

        reply_patcher = mock.patch.object(tasks.send_feedback_reply_email, "delay")
        self.reply_delay = reply_patcher.start()
        self.addCleanup(reply_patcher.stop)

    def test_anonymous_can_create_feedback(self):
        response = self.client.post(
            reverse("feedback-list"),
            {
                "email": "visitor@example.com",
                "category": "bug",
                "subject": "버그를 발견했어요",
                "message": "상세 내용입니다.",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Feedback.objects.count(), 1)
        feedback = Feedback.objects.get()
        self.assertEqual(feedback.email, "visitor@example.com")
        self.assertEqual(feedback.category, "bug")
        self.assertEqual(feedback.status, Feedback.StatusChoices.NEW)
        self.notify_delay.assert_called_once_with(feedback.id)
        self.confirm_delay.assert_called_once_with(feedback.id)

    def test_anonymous_cannot_list_or_retrieve_feedback(self):
        feedback = Feedback.objects.create(
            email="visitor@example.com",
            category="general",
            subject="제목",
            message="내용",
        )

        list_response = self.client.get(reverse("feedback-list"))
        detail_response = self.client.get(
            reverse("feedback-detail", args=[feedback.id])
        )

        self.assertEqual(list_response.status_code, 401)
        self.assertEqual(detail_response.status_code, 401)

    def test_staff_can_list_and_retrieve_feedback(self):
        feedback = Feedback.objects.create(
            email="visitor@example.com",
            category="feature",
            subject="제목",
            message="내용",
        )
        user = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user)

        list_response = self.client.get(reverse("feedback-list"))
        detail_response = self.client.get(
            reverse("feedback-detail", args=[feedback.id])
        )

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.data["category_label"], "기능 제안")
        self.assertEqual(detail_response.data["status_label"], "신규")

    def test_staff_can_reply_to_feedback(self):
        feedback = Feedback.objects.create(
            email="visitor@example.com",
            category="general",
            subject="제목",
            message="내용",
        )
        user = get_user_model().objects.create_user(
            username="admin", password="pw", is_staff=True
        )
        self.client.force_authenticate(user)

        response = self.client.post(
            reverse("feedback-reply", args=[feedback.id]),
            {"reply_message": "답변입니다."},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        feedback.refresh_from_db()
        self.assertEqual(feedback.status, Feedback.StatusChoices.ANSWERED)
        self.assertEqual(feedback.reply_message, "답변입니다.")
        self.assertIsNotNone(feedback.replied_at)
        self.assertEqual(feedback.replied_by, user)
        self.reply_delay.assert_called_once_with(feedback.id)

    def test_non_staff_cannot_reply_to_feedback(self):
        feedback = Feedback.objects.create(
            email="visitor@example.com",
            category="general",
            subject="제목",
            message="내용",
        )
        user = get_user_model().objects.create_user(username="user", password="pw")
        self.client.force_authenticate(user)

        response = self.client.post(
            reverse("feedback-reply", args=[feedback.id]),
            {"reply_message": "답변입니다."},
            format="json",
        )

        self.assertEqual(response.status_code, 403)

    def test_create_feedback_is_throttled(self):
        payload = {
            "email": "visitor@example.com",
            "category": "general",
            "subject": "제목",
            "message": "내용",
        }

        for _ in range(5):
            response = self.client.post(
                reverse("feedback-list"), payload, format="json"
            )
            self.assertEqual(response.status_code, 201)

        response = self.client.post(reverse("feedback-list"), payload, format="json")
        self.assertEqual(response.status_code, 429)


class FeedbackTaskTests(APITestCase):
    def test_notify_admin_new_feedback_sends_email(self):
        feedback = Feedback.objects.create(
            email="visitor@example.com",
            category="bug",
            subject="버그 제목",
            message="버그 내용",
        )

        with self.settings(FEEDBACK_ADMIN_EMAIL="admin@example.com"):
            result = tasks.notify_admin_new_feedback(feedback.id)["output"]

        self.assertEqual(result["notified"], "admin@example.com")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["admin@example.com"])
        self.assertIn("버그 제목", message.subject)
        # Plain-text body carries the content; a branded HTML alternative is attached.
        self.assertIn("버그 내용", message.body)
        html_body, content_type = message.alternatives[0]
        self.assertEqual(content_type, "text/html")
        self.assertIn("MONKEY", html_body)
        self.assertIn("발신 전용", html_body)

    def test_notify_admin_new_feedback_skips_when_unconfigured(self):
        feedback = Feedback.objects.create(
            email="visitor@example.com",
            category="bug",
            subject="버그 제목",
            message="버그 내용",
        )

        with self.settings(FEEDBACK_ADMIN_EMAIL=""):
            result = tasks.notify_admin_new_feedback(feedback.id)["output"]

        self.assertIn("skipped", result)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_feedback_confirmation_email(self):
        feedback = Feedback.objects.create(
            email="visitor@example.com",
            category="bug",
            subject="버그를 발견했어요",
            message="상세 내용입니다.",
        )

        result = tasks.send_feedback_confirmation_email(feedback.id)["output"]

        self.assertEqual(result["sent_to"], "visitor@example.com")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["visitor@example.com"])
        self.assertIn("접수", message.body)
        self.assertIn("상세 내용입니다.", message.body)
        html_body, content_type = message.alternatives[0]
        self.assertEqual(content_type, "text/html")
        self.assertIn("상세 내용입니다.", html_body)

    def test_send_feedback_reply_email(self):
        feedback = Feedback.objects.create(
            email="visitor@example.com",
            category="general",
            subject="제목",
            message="내용",
            reply_message="답변 내용입니다.",
        )

        result = tasks.send_feedback_reply_email(feedback.id)["output"]

        self.assertEqual(result["sent_to"], "visitor@example.com")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["visitor@example.com"])
        self.assertIn("답변 내용입니다.", message.body)
        html_body, content_type = message.alternatives[0]
        self.assertEqual(content_type, "text/html")
        self.assertIn("답변 내용입니다.", html_body)
        self.assertIn("발신 전용", html_body)
