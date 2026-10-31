from django.conf import settings
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
    
    services = models.ManyToManyField(
    "Service",
    related_name="competitions",
    blank=True
)

    def __str__(self):
        return self.name


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


