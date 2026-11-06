import re
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .checkers import MockChecker, checked_call, flag_digest
from .models import (
    Capture, CheckerRun, Competition, Flag, PlayerParticipation, Round,
    ScoreEvent, SubmissionWindow, TeamService,
)
from .mutation import serialized_mutation
from .services import require_organizer

SUBMISSION_LIMIT = 30  # Per team per 60 seconds, shared by all its players.
REJECTED = "Флаг не принят."


def require_running(competition):
    if competition.status != "running" or competition.launch_state != "idle":
        raise ValidationError("Игровые действия доступны только в идущем соревновании.")


def player_for(competition, actor):
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("Требуется активный участник соревнования.")
    player = PlayerParticipation.objects.filter(
        competition=competition, user=actor, participation__competition=competition,
        participation__is_active=True,
    ).select_related("participation__team").first()
    if player is None:
        raise PermissionDenied("Вы не входите в зафиксированный состав этого соревнования.")
    return player


@sensitive_variables()
@serialized_mutation
def open_round(competition, actor, *, number, duration=300):
    require_organizer(actor)
    current = Competition.objects.get(pk=competition.pk)
    require_running(current)
    if not current.roster_frozen_at or not current.roster_verified:
        raise ValidationError("Для игры нужен подтверждённый зафиксированный состав.")
    if type(number) is not int or type(duration) is not int or not 30 <= duration <= 3600:
        raise ValidationError("Длительность раунда: от 30 до 3600 секунд; номер должен быть целым.")
    last = current.rounds.aggregate(number=Max("number"))["number"] or 0
    if number != last + 1 or current.rounds.filter(state="open").exists():
        raise ValidationError("Раунд уже открыт или номер устарел. Обновите страницу.")
    participations = list(current.participations.filter(is_active=True).prefetch_related("players"))
    if len(participations) < 2:
        raise ValidationError("Для игрового раунда нужны минимум две команды.")
    for participation in participations:
        players = list(participation.players.all())
        if (not players or sum(p.role == "captain" for p in players) != 1
                or any(p.competition_id != current.pk for p in players)):
            raise ValidationError("Зафиксированный состав команд неполон или противоречив.")
    service_ids = set(current.services.values_list("pk", flat=True))
    services = list(TeamService.objects.filter(participation__in=participations))
    expected = {(p.pk, s) for p in participations for s in service_ids}
    if not expected or {(s.participation_id, s.service_id) for s in services} != expected:
        raise ValidationError("Набор сервисов команд не соответствует соревнованию.")
    if len(services) > 256:
        raise ValidationError("Локальный mock-движок допускает до 256 сервисов команд в раунде.")
    now = timezone.now()
    game_round = Round.objects.create(
        competition=current, number=number, started_at=now,
        expires_at=now + timedelta(seconds=duration), state="open",
    )
    # Only the bounded DB simulator is allowed inside this short transaction.
    # External checkers must use a future worker protocol, never this call site.
    checker = MockChecker()
    for service in services:
        value = "CTF{" + secrets.token_hex(32) + "}"
        flag = Flag.objects.create(round=game_round, team_service=service, digest=flag_digest(value), state="issued")
        result = checked_call(checker.put_flag, service.pk, flag.pk, value)
        CheckerRun.objects.create(flag=flag, put_status=result)
        flag.state = "installed" if result == "OK" else "rejected"
        flag.save(update_fields=["state"])
    return game_round


@serialized_mutation
def finish_round(game_round, actor):
    require_organizer(actor)
    current = Round.objects.select_related("competition").get(pk=game_round.pk)
    require_running(current.competition)
    if current.state != "open":
        raise ValidationError("Завершённый раунд нельзя изменить или завершить повторно.")
    checker = MockChecker()
    now = timezone.now()
    for flag in current.flags.select_related("team_service", "checker_run"):
        run = flag.checker_run
        run.get_status = (
            checked_call(checker.get_flag, flag.team_service_id, flag.pk, flag.digest)
            if flag.state == "installed" else "ERROR"
        )
        run.service_status = checked_call(checker.check_service, flag.team_service_id)
        run.checked_at = now
        run.save(update_fields=["get_status", "service_status", "checked_at"])
        if run.get_status == "OK" and run.put_status == "OK" and not flag.captures.exists():
            ScoreEvent.objects.create(flag=flag, participation_id=flag.team_service.participation_id, kind="defense", points=10)
        if run.service_status == "OK":
            ScoreEvent.objects.create(flag=flag, participation_id=flag.team_service.participation_id, kind="availability", points=1)
    current.state = "finished"
    current.finished_at = now
    current.save(update_fields=["state", "finished_at"])
    return current


@dataclass(frozen=True)
class SubmissionResult:
    accepted: bool
    message: str
    limited: bool = False


@sensitive_variables()
@serialized_mutation
def submit_flag(competition, actor, value):
    current = Competition.objects.get(pk=competition.pk)
    player = player_for(current, actor)
    require_running(current)
    now = timezone.now()
    window, _ = SubmissionWindow.objects.get_or_create(
        participation=player.participation, defaults={"started_at": now},
    )
    if now >= window.started_at + timedelta(seconds=60):
        window.started_at, window.count = now, 0
    if window.count >= SUBMISSION_LIMIT:
        return SubmissionResult(False, "Лимит отправок команды: 30 за минуту. Повторите позже.", True)
    window.count += 1
    window.save(update_fields=["count", "started_at"])
    # Return ordinary rejection so malformed attempts also commit the limiter.
    if not isinstance(value, str) or not re.fullmatch(r"CTF\{[0-9a-f]{64}\}", value):
        return SubmissionResult(False, REJECTED)
    flag = Flag.objects.filter(
        digest=flag_digest(value), state="installed", checker_run__put_status="OK",
        round__competition=current, round__state="open", round__expires_at__gt=now,
        round__started_at__lte=now, team_service__participation__is_active=True,
    ).select_related("team_service").first()
    if (flag is None or flag.team_service.participation_id == player.participation_id
            or flag.captures.filter(attacker=player.participation).exists()):
        return SubmissionResult(False, REJECTED)
    capture = Capture.objects.create(flag=flag, attacker=player.participation, submitted_by=player)
    ScoreEvent.objects.create(flag=flag, participation=player.participation, capture=capture, kind="attack", points=10)
    return SubmissionResult(True, "Флаг принят: +10 очков атаки.")


def scoreboard(competition):
    """No mutable cached totals: the durable event journal is the source."""
    rows = {
        p.pk: {"team": p.team, "attack": 0, "defense": 0, "availability": 0, "total": 0}
        for p in competition.participations.select_related("team")
    }
    for event in ScoreEvent.objects.filter(flag__round__competition=competition).values("participation_id", "kind").annotate(total=Sum("points")):
        row = rows[event["participation_id"]]
        row[event["kind"]] = event["total"]
        row["total"] += event["total"]
    return sorted(rows.values(), key=lambda r: (-r["total"], r["team"].pk))


@transaction.atomic
def audit_scores(competition):
    """Read-only reconstruction from captures and completed checker results.

    Compare event identities as well as amounts; never silently repair history.
    The read transaction gives a consistent SQLite snapshot during live play.
    """
    expected = Counter()
    flags = Flag.objects.filter(round__competition=competition).select_related(
        "round", "team_service", "checker_run",
    ).prefetch_related("captures")
    for flag in flags:
        captures = list(flag.captures.all())
        for capture in captures:
            expected[(flag.pk, capture.attacker_id, "attack", 10, capture.pk)] += 1
        if flag.round.state == "finished":
            run = flag.checker_run
            if run.put_status == "OK" and run.get_status == "OK" and not captures:
                expected[(flag.pk, flag.team_service.participation_id, "defense", 10, None)] += 1
            if run.service_status == "OK":
                expected[(flag.pk, flag.team_service.participation_id, "availability", 1, None)] += 1
    actual = Counter(ScoreEvent.objects.filter(flag__round__competition=competition).values_list(
        "flag_id", "participation_id", "kind", "points", "capture_id",
    ))
    return {"ok": actual == expected, "missing": sum((expected - actual).values()), "unexpected": sum((actual - expected).values())}
