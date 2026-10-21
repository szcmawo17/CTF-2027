from .models import TeamService
from .infrastructure import deploy_team_service


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
    """
    Разворачивает инфраструктуру соревнования.
    """

    if not competition.teams.exists():
        raise ValueError("У соревнования нет команд.")

    if not competition.services.exists():
        raise ValueError("У соревнования нет сервисов.")

    # Создаём недостающие записи
    create_team_services(competition)

    # Получаем сервисы всех команд соревнования
    team_services = TeamService.objects.filter(
        team__competition=competition,
        service__in=competition.services.all()
    )

    for team_service in team_services:
        deploy_team_service(team_service)


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