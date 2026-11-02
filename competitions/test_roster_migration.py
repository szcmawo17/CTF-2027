from importlib import import_module

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class RosterMigrationTests(TransactionTestCase):
    old_target = ("competitions", "0014_alter_competition_status")
    new_target = ("competitions", "0016_seed_rosters_and_lock")

    def setUp(self):
        self.latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        executor = MigrationExecutor(connection)
        executor.migrate([self.old_target])
        self.apps = executor.loader.project_state([self.old_target]).apps
        self.conflicting_membership_id = None

    def tearDown(self):
        if self.conflicting_membership_id:
            self.apps.get_model("competitions", "TeamMembership").objects.filter(pk=self.conflicting_membership_id).delete()
        MigrationExecutor(connection).migrate(self.latest)
        super().tearDown()

    def seed(self, status="registration"):
        User = self.apps.get_model("auth", "User")
        Competition = self.apps.get_model("competitions", "Competition")
        Team = self.apps.get_model("competitions", "Team")
        Membership = self.apps.get_model("competitions", "TeamMembership")
        Participation = self.apps.get_model("competitions", "CompetitionParticipation")
        user = User.objects.create(username="legacy")
        competition = Competition.objects.create(name="Legacy", description="", status=status)
        team = Team.objects.create(name="Red")
        Membership.objects.create(team=team, user=user, role="captain")
        participation = Participation.objects.create(team=team, competition=competition)
        Service = self.apps.get_model("competitions", "Service")
        TeamService = self.apps.get_model("competitions", "TeamService")
        service = Service.objects.create(name="Web", description="")
        team_service = TeamService.objects.create(participation=participation, service=service, container_id="preserved", host_port=55000, status="running")
        return competition, team, user, participation, team_service

    def migrate(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.new_target])
        return executor.loader.project_state([self.new_target]).apps

    def repeat_seed(self, apps):
        migration = import_module("competitions.migrations.0016_seed_rosters_and_lock")
        with connection.schema_editor() as editor:
            migration.seed_rosters(apps, editor)

    def test_data_seed_is_idempotent_with_registered_players(self):
        self.seed()
        apps = self.migrate()
        Player = apps.get_model("competitions", "PlayerParticipation")
        Lock = apps.get_model("competitions", "CompetitionMutationLock")
        expected = list(Player.objects.order_by("pk").values())
        Lock.objects.filter(pk=1).update(revision=7)
        self.repeat_seed(apps)
        self.assertEqual(list(Player.objects.order_by("pk").values()), expected)
        self.assertEqual(Lock.objects.get(pk=1).revision, 7)

    def test_repeated_seed_preserves_empty_legacy_freeze_timestamp(self):
        competition, *_ = self.seed(status="running")
        self.apps.get_model("competitions", "TeamMembership").objects.all().delete()
        apps = self.migrate()
        Competition = apps.get_model("competitions", "Competition")
        expected = Competition.objects.get(pk=competition.pk).roster_frozen_at
        self.repeat_seed(apps)
        self.assertEqual(Competition.objects.get(pk=competition.pk).roster_frozen_at, expected)

    def test_repeated_seed_does_not_rewrite_finished_history(self):
        competition, team, user, *_ = self.seed(status="finished")
        apps = self.migrate()
        Player = apps.get_model("competitions", "PlayerParticipation")
        Competition = apps.get_model("competitions", "Competition")
        expected_players = list(Player.objects.order_by("pk").values())
        expected_competition = Competition.objects.values().get(pk=competition.pk)
        apps.get_model("competitions", "TeamMembership").objects.filter(
            team_id=team.pk, user_id=user.pk
        ).update(role="member")
        self.repeat_seed(apps)
        self.assertEqual(list(Player.objects.order_by("pk").values()), expected_players)
        self.assertEqual(Competition.objects.values().get(pk=competition.pk), expected_competition)

    def test_seed_rejects_inconsistent_existing_player_without_overwriting(self):
        competition, team, user, participation, _ = self.seed()
        target = ("competitions", "0015_competitionmutationlock_competition_launch_state_and_more")
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        apps = executor.loader.project_state([target]).apps
        Player = apps.get_model("competitions", "PlayerParticipation")
        player = Player.objects.create(
            competition_id=competition.pk, participation_id=participation.pk,
            user_id=user.pk, role="member",
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "Конфликт перенесённых данных"):
                self.migrate()
            self.assertEqual(Player.objects.get(pk=player.pk).role, "member")
            self.assertEqual(Player.objects.count(), 1)
        finally:
            # Remove only this deliberately inconsistent test fixture.
            player.delete()

    def test_legacy_rows_and_container_references_preserved(self):
        competition, team, user, participation, team_service = self.seed()
        apps = self.migrate()
        Player = apps.get_model("competitions", "PlayerParticipation")
        player = Player.objects.get(competition_id=competition.pk, user_id=user.pk)
        self.assertEqual(player.participation_id, participation.pk)
        self.assertEqual(player.role, "captain")
        migrated = apps.get_model("competitions", "TeamService").objects.get(pk=team_service.pk)
        self.assertEqual((migrated.container_id, migrated.host_port, migrated.status), ("preserved", 55000, "running"))
        self.assertTrue(apps.get_model("competitions", "CompetitionMutationLock").objects.filter(pk=1).exists())

    def test_old_running_roster_is_explicitly_unverified(self):
        competition, *_ = self.seed(status="running")
        apps = self.migrate()
        migrated = apps.get_model("competitions", "Competition").objects.get(pk=competition.pk)
        self.assertEqual(migrated.status, "running")
        self.assertFalse(migrated.roster_verified)
        self.assertIsNotNone(migrated.roster_frozen_at)

    def test_conflicting_legacy_players_abort_without_deleting_records(self):
        competition, team, user, participation, team_service = self.seed()
        Team = self.apps.get_model("competitions", "Team")
        Membership = self.apps.get_model("competitions", "TeamMembership")
        Participation = self.apps.get_model("competitions", "CompetitionParticipation")
        other = Team.objects.create(name="Blue")
        Participation.objects.create(team=other, competition=competition)
        self.conflicting_membership_id = Membership.objects.create(team=other, user=user, role="captain").pk
        with self.assertRaisesRegex(RuntimeError, "Конфликт старых данных"):
            self.migrate()
        self.assertEqual(Team.objects.count(), 2)
        self.assertEqual(Participation.objects.count(), 2)
        self.assertTrue(self.apps.get_model("competitions", "TeamService").objects.filter(pk=team_service.pk, container_id="preserved").exists())
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM competitions_playerparticipation")
            self.assertEqual(cursor.fetchone()[0], 0)
