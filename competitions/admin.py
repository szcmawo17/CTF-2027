from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseRedirect

from .models import (
    Competition, CompetitionParticipation, PlayerParticipation, Team,
    TeamInvitation, TeamMembership, Service, TeamService,
)
from .mutation import mutation_transaction
from .services import open_registration, start_competition, finish_competition
from .team_services import add_team_member, validate_member_add
from .infrastructure import stop_team_service, restart_team_service


def run_transition(request, queryset, operation):
    for competition in queryset:
        try:
            result = operation(competition, request.user)
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        else:
            messages.success(request, f"{result.name}: {result.get_status_display()}")


@admin.action(description="Открыть регистрацию", permissions=["change"])
def open_registrations(modeladmin, request, queryset):
    run_transition(request, queryset, open_registration)


@admin.action(description="Запустить соревнование / повторить неудачный запуск", permissions=["change"])
def start_competitions(modeladmin, request, queryset):
    run_transition(request, queryset, start_competition)


@admin.action(description="Завершить соревнование (без остановки контейнеров)", permissions=["change"])
def finish_competitions(modeladmin, request, queryset):
    run_transition(request, queryset, finish_competition)


class SerializedAdmin(admin.ModelAdmin):
    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        if request.method == "POST":
            try:
                with mutation_transaction():
                    return super().changeform_view(request, object_id, form_url, extra_context)
            except ValidationError as error:
                # Catch after rollback, including SQLite's busy-writer error.
                messages.error(request, " ".join(error.messages))
                return HttpResponseRedirect(request.path)
        return super().changeform_view(request, object_id, form_url, extra_context)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Competition)
class CompetitionAdmin(SerializedAdmin):
    list_display = ("name", "status", "launch_state")
    list_filter = ("status", "launch_state")
    actions = [open_registrations, start_competitions, finish_competitions]

    def get_readonly_fields(self, request, obj=None):
        fields = ("status", "launch_state", "roster_frozen_at", "roster_verified")
        if obj and (obj.roster_frozen_at or obj.status in ("running", "finished")):
            fields += ("services",)
        return fields


@admin.register(Team)
class TeamAdmin(SerializedAdmin):
    pass


@admin.register(Service)
class ServiceAdmin(SerializedAdmin):
    pass


class MembershipAdminForm(forms.ModelForm):
    class Meta:
        model = TeamMembership
        fields = ("team", "user", "role")

    def clean(self):
        data = super().clean()
        if all(data.get(field) for field in ("team", "user", "role")):
            validate_member_add(data["team"], data["user"], data["role"])
        return data


@admin.register(TeamMembership)
class TeamMembershipAdmin(SerializedAdmin):
    form = MembershipAdminForm
    list_display = ("team", "user", "role")
    list_filter = ("role",)

    def has_change_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if change:
            raise PermissionDenied("Используйте действия капитана для изменения состава.")
        saved = add_team_member(obj.team, obj.user, obj.role, request.user)
        obj.pk = saved.pk
        obj._state = saved._state


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TeamInvitation)
class TeamInvitationAdmin(ReadOnlyAdmin):
    list_display = ("team", "invitee", "invited_by", "status", "created_at")
    list_filter = ("status",)


@admin.register(CompetitionParticipation)
class CompetitionParticipationAdmin(ReadOnlyAdmin):
    list_display = ("team", "competition", "is_active")
    list_filter = ("competition", "is_active")


@admin.register(PlayerParticipation)
class PlayerParticipationAdmin(ReadOnlyAdmin):
    list_display = ("competition", "participation", "user", "role")
    list_filter = ("competition",)


def operate_services(request, queryset, operation):
    if not request.user.is_staff or not request.user.has_perm("competitions.change_teamservice"):
        raise PermissionDenied("Нет права управления инфраструктурой.")
    for team_service in queryset:
        try:
            operation(team_service)
        except Exception as error:
            messages.error(request, f"{team_service}: {error}")
        else:
            messages.success(request, f"{team_service}: операция выполнена.")


@admin.action(description="Остановить выбранные сервисы", permissions=["operate"])
def stop_services(modeladmin, request, queryset):
    operate_services(request, queryset, stop_team_service)


@admin.action(description="Запустить выбранные сервисы", permissions=["operate"])
def restart_services(modeladmin, request, queryset):
    operate_services(request, queryset, restart_team_service)


@admin.register(TeamService)
class TeamServiceAdmin(ReadOnlyAdmin):
    list_display = ("participation", "service", "status", "host_port")
    list_filter = ("status", "service")
    actions = [stop_services, restart_services]

    def has_operate_permission(self, request):
        return request.user.is_staff and request.user.has_perm("competitions.change_teamservice")
