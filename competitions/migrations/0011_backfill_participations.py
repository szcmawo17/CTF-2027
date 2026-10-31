from django.db import migrations


def backfill_participations(apps, schema_editor):
    database = schema_editor.connection.alias
    Team = apps.get_model("competitions", "Team")
    TeamService = apps.get_model("competitions", "TeamService")
    CompetitionParticipation = apps.get_model(
        "competitions", "CompetitionParticipation"
    )

    for team in Team.objects.using(database).all().iterator(chunk_size=500):
        participation, _ = CompetitionParticipation.objects.using(
            database
        ).get_or_create(team_id=team.pk, competition_id=team.competition_id)
        TeamService.objects.using(database).filter(team_id=team.pk).update(
            participation_id=participation.pk
        )

    if TeamService.objects.using(database).filter(participation_id__isnull=True).exists():
        raise RuntimeError("Не все сервисы команд получили участие в соревновании.")


class Migration(migrations.Migration):
    dependencies = [
        ("competitions", "0010_add_participation_and_membership"),
    ]

    operations = [
        migrations.RunPython(backfill_participations, migrations.RunPython.noop),
    ]
