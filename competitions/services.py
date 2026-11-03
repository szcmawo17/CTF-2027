import logging

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Competition, CompetitionLaunchAttempt, TeamService
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


@serialized_mutation(durable=True)
def _prepare_start(competition, actor):
    require_organizer(actor)
    current = Competition.objects.get(pk=competition.pk)
    if current.status != "registration":
        raise ValidationError("Запуск разрешён только после открытия регистрации.")
    if current.launch_state != "idle" or current.launch_attempts.filter(
        state__in=("in_progress", "review")
    ).exists():
        raise ValidationError(
            "Запуск уже выполняется или требует проверки. Откройте диагностику в Admin."
        )

    attempt = CompetitionLaunchAttempt.objects.create(
        competition=current, initiated_by=actor,
        message="Проверка условий запуска.",
    )
    try:
        # A failed roster validation must not commit a partially updated roster.
        with transaction.atomic():
            validate_start_rosters(current)
    except ValidationError:
        attempt.state = "failed"
        attempt.finished_at = timezone.now()
        attempt.message = (
            "Условия запуска не выполнены. Проверьте наличие сервисов, команд, "
            "активных участников и одного капитана в каждой команде."
        )
        attempt.save(update_fields=["state", "finished_at", "message"])
        current.current_launch_attempt = attempt
        current.save(update_fields=["current_launch_attempt"])
        return current, attempt, False

    current.roster_frozen_at = current.roster_frozen_at or timezone.now()
    current.launch_state = "deploying"
    current.current_launch_attempt = attempt
    current.save(update_fields=["roster_frozen_at", "launch_state", "current_launch_attempt"])
    attempt.message = "Состав зафиксирован. Начато развёртывание."
    attempt.save(update_fields=["message"])
    return current, attempt, True


@serialized_mutation
def _record_launch_result(competition, attempt_id):
    current = Competition.objects.get(pk=competition.pk)
    attempt = CompetitionLaunchAttempt.objects.get(pk=attempt_id, competition=current)
    if (
        current.current_launch_attempt_id != attempt.pk
        or current.status != "registration"
        or current.launch_state != "deploying"
        or attempt.state != "in_progress"
    ):
        raise ValidationError("Результат устаревшей или уже обработанной попытки отклонён.")
    attempt.state = "succeeded"
    attempt.finished_at = timezone.now()
    attempt.message = "Развёртывание завершено; результат сохранён."
    attempt.save(update_fields=["state", "finished_at", "message"])
    current.launch_state = "idle"
    current.status = "running"
    current.save(update_fields=["launch_state", "status"])
    return current


@serialized_mutation
def _mark_launch_uncertain(competition, attempt_id):
    current = Competition.objects.get(pk=competition.pk)
    if current.current_launch_attempt_id != attempt_id:
        return
    attempt = CompetitionLaunchAttempt.objects.get(pk=attempt_id, competition=current)
    if attempt.state != "in_progress" or current.launch_state != "deploying":
        return
    attempt.state = "review"
    attempt.finished_at = timezone.now()
    attempt.message = (
        "Исход развёртывания или запись результата не подтверждены. "
        "Нужна диагностика; повторный запуск заблокирован."
    )
    attempt.save(update_fields=["state", "finished_at", "message"])
    current.launch_state = "review"
    current.save(update_fields=["launch_state"])


def start_competition(competition, actor=None):
    """Commit attempt + frozen roster before Docker; never claim DB/Docker atomicity."""
    require_organizer(actor)
    current, attempt, ready = _prepare_start(competition, actor)
    if not ready:
        raise ValidationError(attempt.message)
    try:
        deploy_competition_services(current)
        current = _record_launch_result(current, attempt.pk)
    except Exception:
        # No raw exception text/traceback: SDK errors may contain credentials.
        logger.error("Launch outcome uncertain: competition=%s attempt=%s", current.pk, attempt.pk)
        try:
            _mark_launch_uncertain(current, attempt.pk)
        except Exception:
            # The committed in_progress attempt remains visible even if DB is unavailable.
            logger.error("Cannot record launch review marker: attempt=%s", attempt.pk)
        raise ValidationError(
            "Запуск требует ручной проверки. Откройте диагностику в Admin. "
            "Не повторяйте развёртывание до проверки процесса и контейнеров."
        ) from None
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
