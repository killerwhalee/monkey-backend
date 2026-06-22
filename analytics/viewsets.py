from rest_framework import permissions, views
from rest_framework.response import Response

from analytics import services


class VisitView(views.APIView):
    """Public dashboard visitor counter.

    ``POST`` records a visit (deduped per visitor per day) and returns the
    updated counts; ``GET`` returns the current counts without recording.
    """

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response(services.current_stats())

    def post(self, request):
        return Response(services.record_visit(request))
