from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.deletion import ProtectedError

from .models import Competition, CompetitionParticipation
from .team_services import require_captain


@transaction.atomic
def register_team(competition, team, actor):
    require_captain(team, actor)
    current = Competition.objects.select_for_update().get(pk=competition.pk)
    if current.status != "registration":
        raise ValidationError("Регистрация на это соревнование закрыта.")

    participation, created = CompetitionParticipation.objects.get_or_create(
        competition=current, team=team
    )
    if not created:
        raise ValidationError("Команда уже зарегистрирована на соревнование.")
    return participation


@transaction.atomic
def withdraw_team(competition, team, actor):
    require_captain(team, actor)
    current = Competition.objects.select_for_update().get(pk=competition.pk)
    if current.status != "registration":
        raise ValidationError("После закрытия регистрации выйти нельзя.")

    participation = CompetitionParticipation.objects.filter(
        competition=current, team=team
    ).first()
    if participation is None:
        raise ValidationError("Команда не зарегистрирована на соревнование.")
    if participation.services.exists():
        raise ValidationError("Нельзя выйти: для команды уже созданы сервисы.")

    try:
        participation.delete()
    except ProtectedError as error:
        raise ValidationError("Нельзя выйти: для команды уже созданы сервисы.") from error
