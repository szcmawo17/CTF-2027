from .models import TeamService
from .infrastructure import (
    deploy_team_service,
    stop_team_service,
    sync_team_service_status,
)
def create_team_services(competition):
    """
    Создаёт записи сервисов для каждой команды.
    """

    for team in competition.teams.all():
        for service in competition.services.all():

            TeamService.objects.get_or_create(
                team=team,
                service=service
            )


def deploy_competition_services(competition):
    """Разворачивает сервисы с попыткой отката при ошибке."""

    if not competition.teams.exists():
        raise ValueError("У соревнования нет команд.")

    if not competition.services.exists():
        raise ValueError("У соревнования нет сервисов.")

    create_team_services(competition)

    team_services = list(TeamService.objects.filter(
        team__competition=competition,
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

def start_competition(competition):
    """
    Запускает соревнование вместе с инфраструктурой.
    """

    if competition.status != "draft":
        raise ValueError(
            "Соревнование уже запущено или завершено."
        )

    # Сначала разворачиваем инфраструктуру
    deploy_competition_services(competition)

    # Меняем статус только после успешного развёртывания
    competition.status = "running"
    competition.save(update_fields=["status"])