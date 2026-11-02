from django.db import migrations
from django.utils import timezone


def seed_rosters(apps, schema_editor):
    database = schema_editor.connection.alias
    Competition = apps.get_model("competitions", "Competition")
    Participation = apps.get_model("competitions", "CompetitionParticipation")
    Membership = apps.get_model("competitions", "TeamMembership")
    Player = apps.get_model("competitions", "PlayerParticipation")
    Lock = apps.get_model("competitions", "CompetitionMutationLock")
    # A repeated seed must never rebuild an already frozen historical roster.
    unfrozen_ids = list(Competition.objects.using(database).filter(
        roster_frozen_at__isnull=True,
    ).values_list("pk", flat=True))
    existing = {
        (player.competition_id, player.user_id): (player.participation_id, player.role)
        for player in Player.objects.using(database).filter(competition_id__in=unfrozen_ids)
    }
    # Stop on ambiguous legacy data; never pick a team or delete old records.
    seen = {}
    rows = []
    participations = Participation.objects.using(database).filter(
        is_active=True, competition_id__in=unfrozen_ids,
    )
    for participation in participations.iterator():
        for member in Membership.objects.using(database).filter(team_id=participation.team_id):
            key = (participation.competition_id, member.user_id)
            if key in seen:
                raise RuntimeError(
                    f"Конфликт старых данных: соревнование {key[0]}, игрок {key[1]}, "
                    f"участия {seen[key]} и {participation.pk}. Требуется ручное решение."
                )
            seen[key] = participation.pk
            if key in existing:
                if existing[key] != (participation.pk, member.role):
                    raise RuntimeError(
                        f"Конфликт перенесённых данных: соревнование {key[0]}, "
                        f"игрок {key[1]}. Требуется ручное решение."
                    )
                continue
            rows.append(Player(
                competition_id=participation.competition_id,
                participation_id=participation.pk, user_id=member.user_id, role=member.role,
            ))
    if existing.keys() - seen.keys():
        raise RuntimeError("Перенесённый состав не соответствует членствам. Требуется ручное решение.")
    Player.objects.using(database).bulk_create(rows)
    # Observed membership is NOT proof of the roster at the old competition start.
    Competition.objects.using(database).filter(
        status__in=("running", "finished"), roster_frozen_at__isnull=True,
    ).update(
        roster_frozen_at=timezone.now(), roster_verified=False,
    )
    Lock.objects.using(database).get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [("competitions", "0015_competitionmutationlock_competition_launch_state_and_more")]
    operations = [migrations.RunPython(seed_rosters, migrations.RunPython.noop)]
