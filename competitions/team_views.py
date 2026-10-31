from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from .models import Team, TeamInvitation, TeamMembership
from .team_forms import TeamCreationForm, TeamInvitationForm
from .team_services import (
    cancel_invitation,
    create_team,
    invite_user,
    leave_team,
    remove_member,
    require_captain,
    respond_to_invitation,
    transfer_captain,
)


def _team_context(team, user, invitation_form=None):
    membership = TeamMembership.objects.filter(team=team, user=user).first()
    if membership is None:
        raise PermissionDenied("Вы не состоите в этой команде.")

    is_captain = membership.role == "captain"
    return {
        "team": team,
        "membership": membership,
        "memberships": team.memberships.select_related("user").order_by(
            "role", "user__username"
        ),
        "is_captain": is_captain,
        "invitation_form": invitation_form or TeamInvitationForm(),
        "pending_invitations": (
            team.invitations.filter(status="pending").select_related("invitee")
            if is_captain
            else ()
        ),
    }


@login_required
@require_http_methods(["GET", "POST"])
def team_create(request):
    form = TeamCreationForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            team = create_team(form.cleaned_data["name"], request.user)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, "Команда создана. Вы назначены капитаном.")
            return redirect("team_detail", team_id=team.pk)

    return render(request, "competitions/team_create.html", {"form": form})


@login_required
@require_http_methods(["GET"])
def team_detail(request, team_id):
    team = get_object_or_404(Team, pk=team_id)
    return render(
        request,
        "competitions/team_detail.html",
        _team_context(team, request.user),
    )


@login_required
@require_POST
def team_invite(request, team_id):
    team = get_object_or_404(Team, pk=team_id)
    require_captain(team, request.user)
    form = TeamInvitationForm(request.POST)
    if form.is_valid():
        try:
            invite_user(team, request.user, form.cleaned_data["username"])
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, "Приглашение отправлено.")
            return redirect("team_detail", team_id=team.pk)

    return render(
        request,
        "competitions/team_detail.html",
        _team_context(team, request.user, invitation_form=form),
    )


def _bad_request(error):
    return HttpResponseBadRequest(" ".join(error.messages))


@login_required
@require_POST
def invitation_accept(request, invitation_id):
    invitation = get_object_or_404(TeamInvitation, pk=invitation_id)
    try:
        respond_to_invitation(invitation, request.user, accept=True)
    except ValidationError as error:
        return _bad_request(error)
    messages.success(request, "Вы вступили в команду.")
    return redirect("team_detail", team_id=invitation.team_id)


@login_required
@require_POST
def invitation_decline(request, invitation_id):
    invitation = get_object_or_404(TeamInvitation, pk=invitation_id)
    try:
        respond_to_invitation(invitation, request.user, accept=False)
    except ValidationError as error:
        return _bad_request(error)
    messages.success(request, "Приглашение отклонено.")
    return redirect("profile")


@login_required
@require_POST
def invitation_cancel(request, invitation_id):
    invitation = get_object_or_404(TeamInvitation, pk=invitation_id)
    try:
        cancel_invitation(invitation, request.user)
    except ValidationError as error:
        return _bad_request(error)
    messages.success(request, "Приглашение отозвано.")
    return redirect("team_detail", team_id=invitation.team_id)


@login_required
@require_POST
def team_transfer_captain(request, team_id, membership_id):
    team = get_object_or_404(Team, pk=team_id)
    successor = get_object_or_404(TeamMembership, pk=membership_id, team=team)
    try:
        transfer_captain(team, request.user, successor.user)
    except ValidationError as error:
        return _bad_request(error)
    messages.success(request, "Капитанство передано.")
    return redirect("team_detail", team_id=team.pk)


@login_required
@require_POST
def team_remove_member(request, team_id, membership_id):
    team = get_object_or_404(Team, pk=team_id)
    membership = get_object_or_404(TeamMembership, pk=membership_id, team=team)
    try:
        remove_member(team, request.user, membership.user)
    except ValidationError as error:
        return _bad_request(error)
    messages.success(request, "Участник исключён из команды.")
    return redirect("team_detail", team_id=team.pk)


@login_required
@require_POST
def team_leave(request, team_id):
    team = get_object_or_404(Team, pk=team_id)
    try:
        leave_team(team, request.user)
    except ValidationError as error:
        return _bad_request(error)
    messages.success(request, "Вы вышли из команды.")
    return redirect("profile")
