"""Transport-neutral queue operations. No network calls in this module."""
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .checker_http import HTTPResult
from .checker_secrets import seal
from .models import CheckerEndpoint, CheckerJob, CheckerRun, FlagMaterial, Round
from .mutation import serialized_mutation

LEASE_SECONDS = 20
CHECK_GRACE_SECONDS = 60
TERMINAL = ("done", "expired")
SAFE_CODES = {"", "unreachable", "response", "authentication", "identity", "timeout", "transport", "checker", "worker_exit", "endpoint", "secret", "expired", "lease_exhausted", "put_failed"}


def _new_job(flag, endpoint, operation, deadline):
    return CheckerJob.objects.create(
        flag=flag, endpoint=endpoint, instance_id=endpoint.instance_id,
        port=endpoint.port, container_id=endpoint.container_id,
        operation=operation, deadline=deadline,
    )


@sensitive_variables()
def queue_install(flag, value):
    """Called by open_round while holding the mutation transaction."""
    service = flag.team_service
    endpoint = CheckerEndpoint.objects.filter(team_service=service).first()
    if (service.service.runtime != "training_http" or endpoint is None or not endpoint.container_id
            or endpoint.container_id != service.container_id or endpoint.port != service.host_port):
        raise ValidationError("Для HTTP-раунда нужны подготовленные экземпляры учебного сервиса.")
    FlagMaterial.objects.create(flag=flag, ciphertext=seal(value, f"flag:{flag.pk}"), expires_at=flag.round.expires_at)
    CheckerRun.objects.create(flag=flag, put_status="")
    _new_job(flag, endpoint, "put_flag", flag.round.expires_at)


def _terminal(job, result, code, now, state="done"):
    job.state, job.result, job.error_code, job.finished_at = state, result, code, now
    job.save(update_fields=["state", "result", "error_code", "finished_at"])
    if job.operation == "put_flag":
        flag = job.flag
        flag.state = "installed" if result == "OK" else "rejected"
        flag.save(update_fields=["state"])
        CheckerRun.objects.filter(flag=flag).update(put_status=result)
        FlagMaterial.objects.filter(flag=flag).delete()


def _expire_jobs(now):
    for job in CheckerJob.objects.filter(state__in=["pending", "running"]).select_related("flag"):
        if job.deadline <= now:
            _terminal(job, "ERROR", "expired", now, "expired")
        elif job.state == "running" and job.lease_until <= now and job.attempts >= 3:
            _terminal(job, "ERROR", "lease_exhausted", now)
    FlagMaterial.objects.filter(expires_at__lte=now).delete()


def begin_checks(game_round, now=None):
    """No HTTP, never cancels an active PUT to allow an early finish."""
    now = now or timezone.now()
    if game_round.state != "open":
        raise ValidationError("Раунд уже завершён или проверки уже запущены.")
    if CheckerJob.objects.filter(flag__round=game_round, operation="put_flag").exclude(state__in=TERMINAL).exists():
        raise ValidationError("Установка флагов ещё не завершена. Дождитесь worker или дедлайна.")
    deadline = min(now + timedelta(seconds=CHECK_GRACE_SECONDS), game_round.expires_at + timedelta(seconds=CHECK_GRACE_SECONDS))
    for flag in game_round.flags.select_related("checker_run"):
        put = flag.jobs.get(operation="put_flag")
        get = _new_job(flag, put.endpoint, "get_flag", deadline)
        health = _new_job(flag, put.endpoint, "check_service", deadline)
        # Preserve the same endpoint identity that was used to install this flag.
        for job in (get, health):
            job.instance_id, job.port, job.container_id = put.instance_id, put.port, put.container_id
            job.save(update_fields=["instance_id", "port", "container_id"])
        if flag.checker_run.put_status != "OK":
            _terminal(get, "ERROR", "put_failed", now)
    game_round.state = "checking"
    game_round.save(update_fields=["state"])


def _finalize_ready(game_round, now):
    jobs = CheckerJob.objects.filter(flag__round=game_round)
    if jobs.exclude(state__in=TERMINAL).exists():
        return False
    flags = list(game_round.flags.all())
    if jobs.count() != len(flags) * 3:
        raise ValidationError("Неполный набор заданий: требуется проверка организатора.")
    for flag in flags:
        results = {job.operation: job.result for job in flag.jobs.all()}
        CheckerRun.objects.filter(flag=flag).update(
            get_status=results["get_flag"], service_status=results["check_service"], checked_at=now,
        )
    from .game_services import finalize_scores
    finalize_scores(game_round, now)
    return True


def _maintenance(now):
    _expire_jobs(now)
    active = Round.objects.filter(checker_backend="http", competition__status="running").exclude(state="finished")
    for game_round in active:
        if game_round.state == "open" and game_round.expires_at <= now:
            begin_checks(game_round, now)
    _expire_jobs(now)  # A worker resumed after the grace period must not send HTTP.
    for game_round in active.filter(state="checking"):
        _finalize_ready(game_round, now)


@serialized_mutation(durable=True)
def maintain_jobs():
    _maintenance(timezone.now())


@dataclass(frozen=True)
class Claim:
    job_id: uuid.UUID
    lease: uuid.UUID


@serialized_mutation(durable=True)
def claim_job():
    now = timezone.now()
    _maintenance(now)
    for job in CheckerJob.objects.filter(state__in=["pending", "running"], deadline__gt=now).select_related("flag__round__competition").order_by("created_at", "pk"):
        game_round = job.flag.round
        if game_round.competition.status != "running" or game_round.competition.launch_state != "idle":
            continue
        required = "open" if job.operation == "put_flag" else "checking"
        if game_round.state != required or (job.state == "running" and job.lease_until > now):
            continue
        if job.attempts >= 3:
            continue
        job.attempts += 1
        job.state, job.lease = "running", uuid.uuid4()
        job.lease_until = min(now + timedelta(seconds=LEASE_SECONDS), job.deadline)
        job.started_at = now
        job.save(update_fields=["attempts", "state", "lease", "lease_until", "started_at"])
        return Claim(job.pk, job.lease)
    return None


@serialized_mutation(durable=True)
def record_result(claim, result):
    now = timezone.now()
    job = CheckerJob.objects.select_related("flag__round__competition", "endpoint", "flag__team_service").get(pk=claim.job_id)
    game_round = job.flag.round
    required = "open" if job.operation == "put_flag" else "checking"
    if (job.state != "running" or job.lease != claim.lease or job.lease_until <= now
            or job.deadline <= now or game_round.state != required
            or game_round.competition.status != "running" or game_round.competition.launch_state != "idle"):
        return False
    if not isinstance(result, HTTPResult) or result.status not in ("OK", "DOWN", "MUMBLE", "ERROR") or result.code not in SAFE_CODES:
        result = HTTPResult("ERROR", "checker")
    service, endpoint = job.flag.team_service, job.endpoint
    if (endpoint.team_service_id != service.pk or endpoint.instance_id != job.instance_id
            or endpoint.port != job.port or service.host_port != job.port
            or endpoint.container_id != job.container_id or service.container_id != job.container_id):
        result = HTTPResult("ERROR", "endpoint")
    if result.retryable and job.attempts < 3 and now + timedelta(seconds=2) < job.deadline:
        job.state, job.lease, job.lease_until, job.error_code = "pending", None, None, result.code
        job.save(update_fields=["state", "lease", "lease_until", "error_code"])
    else:
        _terminal(job, result.status, result.code, now)
    if game_round.state == "checking":
        _finalize_ready(game_round, now)
    return True
