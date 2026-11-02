from django.core.exceptions import ValidationError

from .models import CompetitionParticipation, PlayerParticipation, TeamMembership


def team_roster_locked(team):
    participations = team.participations.filter(is_active=True).exclude(
        competition__status="finished"
    )
    return (
        participations.filter(competition__roster_frozen_at__isnull=False).exists()
        or participations.filter(competition__status="running").exists()
    )


def require_mutable_team(team):
    if team_roster_locked(team):
        raise ValidationError("Состав и капитан команды зафиксированы до завершения соревнования.")


def sync_participation_roster(participation):
    """Caller holds mutation_transaction. Frozen/finished snapshots never change."""
    competition = participation.competition
    if competition.roster_frozen_at is not None or competition.status in ("running", "finished"):
        return
    members = list(participation.team.memberships.all()) if participation.is_active else []
    user_ids = [member.user_id for member in members]
    conflict = PlayerParticipation.objects.filter(
        competition_id=participation.competition_id, user_id__in=user_ids
    ).exclude(participation=participation).select_related("user", "participation__team").first()
    if conflict:
        raise ValidationError(
            f"Игрок {conflict.user.username} уже представляет команду "
            f"{conflict.participation.team.name} в этом соревновании."
        )
    participation.players.exclude(user_id__in=user_ids).delete()
    for member in members:
        PlayerParticipation.objects.update_or_create(
            competition_id=participation.competition_id, user_id=member.user_id,
            defaults={"participation": participation, "role": member.role},
        )


def sync_team_rosters(team):
    for participation in CompetitionParticipation.objects.filter(
        team=team, is_active=True, competition__roster_frozen_at__isnull=True,
        competition__status__in=("draft", "registration"),
    ).select_related("competition", "team"):
        sync_participation_roster(participation)


def validate_start_rosters(competition):
    participations = list(competition.participations.filter(is_active=True).select_related("team"))
    if not participations:
        raise ValidationError("У соревнования нет зарегистрированных команд.")
    if not competition.services.exists():
        raise ValidationError("У соревнования нет сервисов.")
    for participation in participations:
        members = TeamMembership.objects.filter(team=participation.team)
        if not members.exists() or members.filter(role="captain").count() != 1:
            raise ValidationError(f"Для команды {participation.team.name} нужны участники и один капитан.")
        if members.filter(user__is_active=False).exists():
            raise ValidationError(f"В команде {participation.team.name} есть неактивный пользователь.")
        sync_participation_roster(participation)
