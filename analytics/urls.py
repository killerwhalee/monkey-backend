from django.urls import path

from analytics import viewsets

urlpatterns = [
    path("visits/", viewsets.VisitView.as_view(), name="visits"),
]
