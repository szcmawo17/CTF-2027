from django.urls import path

from . import game_views, views


urlpatterns = [
    path("<int:competition_id>/game/", game_views.game_detail, name="game_detail"),
    path("<int:competition_id>/scores/", game_views.game_scoreboard, name="game_scoreboard"),
    path("<int:competition_id>/game/open/", game_views.game_open_round, name="game_open_round"),
    path("<int:competition_id>/game/finish/<int:round_id>/", game_views.game_finish_round, name="game_finish_round"),
    path("<int:competition_id>/game/submit/", game_views.game_submit_flag, name="game_submit_flag"),
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
