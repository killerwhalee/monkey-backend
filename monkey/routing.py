from django.urls import re_path

from monkey import consumers

websocket_urlpatterns = [
    re_path(r"^ws/dashboard/$", consumers.DashboardConsumer.as_asgi()),
    re_path(r"^ws/admin/$", consumers.AdminConsumer.as_asgi()),
]
