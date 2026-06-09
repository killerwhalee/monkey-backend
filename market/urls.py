from rest_framework.routers import DefaultRouter

from market import viewsets

router = DefaultRouter()
router.register("stocks", viewsets.StockViewSet, basename="stock")
router.register("holdings", viewsets.HoldingViewSet, basename="holding")
router.register("orders", viewsets.OrderViewSet, basename="order")

urlpatterns = router.urls
