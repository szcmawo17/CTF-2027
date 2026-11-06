from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Max
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from .game_services import finish_round, open_round, player_for, scoreboard, submit_flag
from .models import CheckerRun, Competition, Round


def is_organizer(user):
    return user.is_active and user.is_staff and user.has_perm("competitions.change_competition")


@login_required
@require_GET
def game_detail(request, competition_id):
    competition = get_object_or_404(Competition, pk=competition_id)
    organizer = is_organizer(request.user)
    try:
        player = player_for(competition, request.user)
    except PermissionDenied:
        if not organizer:
            raise
        player = None
    game_round = competition.rounds.first()
    own_results = CheckerRun.objects.none()
    if player and game_round:
        own_results = CheckerRun.objects.filter(
            flag__round=game_round, flag__team_service__participation=player.participation,
        ).values("flag__team_service__service__name", "put_status", "get_status", "service_status")
    return render(request, "competitions/game.html", {
        "competition": competition, "game_round": game_round, "organizer": organizer,
        "player": player, "own_results": own_results,
        "next_number": (competition.rounds.aggregate(n=Max("number"))["n"] or 0) + 1,
        "captures": player.captures.order_by("-created_at")[:20] if player else [],
    })


@login_required
@require_POST
def game_open_round(request, competition_id):
    competition = get_object_or_404(Competition, pk=competition_id)
    try:
        try:
            number = int(request.POST.get("number", ""))
        except (ValueError, TypeError):
            raise ValidationError("Неверный номер раунда.") from None
        open_round(competition, request.user, number=number)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, "Открыт mock-раунд на 5 минут.")
    return redirect("game_detail", competition_id=competition.pk)


@login_required
@require_POST
def game_finish_round(request, competition_id, round_id):
    game_round = get_object_or_404(Round, pk=round_id, competition_id=competition_id)
    try:
        finish_round(game_round, request.user)
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, "Раунд завершён, результаты checker и очки сохранены.")
    return redirect("game_detail", competition_id=competition_id)


@sensitive_post_parameters("flag")
@login_required
@require_POST
def game_submit_flag(request, competition_id):
    competition = get_object_or_404(Competition, pk=competition_id)
    try:
        result = submit_flag(competition, request.user, request.POST.get("flag", ""))
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        if result.limited:
            response = render(request, "competitions/game_limit.html", {"competition": competition}, status=429)
            response["Retry-After"] = "60"
            return response
        messages.info(request, result.message)
    # Never echo submitted flags or place them in the URL/session/messages.
    return redirect("game_detail", competition_id=competition_id)


@require_GET
def game_scoreboard(request, competition_id):
    competition = get_object_or_404(Competition, pk=competition_id)
    if competition.status == "draft" and not request.user.has_perm("competitions.view_competition"):
        raise Http404
    return render(request, "competitions/scoreboard.html", {
        "competition": competition, "rows": scoreboard(competition),
        "rounds": competition.rounds.all()[:50],
    })
