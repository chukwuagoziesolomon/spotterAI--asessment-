from urllib.parse import urlencode

from django.shortcuts import render
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RouteQuerySerializer
from .services import optimizer, planner, providers


class RoutePlanView(APIView):
    """GET/POST /api/route/?start=Dallas, TX&finish=Seattle, WA  (also accepts "lat,lng")."""

    def _handle(self, data):
        ser = RouteQuerySerializer(data=data)
        ser.is_valid(raise_exception=True)
        try:
            plan = planner.build_plan(ser.validated_data["start"], ser.validated_data["finish"])
        except (providers.RoutingError, optimizer.NoFeasibleRoute) as e:
            return Response({"error": str(e)}, status=422)
        q = ser.validated_data
        plan = {**plan, "map_url": self.request.build_absolute_uri(
            "/api/route/map/?" + urlencode({"start": q["start"], "finish": q["finish"]}))}
        return Response(plan)

    def get(self, request):
        return self._handle(request.query_params)

    def post(self, request):
        return self._handle(request.data)


def route_map(request):
    """Browser map (Leaflet + OpenStreetMap tiles) that renders the API response."""
    return render(request, "planner/map.html", {
        "start": request.GET.get("start", ""), "finish": request.GET.get("finish", "")})
