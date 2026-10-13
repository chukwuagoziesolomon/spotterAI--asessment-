from django.urls import path
from . import views

urlpatterns = [
    path("route/", views.RoutePlanView.as_view(), name="route"),
    path("route/map/", views.route_map, name="route-map"),
]
