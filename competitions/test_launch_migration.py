from unittest.mock import patch

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class LaunchJournalMigrationTests(TransactionTestCase):
    def test_0017_preserves_existing_rosters_and_container_records_without_fake_attempts(self):
        old_target = [("competitions", "0016_seed_rosters_and_lock")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(old_target)
            old = executor.loader.project_state(old_target).apps
            User = old.get_model("auth", "User")
            Competition = old.get_model("competitions", "Competition")
            Team = old.get_model("competitions", "Team")
            Membership = old.get_model("competitions", "TeamMembership")
            Participation = old.get_model("competitions", "CompetitionParticipation")
            Player = old.get_model("competitions", "PlayerParticipation")
            Service = old.get_model("competitions", "Service")
            TeamService = old.get_model("competitions", "TeamService")
            user = User.objects.create(username="legacy-player")
            competition = Competition.objects.create(
                name="Legacy", description="", status="registration", launch_state="deploying",
                roster_frozen_at=timezone.now(), roster_verified=True,
            )
            team = Team.objects.create(name="Legacy team")
            Membership.objects.create(team=team, user=user, role="captain")
            participation = Participation.objects.create(team=team, competition=competition)
            Player.objects.create(competition=competition, participation=participation, user=user, role="captain")
            service = Service.objects.create(name="Legacy service", description="")
            competition.services.add(service)
            TeamService.objects.create(
                participation=participation, service=service,
                container_id="existing-container", host_port=55000, status="running",
            )
            models = (User, Competition, Team, Membership, Participation, Player, Service, TeamService)
            before = [list(model.objects.order_by("pk").values()) for model in models]
            with patch("docker.from_env", side_effect=AssertionError("Real Docker forbidden")):
                executor = MigrationExecutor(connection)
                executor.migrate(latest)
            self.assertEqual([list(model.objects.order_by("pk").values()) for model in models], before)
            apps = executor.loader.project_state(latest).apps
            self.assertIsNone(apps.get_model("competitions", "Competition").objects.get(pk=competition.pk).current_launch_attempt_id)
            self.assertFalse(apps.get_model("competitions", "CompetitionLaunchAttempt").objects.exists())
            self.assertFalse(apps.get_model("competitions", "CompetitionRecoveryAction").objects.exists())
        finally:
            MigrationExecutor(connection).migrate(latest)
