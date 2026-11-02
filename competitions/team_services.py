from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError

from .mutation import serialized_mutation
from .roster_services import require_mutable_team, sync_team_rosters

from .models import Team, TeamInvitation, TeamMembership


def require_captain(team, actor):
    if not actor.is_authenticated or not TeamMembership.objects.filter(
        team=team, user=actor, role="captain"
    ).exists():
        raise PermissionDenied("Только капитан может управлять командой.")


@serialized_mutation
def create_team(name, creator):
    if not creator.is_authenticated:
        raise PermissionDenied("Для создания команды необходим вход.")

    team = Team(name=name.strip())
    team.full_clean()
    team.save()
    TeamMembership.objects.create(team=team, user=creator, role="captain")
    return team


@serialized_mutation
def invite_user(team, actor, username):
    require_captain(team, actor)
    try:
        invitee = get_user_model()._default_manager.get_by_natural_key(
            username.strip()
        )
    except get_user_model().DoesNotExist as error:
        raise ValidationError("Пользователь с таким именем не найден.") from error

    if not invitee.is_active:
        raise ValidationError("Этого пользователя нельзя пригласить.")
    if TeamMembership.objects.filter(team=team, user=invitee).exists():
        raise ValidationError("Пользователь уже состоит в команде.")
    if TeamInvitation.objects.filter(
        team=team, invitee=invitee, status="pending"
    ).exists():
        raise ValidationError("Приглашение уже ожидает ответа.")

    try:
        return TeamInvitation.objects.create(
            team=team, invitee=invitee, invited_by=actor
        )
    except IntegrityError as error:
        raise ValidationError("Приглашение уже ожидает ответа.") from error


@serialized_mutation
def respond_to_invitation(invitation, actor, accept):
    current = TeamInvitation.objects.get(pk=invitation.pk)
    if current.invitee_id != actor.pk:
        raise PermissionDenied("Это приглашение адресовано другому пользователю.")
    new_status = "accepted" if accept else "declined"
    if not TeamInvitation.objects.filter(
        pk=current.pk, status="pending"
    ).update(status=new_status):
        raise ValidationError("Приглашение уже обработано.")

    if accept:
        require_mutable_team(current.team)
        TeamMembership.objects.get_or_create(
            team=current.team, user=actor, defaults={"role": "member"}
        )
        sync_team_rosters(current.team)
    current.status = new_status
    return current


@serialized_mutation
def cancel_invitation(invitation, actor):
    current = TeamInvitation.objects.get(pk=invitation.pk)
    require_captain(current.team, actor)
    if not TeamInvitation.objects.filter(
        pk=current.pk, status="pending"
    ).update(status="cancelled"):
        raise ValidationError("Приглашение уже обработано.")
    current.status = "cancelled"
    return current


@serialized_mutation
def transfer_captain(team, actor, new_captain):
    require_captain(team, actor)
    require_mutable_team(team)
    current = TeamMembership.objects.select_for_update().filter(
        team=team, user=actor, role="captain"
    ).first()
    if current is None:
        raise PermissionDenied("Только капитан может передать капитанство.")

    successor = TeamMembership.objects.select_for_update().filter(
        team=team, user=new_captain
    ).first()
    if successor is None or successor.pk == current.pk:
        raise ValidationError("Выберите другого участника этой команды.")

    current.role = "member"
    current.save(update_fields=["role"])
    successor.role = "captain"
    successor.save(update_fields=["role"])
    sync_team_rosters(team)
    return successor


@serialized_mutation
def remove_member(team, actor, member):
    require_captain(team, actor)
    require_mutable_team(team)
    membership = TeamMembership.objects.select_for_update().filter(
        team=team, user=member
    ).first()
    if membership is None:
        raise ValidationError("Пользователь не состоит в этой команде.")
    if membership.role == "captain":
        raise ValidationError("Сначала передайте капитанство другому участнику.")
    membership.delete()
    sync_team_rosters(team)


@serialized_mutation
def leave_team(team, actor):
    require_mutable_team(team)
    membership = TeamMembership.objects.select_for_update().filter(
        team=team, user=actor
    ).first()
    if membership is None:
        raise PermissionDenied("Вы не состоите в этой команде.")
    if membership.role == "captain":
        raise ValidationError("Сначала передайте капитанство другому участнику.")
    membership.delete()
    sync_team_rosters(team)


@serialized_mutation
def add_team_member(team, user, role, actor):
    """Organizer-only assignment, including captains of legacy teams."""
    if not actor.is_active or not actor.is_staff or not actor.has_perm("competitions.add_teammembership"):
        raise PermissionDenied("Нет права добавлять участников команды.")
    validate_member_add(team, user, role)
    membership = TeamMembership.objects.create(team=team, user=user, role=role)
    sync_team_rosters(team)
    return membership


def validate_member_add(team, user, role):
    require_mutable_team(team)
    if role not in ("captain", "member"):
        raise ValidationError("Неизвестная роль.")
    if TeamMembership.objects.filter(team=team, user=user).exists():
        raise ValidationError("Пользователь уже состоит в команде.")
    if role == "captain" and team.memberships.filter(role="captain").exists():
        raise ValidationError("У команды уже есть капитан.")
    from .models import PlayerParticipation
    competition_ids = team.participations.filter(is_active=True).exclude(competition__status="finished").values_list("competition_id", flat=True)
    if PlayerParticipation.objects.filter(competition_id__in=competition_ids, user=user).exclude(participation__team=team).exists():
        raise ValidationError("Игрок уже представляет другую команду в этом соревновании.")
