from django.contrib import admin, messages

from .models import Competition, Team, Service, TeamService
from .services import (
    start_competition,
    deploy_competition_services )
from .infrastructure import (
    stop_team_service,
    restart_team_service,
)


@admin.action(description="Запустить соревнование")
def start_competitions(modeladmin, request, queryset):

    for competition in queryset:
        try:
            start_competition(competition)

            messages.success(
                request,
                f'Соревнование "{competition.name}" запущено.'
            )

        except Exception as error:
            messages.error(
                request,
                f'{competition.name}: {error}'
            )


@admin.action(description="Развернуть инфраструктуру")
def deploy_infrastructure(modeladmin, request, queryset):

    for competition in queryset:
        try:
            deploy_competition_services(competition)

            messages.success(
                request,
                f'Инфраструктура "{competition.name}" развернута.'
            )

        except Exception as error:
            messages.error(
                request,
                f'{competition.name}: {error}'
            )


@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):

    list_display = (
        "name",
        "status",
    )

    list_filter = (
        "status",
    )

    actions = [
        start_competitions,
        deploy_infrastructure,
    ]


admin.site.register(Team)
admin.site.register(Service)
@admin.action(description="Остановить выбранные сервисы")
def stop_services(modeladmin, request, queryset):

    for team_service in queryset:
        try:
            stop_team_service(team_service)

            messages.success(
                request,
                f"{team_service}: остановлен"
            )

        except Exception as error:
            messages.error(
                request,
                f"{team_service}: {error}"
            )


@admin.action(description="Запустить выбранные сервисы")
def restart_services(modeladmin, request, queryset):

    for team_service in queryset:
        try:
            restart_team_service(team_service)

            messages.success(
                request,
                f"{team_service}: запущен"
            )

        except Exception as error:
            messages.error(
                request,
                f"{team_service}: {error}"
            )
@admin.register(TeamService)
class TeamServiceAdmin(admin.ModelAdmin):

    list_display = (
        "team",
        "service",
        "status",
        "host_port",
    )

    list_filter = (
        "status",
        "service",
    )

    actions = [
        stop_services,
        restart_services,
    ]