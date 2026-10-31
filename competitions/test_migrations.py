from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class LegacyRelationsMigrationTests(TransactionTestCase):
    migrate_from = ("competitions", "0009_teamservice_unique_team_service")
    migrate_to = ("competitions", "0012_finalize_participation_relations")

    def test_legacy_teams_and_container_references_survive(self):
        try:
            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_from])
            old_apps = executor.loader.project_state([self.migrate_from]).apps

            Competition = old_apps.get_model("competitions", "Competition")
            Team = old_apps.get_model("competitions", "Team")
            Service = old_apps.get_model("competitions", "Service")
            TeamService = old_apps.get_model("competitions", "TeamService")

            first_competition = Competition.objects.create(
                name="Legacy CTF", description="Existing competition"
            )
            second_competition = Competition.objects.create(
                name="Another CTF", description="Another competition"
            )
            first_team = Team.objects.create(
                name="Legacy team", competition=first_competition
            )
            second_team = Team.objects.create(
                name="Second legacy team", competition=first_competition
            )
            empty_team = Team.objects.create(
                name="Team without services", competition=second_competition
            )
            service = Service.objects.create(
                name="Legacy service", description="Existing service"
            )
            second_service = Service.objects.create(
                name="Second service", description="Another service"
            )
            first_competition.services.add(service, second_service)
            old_team_service = TeamService.objects.create(
                team=first_team,
                service=service,
                container_id="existing-container-id",
                host_port=55000,
                status="running",
            )
            other_old_services = [
                TeamService.objects.create(
                    team=team,
                    service=team_service,
                    container_id=f"container-{index}",
                    host_port=55000 + index,
                    status="running",
                )
                for index, (team, team_service) in enumerate(
                    (
                        (first_team, second_service),
                        (second_team, service),
                        (second_team, second_service),
                    ),
                    start=1,
                )
            ]

            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_to])
            new_apps = executor.loader.project_state([self.migrate_to]).apps

            MigratedTeam = new_apps.get_model("competitions", "Team")
            MigratedTeamService = new_apps.get_model("competitions", "TeamService")
            Participation = new_apps.get_model(
                "competitions", "CompetitionParticipation"
            )
            Membership = new_apps.get_model("competitions", "TeamMembership")

            self.assertTrue(MigratedTeam.objects.filter(pk=first_team.pk).exists())
            self.assertTrue(MigratedTeam.objects.filter(pk=second_team.pk).exists())
            self.assertTrue(MigratedTeam.objects.filter(pk=empty_team.pk).exists())
            self.assertEqual(Participation.objects.count(), 3)
            self.assertEqual(Membership.objects.count(), 0)
            self.assertEqual(MigratedTeamService.objects.count(), 4)

            migrated_service = MigratedTeamService.objects.get(pk=old_team_service.pk)
            self.assertEqual(migrated_service.service_id, service.pk)
            self.assertEqual(migrated_service.container_id, "existing-container-id")
            self.assertEqual(migrated_service.host_port, 55000)
            self.assertEqual(migrated_service.status, "running")
            self.assertEqual(migrated_service.participation.team_id, first_team.pk)
            self.assertEqual(
                migrated_service.participation.competition_id,
                first_competition.pk,
            )
            for old_service in other_old_services:
                migrated = MigratedTeamService.objects.get(pk=old_service.pk)
                self.assertEqual(migrated.service_id, old_service.service_id)
                self.assertEqual(migrated.container_id, old_service.container_id)
                self.assertEqual(migrated.host_port, old_service.host_port)
                self.assertEqual(migrated.status, old_service.status)
                self.assertEqual(
                    migrated.participation.team_id, old_service.team_id
                )

            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_from])
            restored_apps = executor.loader.project_state([self.migrate_from]).apps
            RestoredTeam = restored_apps.get_model("competitions", "Team")
            RestoredTeamService = restored_apps.get_model(
                "competitions", "TeamService"
            )
            self.assertEqual(
                RestoredTeam.objects.get(pk=first_team.pk).competition_id,
                first_competition.pk,
            )
            restored_service = RestoredTeamService.objects.get(
                pk=old_team_service.pk
            )
            self.assertEqual(restored_service.team_id, first_team.pk)
            self.assertEqual(restored_service.container_id, "existing-container-id")
            self.assertEqual(RestoredTeamService.objects.count(), 4)
        finally:
            MigrationExecutor(connection).migrate([self.migrate_to])
