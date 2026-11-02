from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import Competition, CompetitionParticipation, Team
from .participation_services import register_team, withdraw_team


def competition_list(request):
    competitions = Competition.objects.prefetch_related(Prefetch("participations", queryset=CompetitionParticipation.objects.filter(is_active=True).select_related("team")))
    if not request.user.has_perm("competitions.view_competition"):
        competitions = competitions.exclude(status="draft")

    return render(
        request,
        "competitions/list.html",
        {"competitions": competitions}
        
    )


def competition_detail(request, competition_id, error=None, response_status=200):
    competition = get_object_or_404(Competition, pk=competition_id)
    if competition.status == "draft" and not request.user.has_perm(
        "competitions.view_competition"
    ):
        raise Http404("Соревнование недоступно.")

    participations = list(
        competition.participations.filter(is_active=True).select_related("team").prefetch_related("services", "players__user")
    )
    captain_teams = (
        [
            membership.team
            for membership in request.user.team_memberships.filter(
                role="captain"
            ).select_related("team")
        ]
        if request.user.is_authenticated
        else []
    )
    captain_team_ids = {team.pk for team in captain_teams}
    registered_team_ids = {participation.team_id for participation in participations}
    registration_open = competition.registration_open

    return render(
        request,
        "competitions/detail.html",
        {
            "competition": competition,
            "registration_error": error,
            "registration_open": registration_open,
            "available_teams": [
                team for team in captain_teams if team.pk not in registered_team_ids
            ] if registration_open else [],
            "participation_rows": [
                {
                    "participation": participation,
                    "can_withdraw": (
                        registration_open
                        and participation.team_id in captain_team_ids
                        and not participation.services.exists()
                    ),
                }
                for participation in participations
            ],
        },
        status=response_status,
    )


@login_required
@require_POST
def competition_register_team(request, competition_id, team_id):
    competition = get_object_or_404(Competition, pk=competition_id)
    team = get_object_or_404(Team, pk=team_id)
    try:
        register_team(competition, team, request.user)
    except ValidationError as error:
        return competition_detail(request, competition_id, error=" ".join(error.messages), response_status=400)
    messages.success(request, "Команда зарегистрирована на соревнование.")
    return redirect("competition_detail", competition_id=competition.pk)


@login_required
@require_POST
def competition_withdraw_team(request, competition_id, team_id):
    competition = get_object_or_404(Competition, pk=competition_id)
    team = get_object_or_404(Team, pk=team_id)
    try:
        withdraw_team(competition, team, request.user)
    except ValidationError as error:
        return competition_detail(request, competition_id, error=" ".join(error.messages), response_status=400)
    messages.success(request, "Команда снята с соревнования.")
    return redirect("competition_detail", competition_id=competition.pk)
