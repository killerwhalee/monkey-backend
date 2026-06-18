"""WebSocket consumers for live dashboard/admin updates (push-only).

``DashboardConsumer`` is public and joins the dashboard groups (orders, index,
monkeys). ``AdminConsumer`` is staff-only and joins the admin task group. Neither
expects messages from the client — all traffic is server→client pushes published
via ``monkey.realtime``.
"""

from channels.generic.websocket import AsyncJsonWebsocketConsumer

DASHBOARD_GROUPS = ("dashboard.orders", "dashboard.index", "dashboard.monkeys")
ADMIN_GROUPS = ("admin.tasks",)


class DashboardConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        for group in DASHBOARD_GROUPS:
            await self.channel_layer.group_add(group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        for group in DASHBOARD_GROUPS:
            await self.channel_layer.group_discard(group, self.channel_name)

    # group_send messages carry a "type" that maps to one of these handlers.
    async def order_event(self, content):
        await self.send_json(content["data"])

    async def index_event(self, content):
        await self.send_json(content["data"])

    async def monkey_event(self, content):
        await self.send_json(content["data"])


class AdminConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if not (user and user.is_authenticated and user.is_staff):
            await self.close(code=4403)
            return
        for group in ADMIN_GROUPS:
            await self.channel_layer.group_add(group, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        for group in ADMIN_GROUPS:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def task_event(self, content):
        await self.send_json(content["data"])
