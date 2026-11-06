from unittest.mock import patch

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class GameMigrationTests(TransactionTestCase):
    def test_0018_preserves_old_tables_and_creates_no_fake_game_history(self):
        old_target = [("competitions", "0017_alter_competition_launch_state_and_more")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(old_target)
            old = executor.loader.project_state(old_target).apps
            User = old.get_model("auth", "User")
            Competition = old.get_model("competitions", "Competition")
            user = User.objects.create(username="legacy-game")
            competition = Competition.objects.create(name="Old", status="running", roster_frozen_at=timezone.now())
            team = old.get_model("competitions", "Team").objects.create(name="Old team")
            old.get_model("competitions", "TeamMembership").objects.create(team=team, user=user, role="captain")
            participation = old.get_model("competitions", "CompetitionParticipation").objects.create(team=team, competition=competition)
            old.get_model("competitions", "PlayerParticipation").objects.create(competition=competition, participation=participation, user=user, role="captain")
            service = old.get_model("competitions", "Service").objects.create(name="Old service")
            competition.services.add(service)
            old.get_model("competitions", "TeamService").objects.create(participation=participation, service=service, container_id="preserved", host_port=55001, status="running")
            attempt = old.get_model("competitions", "CompetitionLaunchAttempt").objects.create(competition=competition, initiated_by=user, state="succeeded", finished_at=timezone.now())
            competition.current_launch_attempt = attempt
            competition.save()
            models = list(old.get_app_config("competitions").get_models(include_auto_created=True))
            before = {m._meta.label: list(m.objects.order_by("pk").values()) for m in models}
            with patch("docker.from_env", side_effect=AssertionError("No Docker")):
                executor = MigrationExecutor(connection)
                executor.migrate(latest)
            self.assertEqual(before, {m._meta.label: list(m.objects.order_by("pk").values()) for m in models})
            current = executor.loader.project_state(latest).apps
            for name in ("Round", "Flag", "CheckerRun", "MockFlagStorage", "Capture", "ScoreEvent", "SubmissionWindow"):
                self.assertFalse(current.get_model("competitions", name).objects.exists())
            self.assertEqual(MigrationExecutor(connection).migration_plan(latest), [])
            with connection.cursor() as cursor:
                self.assertEqual(cursor.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                self.assertEqual(cursor.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            MigrationExecutor(connection).migrate(latest)
