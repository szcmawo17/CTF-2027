from django.db import migrations, models
import django.db.models.deletion


def validate_backfill(apps, schema_editor):
    database = schema_editor.connection.alias
    Team = apps.get_model("competitions", "Team")
    TeamService = apps.get_model("competitions", "TeamService")
    CompetitionParticipation = apps.get_model(
        "competitions", "CompetitionParticipation"
    )

    for team in Team.objects.using(database).all().iterator(chunk_size=500):
        participations = CompetitionParticipation.objects.using(database).filter(
            team_id=team.pk
        )
        if participations.count() != 1 or not participations.filter(
            competition_id=team.competition_id
        ).exists():
            raise RuntimeError(f"Неоднозначное участие команды {team.pk}.")

    for team_service in TeamService.objects.using(database).select_related(
        "participation"
    ).iterator(chunk_size=500):
        if (
            team_service.participation_id is None
            or team_service.participation.team_id != team_service.team_id
        ):
            raise RuntimeError(f"Неоднозначная связь сервиса {team_service.pk}.")


def restore_legacy_relations(apps, schema_editor):
    """Reverse only when every team still belongs to exactly one competition."""
    database = schema_editor.connection.alias
    Team = apps.get_model("competitions", "Team")
    TeamService = apps.get_model("competitions", "TeamService")
    CompetitionParticipation = apps.get_model(
        "competitions", "CompetitionParticipation"
    )

    for team in Team.objects.using(database).all().iterator(chunk_size=500):
        competition_ids = list(
            CompetitionParticipation.objects.using(database)
            .filter(team_id=team.pk)
            .values_list("competition_id", flat=True)
        )
        if len(competition_ids) != 1:
            raise RuntimeError(
                f"Команду {team.pk} нельзя вернуть к одной связи с соревнованием."
            )
        Team.objects.using(database).filter(pk=team.pk).update(
            competition_id=competition_ids[0]
        )

    for team_service in TeamService.objects.using(database).select_related(
        "participation"
    ).iterator(chunk_size=500):
        if team_service.participation_id is None:
            raise RuntimeError(f"Сервис {team_service.pk} не связан с участием.")
        TeamService.objects.using(database).filter(pk=team_service.pk).update(
            team_id=team_service.participation.team_id
        )


class Migration(migrations.Migration):
    dependencies = [
        ("competitions", "0011_backfill_participations"),
    ]

    operations = [
        migrations.AlterField(
            model_name="team",
            name="competition",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name="teams", to="competitions.competition"),
        ),
        migrations.AlterField(
            model_name="teamservice",
            name="team",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name="services", to="competitions.team"),
        ),
        migrations.RunPython(validate_backfill, restore_legacy_relations),
        migrations.RemoveConstraint(
            model_name="teamservice", name="unique_team_service"
        ),
        migrations.AlterField(
            model_name="teamservice",
            name="participation",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="services", to="competitions.competitionparticipation"),
        ),
        migrations.AddConstraint(
            model_name="teamservice",
            constraint=models.UniqueConstraint(fields=["participation", "service"], name="unique_participation_service"),
        ),
        migrations.AlterField(
            model_name="teamservice",
            name="service",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="team_services", to="competitions.service"),
        ),
        migrations.RemoveField(model_name="teamservice", name="team"),
        migrations.RemoveField(model_name="team", name="competition"),
    ]
