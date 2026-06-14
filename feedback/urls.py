from rest_framework.routers import DefaultRouter

from feedback import viewsets

router = DefaultRouter()
router.register("feedback", viewsets.FeedbackViewSet, basename="feedback")

urlpatterns = router.urls
