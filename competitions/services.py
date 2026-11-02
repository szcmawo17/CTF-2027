import logging

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from .models import Competition, TeamService
from .mutation import serialized_mutation
from .roster_services import sync_participation_roster, validate_start_rosters

from .infrastructure import (
    deploy_team_service,
    stop_team_service,
    sync_team_service_status,
)

logger = logging.getLogger(__name__)

def create_team_services(competition):
    """
    Создаёт записи сервисов для каждой команды.
    """

    for participation in competition.participations.filter(is_active=True):
        for service in competition.services.all():

            TeamService.objects.get_or_create(
                participation=participation,
                service=service
            )


def deploy_competition_services(competition):
    """Разворачивает сервисы с попыткой отката при ошибке."""

    if not competition.participations.filter(is_active=True).exists():
        raise ValueError("У соревнования нет команд.")

    if not competition.services.exists():
        raise ValueError("У соревнования нет сервисов.")

    create_team_services(competition)

    team_services = list(TeamService.objects.filter(
        participation__competition=competition,
        participation__is_active=True,
        service__in=competition.services.all()
    ))

    # До запуска запоминаем реальное состояние всех сервисов.
    initial_statuses = {
        service.pk: sync_team_service_status(service)
        for service in team_services
    }

    attempted = []

    try:
        for service in team_services:
            attempted.append(service)
            deploy_team_service(service)

    except Exception as error:
        rollback_errors = []

        for service in reversed(attempted):
            # Не трогаем контейнеры, работавшие до операции.
            if initial_statuses[service.pk] == "running":
                continue

            if service.container_id:
                try:
                    stop_team_service(service)
                except Exception as rollback_error:
                    rollback_errors.append(
                        f"{service.pk}: {rollback_error}"
                    )

        if rollback_errors:
            raise RuntimeError(
                "Развёртывание завершилось ошибкой. "
                "Откат выполнен не полностью: "
                + "; ".join(rollback_errors)
            ) from error

        raise


def require_organizer(actor):
    if actor is None or not actor.is_active or not actor.is_staff or not actor.has_perm("competitions.change_competition"):
        raise PermissionDenied("Требуется право организатора на изменение соревнований.")


@serialized_mutation
def open_registration(competition, actor):
    require_organizer(actor)
    current = Competition.objects.get(pk=competition.pk)
    if current.status != "draft":
        raise ValidationError("Открыть регистрацию можно только из подготовки.")
    for participation in current.participations.filter(is_active=True).select_related("team", "competition"):
        sync_participation_roster(participation)
    current.status = "registration"
    current.save(update_fields=["status"])
    competition.status = current.status
    return current


@serialized_mutation
def _prepare_start(competition):
    current = Competition.objects.get(pk=competition.pk)
    if current.status != "registration":
        raise ValidationError("Запуск разрешён только после открытия регистрации.")
    if current.launch_state == "deploying":
        raise ValidationError("Запуск уже выполняется. Повторный запуск запрещён.")
    if current.roster_frozen_at is None:
        validate_start_rosters(current)
        current.roster_frozen_at = timezone.now()
    # A failed attempt retries the same frozen roster; registration stays closed.
    current.launch_state = "deploying"
    current.save(update_fields=["roster_frozen_at", "launch_state"])
    return current


@serialized_mutation
def _record_launch_result(competition, successful):
    current = Competition.objects.get(pk=competition.pk)
    if current.status != "registration" or current.launch_state != "deploying":
        raise ValidationError("Состояние запуска изменилось; нужна проверка организатора.")
    current.launch_state = "idle" if successful else "failed"
    if successful:
        current.status = "running"
    current.save(update_fields=["launch_state", "status"])
    return current


def start_competition(competition, actor=None):
    """Commit the registration barrier and roster BEFORE any external operation."""
    require_organizer(actor)
    current = _prepare_start(competition)
    try:
        deploy_competition_services(current)
    except Exception as error:
        _record_launch_result(current, successful=False)
        logger.exception("Competition deployment failed: %s", current.pk)
        raise ValidationError(
            "Развёртывание завершилось ошибкой. Соревнование не запущено, "
            "регистрация и состав остаются закрыты. Устраните причину и повторите запуск."
        ) from error
    current = _record_launch_result(current, successful=True)
    competition.status = current.status
    return current


@serialized_mutation
def finish_competition(competition, actor):
    require_organizer(actor)
    current = Competition.objects.get(pk=competition.pk)
    if current.status != "running":
        raise ValidationError("Завершить можно только идущее соревнование.")
    current.status = "finished"
    current.save(update_fields=["status"])
    competition.status = current.status
    return current
