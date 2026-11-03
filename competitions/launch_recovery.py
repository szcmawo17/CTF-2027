"""Read-only diagnosis and a conservative, audited manual-review action."""
import hashlib
import json
import uuid

import docker
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Competition, CompetitionRecoveryAction, TeamService
from .mutation import serialized_mutation
from .services import require_organizer


TOKEN_SALT = "competitions.launch-diagnosis.v1"
TOKEN_MAX_AGE = 300
MANUAL_INSTRUCTION = (
    "Остановка исходного процесса не подтверждена. Не запускайте соревнование повторно. "
    "Проверьте и при необходимости остановите исходный worker вне приложения, "
    "затем сверьте контейнеры, включая возможные контейнеры без записи в БД. "
    "Сброс запуска через Admin пока не поддерживается."
)
CONTAINER_STATES = {"created", "running", "paused", "restarting", "removing", "exited", "dead"}
DB_SERVICE_STATES = CONTAINER_STATES | {"stopped", "missing"}


def _iso(value):
    return value.isoformat() if value else None


def _snapshot(competition_id):
    # Short read transaction only; never held during Docker inspection.
    with transaction.atomic():
        current = Competition.objects.select_related("current_launch_attempt").get(pk=competition_id)
        attempt = current.current_launch_attempt
        teams = [
            {
                "participation_id": participation.pk, "team_id": participation.team_id,
                "active": participation.is_active,
                "players": list(participation.players.order_by("user_id").values(
                    "user_id", "role"
                )),
            }
            for participation in current.participations.order_by("pk").prefetch_related("players")
        ]
        services = list(TeamService.objects.filter(
            participation__competition=current
        ).order_by("pk").values("id", "participation_id", "service_id", "container_id", "host_port", "status"))
        for service in services:
            if service["status"] not in DB_SERVICE_STATES:
                service["status"] = "unknown"
        return {
            "competition_id": current.pk, "status": current.status,
            "launch_state": current.launch_state,
            "roster_frozen_at": _iso(current.roster_frozen_at),
            "roster_verified": current.roster_verified,
            "attempt": {
                "id": str(attempt.pk), "competition_id": attempt.competition_id,
                "state": attempt.state, "started_at": _iso(attempt.started_at),
                "finished_at": _iso(attempt.finished_at),
            } if attempt else None,
            "teams": teams, "services": services,
            "configured_services": list(current.services.order_by("pk").values_list("pk", flat=True)),
        }


def _fingerprint(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def _inspect_services(services):
    """Only GET container inspection. Never call sync_team_service_status()."""
    client = None
    connection_failed = False
    observations = []
    try:
        for service in services:
            observed = {"team_service_id": service["id"], "state": "unassigned", "localhost_port_matches": None}
            if service["container_id"]:
                try:
                    if connection_failed:
                        raise RuntimeError("unavailable")
                    if client is None:
                        try:
                            client = docker.from_env(timeout=5)
                        except Exception:
                            connection_failed = True
                            raise
                    container = client.containers.get(service["container_id"])
                    state = container.status
                    observed["state"] = state if state in CONTAINER_STATES else "unknown"
                    bindings = (container.attrs.get("NetworkSettings", {}).get("Ports") or {}).get("5000/tcp") or []
                    observed["localhost_port_matches"] = bool(bindings) and all(
                        binding.get("HostIp") == "127.0.0.1"
                        and str(binding.get("HostPort")) == str(service["host_port"])
                        for binding in bindings
                    )
                except docker.errors.NotFound:
                    observed["state"] = "missing"
                except Exception:
                    # Do not copy exception text, environment, logs or full attrs.
                    observed["state"] = "unavailable"
            comparable = "stopped" if observed["state"] == "exited" else observed["state"]
            observed["status_matches"] = comparable == service["status"]
            observations.append(observed)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    return observations


def diagnose_launch(competition, actor):
    require_organizer(actor)
    before = _snapshot(competition.pk)
    observations = _inspect_services(before["services"])
    unchanged = before == _snapshot(competition.pk)
    expected = {
        (team["participation_id"], service_id)
        for team in before["teams"] if team["active"]
        for service_id in before["configured_services"]
    }
    actual = {(service["participation_id"], service["service_id"]) for service in before["services"]}
    report = {
        "id": str(uuid.uuid4()), "observed_at": timezone.now().isoformat(),
        "database": before, "fingerprint": _fingerprint(before),
        "database_unchanged": unchanged, "containers": observations,
        "missing_services": [list(pair) for pair in sorted(expected - actual)],
        "unexpected_services": [list(pair) for pair in sorted(actual - expected)],
        "process_stopped_proven": False, "instruction": MANUAL_INSTRUCTION,
    }
    token = signing.dumps({"actor_id": actor.pk, "report": report}, salt=TOKEN_SALT, compress=True)
    return report, token


@serialized_mutation
def request_manual_review(competition, actor, diagnosis_token):
    require_organizer(actor)
    try:
        payload = signing.loads(diagnosis_token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
    except (signing.BadSignature, TypeError):
        raise ValidationError("Диагностика недействительна или устарела. Выполните её заново.") from None
    if payload["actor_id"] != actor.pk:
        raise PermissionDenied("Диагностика выполнена другим организатором.")
    report = payload["report"]
    if report["database"]["competition_id"] != competition.pk:
        raise ValidationError("Диагностика относится к другому соревнованию.")
    if not report["database_unchanged"] or report["fingerprint"] != _fingerprint(_snapshot(competition.pk)):
        raise ValidationError("Состояние изменилось. Выполните диагностику заново.")
    current = Competition.objects.select_related("current_launch_attempt").get(pk=competition.pk)
    if current.status != "registration" or current.launch_state not in ("deploying", "failed", "review"):
        raise ValidationError("Этому соревнованию не требуется разбор незавершённого запуска.")
    attempt = current.current_launch_attempt
    if attempt and (attempt.competition_id != current.pk or attempt.state not in ("in_progress", "review")):
        raise ValidationError("Связь попытки с соревнованием требует ручной проверки.")
    if current.recovery_actions.filter(attempt=attempt).exists():
        raise ValidationError("Ручное вмешательство для этой попытки уже зафиксировано.")
    event = CompetitionRecoveryAction.objects.create(
        id=report["id"], competition=current, attempt=attempt, initiated_by=actor,
        message=MANUAL_INSTRUCTION, diagnosis=report,
    )
    if attempt:
        attempt.state = "review"
        attempt.message = MANUAL_INSTRUCTION
        # A manual flag is NOT evidence that a worker finished.
        attempt.save(update_fields=["state", "message"])
    current.launch_state = "review"
    current.save(update_fields=["launch_state"])
    return event
