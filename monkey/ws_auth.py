"""JWT authentication for WebSocket connections.

Browsers can't set an Authorization header on a WebSocket, so the access token
is passed as a ``?token=`` query-string param. This middleware validates it with
simplejwt and puts the resolved user on the connection scope. Anonymous is
allowed (the dashboard consumer is public); consumers that need staff enforce it
themselves.
"""

from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser


@database_sync_to_async
def _get_user(token):
    from rest_framework_simplejwt.exceptions import TokenError
    from rest_framework_simplejwt.tokens import AccessToken

    try:
        access = AccessToken(token)
        user = get_user_model().objects.get(pk=access["user_id"])
    except (TokenError, KeyError, get_user_model().DoesNotExist):
        return AnonymousUser()
    return user


class JWTAuthMiddleware(BaseMiddleware):
    async def __call__(self, scope, receive, send):
        query = parse_qs(scope.get("query_string", b"").decode())
        token = (query.get("token") or [None])[0]
        if token:
            scope["user"] = await _get_user(token)
        else:
            scope.setdefault("user", AnonymousUser())
        return await super().__call__(scope, receive, send)
