from django.db import models

class Competition(models.Model):
    STATUS_CHOICES = [
        ("draft", "Подготовка"),
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

    
    name = models.CharField(max_length=200)
    description = models.TextField()
    
    services = models.ManyToManyField(
    "Service",
    related_name="competitions",
    blank=True
)

    def __str__(self):
        return self.name


class Team(models.Model):
    name = models.CharField(max_length=200)
    competition = models.ForeignKey(
        Competition,
        on_delete=models.CASCADE,
        related_name="teams"
    )
    def __str__(self):
        return self.name

class Service(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField()

    def __str__(self):
        return self.name

class TeamService(models.Model):
    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name="services"
    )

    service = models.ForeignKey(
        Service,
        on_delete=models.CASCADE,
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

    def __str__(self):
        return f"{self.team} - {self.service}"    


