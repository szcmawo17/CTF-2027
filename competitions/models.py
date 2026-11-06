import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

class Competition(models.Model):
    STATUS_CHOICES = [
        ("draft", "Подготовка"),
        ("registration", "Регистрация команд"),
        ("running", "Идёт"),
        ("finished", "Завершено"),
    ]

    name = models.CharField(max_length=200)
    description = models.TextField()

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="draft"
    )
    
    launch_state = models.CharField(
        max_length=20,
        choices=[
            ("idle", "Не запускается"),
            ("deploying", "Развёртывание"),
            ("failed", "Ошибка запуска"),
            ("review", "Требуется ручное вмешательство"),
        ],
        default="idle", editable=False,
    )
    roster_frozen_at = models.DateTimeField(null=True, blank=True, editable=False)
    roster_verified = models.BooleanField(default=True, editable=False)
    current_launch_attempt = models.ForeignKey(
        "CompetitionLaunchAttempt", null=True, blank=True,
        on_delete=models.PROTECT, related_name="current_for", editable=False,
    )

    @property
    def registration_open(self):
        return (
            self.status == "registration"
            and self.launch_state == "idle"
            and self.roster_frozen_at is None
        )

    services = models.ManyToManyField(
    "Service",
    related_name="competitions",
    blank=True
)

    def __str__(self):
        return self.name


class CompetitionLaunchAttempt(models.Model):
    STATES = [
        ("in_progress", "Выполняется"),
        ("succeeded", "Успешна"),
        ("failed", "Ошибка до развёртывания"),
        ("review", "Требует проверки"),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    competition = models.ForeignKey(
        Competition, on_delete=models.PROTECT, related_name="launch_attempts"
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="competition_launch_attempts",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=20, choices=STATES, default="in_progress")
    message = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["competition"], condition=Q(state__in=["in_progress", "review"]),
                name="one_unresolved_launch_per_competition",
            ),
            models.CheckConstraint(
                condition=(
                    Q(state="in_progress", finished_at__isnull=True)
                    | Q(state__in=["succeeded", "failed"], finished_at__isnull=False)
                    | Q(state="review")
                ), name="launch_attempt_state_and_end",
            ),
        ]

    def __str__(self):
        return f"{self.competition_id}: {self.id} ({self.get_state_display()})"


class CompetitionRecoveryAction(models.Model):
    # The diagnosis UUID is also a single-use action identifier.
    id = models.UUIDField(primary_key=True, editable=False)
    competition = models.ForeignKey(
        Competition, on_delete=models.PROTECT, related_name="recovery_actions"
    )
    attempt = models.ForeignKey(
        CompetitionLaunchAttempt, on_delete=models.PROTECT, null=True, blank=True,
        related_name="recovery_actions",
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="competition_recovery_actions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    action = models.CharField(
        max_length=30, default="manual_review",
        choices=[("manual_review", "Зафиксировать ручное вмешательство")],
    )
    message = models.TextField()
    diagnosis = models.JSONField()

    class Meta:
        ordering = ["-created_at"]


class Team(models.Model):
    name = models.CharField(max_length=200)

    def __str__(self):
        return self.name


class TeamMembership(models.Model):
    ROLE_CHOICES = [
        ("captain", "Капитан"),
        ("member", "Участник"),
    ]

    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="team_memberships",
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="member")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team", "user"], name="unique_team_membership"
            ),
            models.UniqueConstraint(
                fields=["team"],
                condition=Q(role="captain"),
                name="unique_team_captain",
            ),
        ]

    def __str__(self):
        return f"{self.user} — {self.team} ({self.get_role_display()})"


class TeamInvitation(models.Model):
    STATUS_CHOICES = [
        ("pending", "Ожидает ответа"),
        ("accepted", "Принято"),
        ("declined", "Отклонено"),
        ("cancelled", "Отозвано"),
    ]

    team = models.ForeignKey(
        Team, on_delete=models.CASCADE, related_name="invitations"
    )
    invitee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="team_invitations",
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_team_invitations",
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="pending"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team", "invitee"],
                condition=Q(status="pending"),
                name="unique_pending_team_invitation",
            ),
        ]

    def __str__(self):
        return f"{self.invitee} — {self.team} ({self.get_status_display()})"


class CompetitionParticipation(models.Model):
    is_active = models.BooleanField(default=True)
    team = models.ForeignKey(
        Team, on_delete=models.PROTECT, related_name="participations"
    )
    competition = models.ForeignKey(
        Competition, on_delete=models.PROTECT, related_name="participations"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team", "competition"],
                name="unique_team_competition_participation",
            ),
        ]

    def __str__(self):
        return f"{self.team} — {self.competition}"


class CompetitionMutationLock(models.Model):
    """Singleton used to serialize short lifecycle/roster writes."""
    revision = models.PositiveBigIntegerField(default=0)


class PlayerParticipation(models.Model):
    """Mutable registration claim, then a frozen competition roster entry."""

    competition = models.ForeignKey(
        Competition, on_delete=models.PROTECT, related_name="players"
    )
    participation = models.ForeignKey(
        CompetitionParticipation, on_delete=models.PROTECT, related_name="players"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="competition_players",
    )
    role = models.CharField(max_length=20, choices=TeamMembership.ROLE_CHOICES)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["competition", "user"], name="unique_player_per_competition"
            ),
        ]

    def clean(self):
        super().clean()
        if self.participation_id and self.participation.competition_id != self.competition_id:
            raise ValidationError("Участие команды относится к другому соревнованию.")

    def __str__(self):
        return f"{self.user} — {self.participation}"


class Service(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField()

    def __str__(self):
        return self.name

class TeamService(models.Model):
    participation = models.ForeignKey(
        CompetitionParticipation,
        on_delete=models.PROTECT,
        related_name="services"
    )

    service = models.ForeignKey(
        Service,
        on_delete=models.PROTECT,
        related_name="team_services"
    )

    container_id = models.CharField(
    max_length=100,
    blank=True,
    default=""
    )

    host_port = models.PositiveIntegerField(
        null=True,
        blank=True
    )

    status = models.CharField(
        max_length=20,
        default="created"
    )
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["participation", "service"],
                name="unique_participation_service"
            )
        ]
    def __str__(self):
        return f"{self.participation.team} - {self.service} ({self.participation.competition})"


class Round(models.Model):
    competition = models.ForeignKey(Competition, on_delete=models.PROTECT, related_name="rounds")
    number = models.PositiveIntegerField()
    started_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=12, choices=[("open", "Открыт"), ("finished", "Завершён")])
    checker_backend = models.CharField(max_length=16, default="mock", editable=False)

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["competition", "number"], name="unique_competition_round"),
            models.UniqueConstraint(fields=["competition"], condition=Q(state="open"), name="one_open_round"),
            models.CheckConstraint(condition=Q(number__gte=1), name="positive_round_number"),
            models.CheckConstraint(condition=Q(expires_at__gt=models.F("started_at")), name="round_expiry_after_start"),
            models.CheckConstraint(condition=(Q(state="open", finished_at__isnull=True) | Q(state="finished", finished_at__isnull=False, finished_at__gte=models.F("started_at"))), name="round_state_and_end"),
            models.CheckConstraint(condition=Q(checker_backend="mock"), name="round_mock_backend_only"),
        ]

    def __str__(self):
        return f"{self.competition_id}: раунд {self.number}"


class Flag(models.Model):
    round = models.ForeignKey(Round, on_delete=models.PROTECT, related_name="flags")
    team_service = models.ForeignKey(TeamService, on_delete=models.PROTECT, related_name="flags")
    digest = models.CharField(max_length=64, unique=True, editable=False)
    state = models.CharField(max_length=12, choices=[("issued", "Создан"), ("installed", "Установлен в mock"), ("rejected", "Не установлен")])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["round", "team_service"], name="one_flag_per_round_service"),
            models.CheckConstraint(condition=Q(state__in=["issued", "installed", "rejected"]), name="valid_flag_state"),
        ]

    def clean(self):
        super().clean()
        if self.round_id and self.team_service_id and self.round.competition_id != self.team_service.participation.competition_id:
            raise ValidationError("Сервис и раунд относятся к разным соревнованиям.")


CHECKER_STATUSES = [(code, code) for code in ("OK", "DOWN", "MUMBLE", "ERROR")]


class CheckerRun(models.Model):
    flag = models.OneToOneField(Flag, on_delete=models.PROTECT, related_name="checker_run")
    put_status = models.CharField(max_length=8, choices=CHECKER_STATUSES)
    get_status = models.CharField(max_length=8, choices=CHECKER_STATUSES, blank=True, default="")
    service_status = models.CharField(max_length=8, choices=CHECKER_STATUSES, blank=True, default="")
    checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(put_status__in=["OK", "DOWN", "MUMBLE", "ERROR"]), name="valid_checker_put"),
            models.CheckConstraint(condition=(
                Q(checked_at__isnull=True, get_status="", service_status="")
                | Q(checked_at__isnull=False, get_status__in=["OK", "DOWN", "MUMBLE", "ERROR"], service_status__in=["OK", "DOWN", "MUMBLE", "ERROR"])
            ), name="checker_complete_result"),
        ]


class MockFlagStorage(models.Model):
    """Simulator storage, never proof that a real container received a flag."""
    flag = models.OneToOneField(Flag, on_delete=models.PROTECT, related_name="mock_storage")
    digest = models.CharField(max_length=64, editable=False)


class Capture(models.Model):
    flag = models.ForeignKey(Flag, on_delete=models.PROTECT, related_name="captures")
    attacker = models.ForeignKey(CompetitionParticipation, on_delete=models.PROTECT, related_name="captures")
    submitted_by = models.ForeignKey(PlayerParticipation, on_delete=models.PROTECT, related_name="captures")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["flag", "attacker"], name="one_capture_per_attacker_flag")]

    def clean(self):
        super().clean()
        if self.flag_id and self.attacker_id:
            if self.flag.round.competition_id != self.attacker.competition_id or self.flag.team_service.participation_id == self.attacker_id:
                raise ValidationError("Недопустимая команда для захвата флага.")
        if self.submitted_by_id and self.submitted_by.participation_id != self.attacker_id:
            raise ValidationError("Игрок не представляет атакующую команду.")


class ScoreEvent(models.Model):
    flag = models.ForeignKey(Flag, on_delete=models.PROTECT, related_name="score_events")
    participation = models.ForeignKey(CompetitionParticipation, on_delete=models.PROTECT, related_name="score_events")
    capture = models.OneToOneField(Capture, on_delete=models.PROTECT, null=True, blank=True, related_name="score_event")
    kind = models.CharField(max_length=12, choices=[("attack", "Атака"), ("defense", "Защита"), ("availability", "Доступность")])
    points = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        super().clean()
        if not self.flag_id or not self.participation_id:
            return
        if self.flag.round.competition_id != self.participation.competition_id:
            raise ValidationError("Событие очков относится к другому соревнованию.")
        if self.kind == "attack":
            if (not self.capture_id or self.capture.flag_id != self.flag_id
                    or self.capture.attacker_id != self.participation_id):
                raise ValidationError("Событие атаки не соответствует захвату флага.")
        elif self.participation_id != self.flag.team_service.participation_id:
            raise ValidationError("Защита и доступность начисляются владельцу сервиса.")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["flag", "participation", "kind"], name="one_score_event_per_source"),
            models.CheckConstraint(condition=(
                Q(kind="attack", points=10, capture__isnull=False)
                | Q(kind="defense", points=10, capture__isnull=True)
                | Q(kind="availability", points=1, capture__isnull=True)
            ), name="score_event_shape"),
        ]


class SubmissionWindow(models.Model):
    """Shared per-team limit across users and Django processes."""
    participation = models.OneToOneField(CompetitionParticipation, on_delete=models.PROTECT, related_name="submission_window")
    started_at = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)


