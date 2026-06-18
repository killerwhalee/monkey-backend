from django.urls import path
from rest_framework.routers import DefaultRouter

from monkey import viewsets

router = DefaultRouter()
router.register("accounts", viewsets.AccountViewSet, basename="account")
router.register("monkeys", viewsets.MonkeyViewSet, basename="monkey")
router.register(
    "global-monkey-control",
    viewsets.GlobalMonkeyControlViewSet,
    basename="global-monkey-control",
)
router.register(
    "kis-access-tokens", viewsets.KisAccessTokenViewSet, basename="kis-access-token"
)

urlpatterns = router.urls + [
    path(
        "dashboard-summary/",
        viewsets.DashboardSummaryView.as_view(),
        name="dashboard-summary",
    ),
    path(
        "account-summary/",
        viewsets.AccountSummaryView.as_view(),
        name="account-summary",
    ),
    path(
        "candlesticks/",
        viewsets.CandlestickView.as_view(),
        name="candlesticks",
    ),
    path(
        "index-returns/",
        viewsets.IndexReturnsView.as_view(),
        name="index-returns",
    ),
]
