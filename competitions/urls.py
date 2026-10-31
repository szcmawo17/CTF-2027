from django.urls import path

from . import views


urlpatterns = [
    path("", views.competition_list, name="competition_list"),
    path("<int:competition_id>/", views.competition_detail, name="competition_detail"),
    path(
        "<int:competition_id>/register/<int:team_id>/",
        views.competition_register_team,
        name="competition_register_team",
    ),
    path(
        "<int:competition_id>/withdraw/<int:team_id>/",
        views.competition_withdraw_team,
        name="competition_withdraw_team",
    ),
]
