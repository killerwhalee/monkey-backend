"""
ASGI config for core project.

Exposes the ASGI callable as a module-level variable named ``application``.
Routes HTTP to Django's ASGI app and WebSocket to the Channels stack. In
production gunicorn serves HTTP and a separate daphne process serves /ws.

https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

# Initialize Django before importing anything that touches models/settings.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

import monkey.routing  # noqa: E402
from monkey.ws_auth import JWTAuthMiddleware  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AllowedHostsOriginValidator(
            JWTAuthMiddleware(
                AuthMiddlewareStack(URLRouter(monkey.routing.websocket_urlpatterns))
            )
        ),
    }
)
