from django.urls import path

from . import team_views


urlpatterns = [
    path("create/", team_views.team_create, name="team_create"),
    path("<int:team_id>/", team_views.team_detail, name="team_detail"),
    path("<int:team_id>/invite/", team_views.team_invite, name="team_invite"),
    path("<int:team_id>/leave/", team_views.team_leave, name="team_leave"),
    path(
        "<int:team_id>/members/<int:membership_id>/transfer/",
        team_views.team_transfer_captain,
        name="team_transfer_captain",
    ),
    path(
        "<int:team_id>/members/<int:membership_id>/remove/",
        team_views.team_remove_member,
        name="team_remove_member",
    ),
    path(
        "invitations/<int:invitation_id>/accept/",
        team_views.invitation_accept,
        name="team_invitation_accept",
    ),
    path(
        "invitations/<int:invitation_id>/decline/",
        team_views.invitation_decline,
        name="team_invitation_decline",
    ),
    path(
        "invitations/<int:invitation_id>/cancel/",
        team_views.invitation_cancel,
        name="team_invitation_cancel",
    ),
]
