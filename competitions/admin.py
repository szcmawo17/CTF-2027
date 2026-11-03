from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.decorators import method_decorator
from django.views.decorators.http import require_http_methods

from .models import (
    Competition, CompetitionParticipation, PlayerParticipation, Team,
    TeamInvitation, TeamMembership, Service, TeamService,
    CompetitionLaunchAttempt, CompetitionRecoveryAction,
)
from .mutation import mutation_transaction
from .services import open_registration, start_competition, finish_competition
from .team_services import add_team_member, validate_member_add
from .infrastructure import stop_team_service, restart_team_service
from .services import require_organizer
from .launch_recovery import diagnose_launch, request_manual_review, MANUAL_INSTRUCTION


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


@admin.action(description="Запустить соревнование", permissions=["change"])
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
    list_display = ("name", "status", "launch_state", "current_launch_attempt", "launch_tools")
    list_filter = ("status", "launch_state")
    actions = [open_registrations, start_competitions, finish_competitions]

    def get_readonly_fields(self, request, obj=None):
        fields = ("status", "launch_state", "roster_frozen_at", "roster_verified", "current_launch_attempt", "launch_tools")
        if obj and (obj.roster_frozen_at or obj.status in ("running", "finished")):
            fields += ("services",)
        return fields

    @admin.display(description="Запуск и диагностика")
    def launch_tools(self, obj):
        if not obj or not obj.pk:
            return "Сначала сохраните соревнование."
        return format_html('<a href="{}">История и диагностика</a>', reverse(
            "admin:competitions_launch_diagnostics", args=[obj.pk]
        ))

    def get_urls(self):
        return [path(
            "<int:competition_id>/launch-diagnostics/",
            self.admin_site.admin_view(self.launch_diagnostics),
            name="competitions_launch_diagnostics",
        )] + super().get_urls()

    @method_decorator(require_http_methods(["GET", "POST"]))
    def launch_diagnostics(self, request, competition_id):
        require_organizer(request.user)
        competition = get_object_or_404(Competition, pk=competition_id)
        report, token = None, None
        if request.method == "POST":
            try:
                if request.POST.get("operation") == "diagnose":
                    report, token = diagnose_launch(competition, request.user)
                elif request.POST.get("operation") == "manual_review":
                    request_manual_review(competition, request.user, request.POST.get("diagnosis_token", ""))
                    messages.success(request, "Ручное вмешательство зафиксировано. Повторный запуск остаётся заблокирован.")
                    return redirect("admin:competitions_launch_diagnostics", competition_id=competition.pk)
                else:
                    raise ValidationError("Неизвестное действие.")
            except ValidationError as error:
                messages.error(request, " ".join(error.messages))
        competition.refresh_from_db()
        return TemplateResponse(request, "admin/competitions/launch_diagnostics.html", {
            **self.admin_site.each_context(request), "opts": self.model._meta,
            "title": "История запуска и диагностика", "competition": competition,
            "attempts": competition.launch_attempts.select_related("initiated_by"),
            "recovery_actions": competition.recovery_actions.select_related("initiated_by"),
            "report": report, "diagnosis_token": token, "instruction": MANUAL_INSTRUCTION,
            "can_request_review": (
                report is not None and report["database_unchanged"]
                and competition.status == "registration"
                and competition.launch_state in ("deploying", "failed", "review")
                and not competition.recovery_actions.filter(
                    attempt_id=competition.current_launch_attempt_id
                ).exists()
            ),
        })


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


class OrganizerHistoryAdmin(ReadOnlyAdmin):
    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_staff and request.user.has_perm("competitions.change_competition")


@admin.register(CompetitionLaunchAttempt)
class CompetitionLaunchAttemptAdmin(OrganizerHistoryAdmin):
    list_display = ("id", "competition", "initiated_by", "started_at", "finished_at", "state")
    list_filter = ("state", "competition")


@admin.register(CompetitionRecoveryAction)
class CompetitionRecoveryActionAdmin(OrganizerHistoryAdmin):
    list_display = ("id", "competition", "attempt", "initiated_by", "created_at", "action")
    list_filter = ("competition",)


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
        if team_service.participation.competition.launch_state in ("deploying", "failed", "review"):
            messages.error(request, "Операции с сервисами заблокированы: запуск выполняется или требует ручной проверки.")
            continue
        try:
            operation(team_service)
        except Exception:
            messages.error(request, "Операция с сервисом не выполнена. Требуется диагностика.")
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
