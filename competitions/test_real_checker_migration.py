from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone
from datetime import timedelta


class RealCheckerMigrationTests(TransactionTestCase):
    def test_0019_preserves_nonempty_game_history_and_old_container_values(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_target = [("competitions", "0018_game_engine")]
        try:
            executor.migrate(old_target)
            apps = executor.loader.project_state(old_target).apps
            def model(name):
                return apps.get_model("competitions", name)
            user = apps.get_model("auth", "User").objects.create(username="old-player")
            now = timezone.now()
            competition = model("Competition").objects.create(name="Old", status="finished", roster_frozen_at=now)
            service = model("Service").objects.create(name="Old service")
            competition.services.add(service)
            participations = []
            for n in range(2):
                team = model("Team").objects.create(name=f"Old {n}")
                participation = model("CompetitionParticipation").objects.create(team=team, competition=competition)
                participations.append(participation)
            model("TeamMembership").objects.create(team=participations[0].team, user=user, role="captain")
            player = model("PlayerParticipation").objects.create(competition=competition, participation=participations[0], user=user, role="captain")
            ts = model("TeamService").objects.create(participation=participations[1], service=service, container_id="keep-container", host_port=55000, status="running")
            game_round = model("Round").objects.create(competition=competition, number=1, started_at=now, expires_at=now+timedelta(minutes=5), finished_at=now+timedelta(minutes=1), state="finished", checker_backend="mock")
            flag = model("Flag").objects.create(round=game_round, team_service=ts, digest="a"*64, state="installed")
            model("MockFlagStorage").objects.create(flag=flag, digest=flag.digest)
            model("CheckerRun").objects.create(flag=flag, put_status="OK", get_status="OK", service_status="OK", checked_at=now)
            capture = model("Capture").objects.create(flag=flag, attacker=participations[0], submitted_by=player)
            model("ScoreEvent").objects.create(flag=flag, participation=participations[0], capture=capture, kind="attack", points=10)
            model("SubmissionWindow").objects.create(participation=participations[0], started_at=now, count=1)
            models = list(apps.get_app_config("competitions").get_models(include_auto_created=True))
            before = {m._meta.label: list(m.objects.order_by("pk").values()) for m in models}
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            self.assertEqual(before, {m._meta.label: list(m.objects.order_by("pk").values()) for m in models})
            current = executor.loader.project_state(latest).apps
            self.assertEqual(current.get_model("competitions", "Service").objects.get(pk=service.pk).runtime, "legacy")
            for name in ("CheckerJob", "CheckerEndpoint", "FlagMaterial"):
                self.assertFalse(current.get_model("competitions", name).objects.exists())
            self.assertEqual(MigrationExecutor(connection).migration_plan(latest), [])
            with connection.cursor() as cursor:
                self.assertEqual(cursor.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                self.assertEqual(cursor.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            MigrationExecutor(connection).migrate(latest)
