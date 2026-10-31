from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("competitions", "0009_teamservice_unique_team_service"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CompetitionParticipation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("competition", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="participations", to="competitions.competition")),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="participations", to="competitions.team")),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=["team", "competition"], name="unique_team_competition_participation"),
                ],
            },
        ),
        migrations.CreateModel(
            name="TeamMembership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("captain", "Капитан"), ("member", "Участник")], default="member", max_length=20)),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="memberships", to="competitions.team")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="team_memberships", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=["team", "user"], name="unique_team_membership"),
                    models.UniqueConstraint(fields=["team"], condition=Q(role="captain"), name="unique_team_captain"),
                ],
            },
        ),
        migrations.AddField(
            model_name="teamservice",
            name="participation",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name="services", to="competitions.competitionparticipation"),
        ),
    ]
