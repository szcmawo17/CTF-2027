from django.core.exceptions import ValidationError

from .models import Competition, CompetitionParticipation
from .team_services import require_captain
from .mutation import serialized_mutation
from .roster_services import sync_participation_roster


@serialized_mutation
def register_team(competition, team, actor):
    require_captain(team, actor)
    current = Competition.objects.select_for_update().get(pk=competition.pk)
    if not current.registration_open:
        raise ValidationError("Регистрация на это соревнование закрыта.")

    participation, created = CompetitionParticipation.objects.get_or_create(
        competition=current, team=team
    )
    if not created and participation.is_active:
        raise ValidationError("Команда уже зарегистрирована на соревнование.")
    participation.is_active = True
    participation.save(update_fields=["is_active"])
    sync_participation_roster(participation)
    return participation


@serialized_mutation
def withdraw_team(competition, team, actor):
    require_captain(team, actor)
    current = Competition.objects.select_for_update().get(pk=competition.pk)
    if not current.registration_open:
        raise ValidationError("После закрытия регистрации выйти нельзя.")

    participation = CompetitionParticipation.objects.filter(
        competition=current, team=team, is_active=True
    ).first()
    if participation is None:
        raise ValidationError("Команда не зарегистрирована на соревнование.")
    if participation.services.exists():
        raise ValidationError("Нельзя выйти: для команды уже созданы сервисы.")

    participation.is_active = False
    participation.save(update_fields=["is_active"])
    sync_participation_roster(participation)
