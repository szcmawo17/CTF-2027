from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from .models import (
    Competition, CompetitionMutationLock, CompetitionParticipation,
    PlayerParticipation, Service, TeamMembership, TeamService,
)
from .participation_services import register_team, withdraw_team
from .services import open_registration, start_competition, finish_competition
from .team_services import (
    create_team, invite_user, respond_to_invitation, transfer_captain,
    leave_team, remove_member,
)


class LifecycleFixtures:
    def setUp(self):
        super().setUp()
        guard = patch("docker.from_env", side_effect=AssertionError("Real Docker is forbidden in tests"))
        guard.start()
        self.addCleanup(guard.stop)
        CompetitionMutationLock.objects.get_or_create(pk=1)
        User = get_user_model()
        self.organizer = User.objects.create_user(username="organizer", is_staff=True)
        self.organizer.user_permissions.add(Permission.objects.get(
            content_type__app_label="competitions", codename="change_competition"
        ))
        self.alice = User.objects.create_user(username="alice")
        self.bob = User.objects.create_user(username="bob")
        self.charlie = User.objects.create_user(username="charlie")
        self.red = create_team("Red", self.alice)
        self.blue = create_team("Blue", self.bob)
        self.competition = Competition.objects.create(name="CTF", description="Test")
        self.service = Service.objects.create(name="Web", description="Test")
        self.competition.services.add(self.service)

    def register_red(self):
        open_registration(self.competition, self.organizer)
        return register_team(self.competition, self.red, self.alice)

    def start_red(self):
        participation = self.register_red()
        with patch("competitions.services.deploy_competition_services"):
            start_competition(self.competition, self.organizer)
        return participation


class LifecycleTests(LifecycleFixtures, TestCase):
    def test_every_forbidden_status_transition_leaves_state_unchanged(self):
        allowed = {open_registration: "draft", start_competition: "registration", finish_competition: "running"}
        with patch("competitions.services.deploy_competition_services") as deploy:
            for status in ("draft", "registration", "running", "finished"):
                for operation, source in allowed.items():
                    if status == source:
                        continue
                    with self.subTest(status=status, operation=operation.__name__):
                        Competition.objects.filter(pk=self.competition.pk).update(status=status)
                        with self.assertRaises(ValidationError):
                            operation(self.competition, self.organizer)
                        self.competition.refresh_from_db()
                        self.assertEqual(self.competition.status, status)
            deploy.assert_not_called()

    def test_forward_transitions_and_finish_never_operates_docker(self):
        with patch("competitions.services.deploy_competition_services") as deploy:
            open_registration(self.competition, self.organizer)
            deploy.assert_not_called()
            participation = register_team(self.competition, self.red, self.alice)
            start_competition(self.competition, self.organizer)
            deploy.assert_called_once()
        with patch("competitions.infrastructure.stop_team_service") as stop:
            finish_competition(self.competition, self.organizer)
            stop.assert_not_called()
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, "finished")
        self.assertIsNotNone(self.competition.roster_frozen_at)
        self.assertEqual(participation.players.get().user, self.alice)

    def test_no_skip_backward_or_repeated_transitions(self):
        with patch("competitions.services.deploy_competition_services") as deploy:
            for operation in (start_competition, finish_competition):
                with self.assertRaises(ValidationError):
                    operation(self.competition, self.organizer)
            deploy.assert_not_called()
        self.start_red()
        for operation in (open_registration, start_competition):
            with self.assertRaises(ValidationError):
                operation(self.competition, self.organizer)
        finish_competition(self.competition, self.organizer)
        for operation in (open_registration, start_competition, finish_competition):
            with self.assertRaises(ValidationError):
                operation(self.competition, self.organizer)

    def test_permissions_checked_in_service_even_for_staff(self):
        self.alice.is_staff = True
        self.alice.save(update_fields=["is_staff"])
        for operation in (open_registration, start_competition, finish_competition):
            with self.assertRaises(PermissionDenied):
                operation(self.competition, self.alice)
        with self.assertRaises(PermissionDenied):
            start_competition(self.competition)

    def test_start_requires_teams_services_and_a_captain(self):
        open_registration(self.competition, self.organizer)
        with patch("competitions.services.deploy_competition_services") as deploy:
            with self.assertRaises(ValidationError):
                start_competition(self.competition, self.organizer)
            register_team(self.competition, self.red, self.alice)
            self.competition.services.clear()
            with self.assertRaises(ValidationError):
                start_competition(self.competition, self.organizer)
            self.competition.services.add(self.service)
            TeamMembership.objects.filter(team=self.red).update(role="member")
            with self.assertRaises(ValidationError):
                start_competition(self.competition, self.organizer)
            deploy.assert_not_called()
        self.competition.refresh_from_db()
        self.assertTrue(self.competition.registration_open)

    def test_failure_keeps_roster_closed_and_retry_uses_same_snapshot(self):
        participation = self.register_red()
        original = list(participation.players.values_list("pk", "user_id", "role"))
        with patch("competitions.services.deploy_competition_services", side_effect=RuntimeError("mock failure")):
            with self.assertLogs("competitions.services", level="ERROR"):
                with self.assertRaises(ValidationError):
                    start_competition(self.competition, self.organizer)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, "registration")
        self.assertEqual(self.competition.launch_state, "failed")
        frozen = self.competition.roster_frozen_at
        with self.assertRaises(ValidationError):
            register_team(self.competition, self.blue, self.bob)
        with self.assertRaises(ValidationError):
            withdraw_team(self.competition, self.red, self.alice)
        with patch("competitions.services.deploy_competition_services"):
            start_competition(self.competition, self.organizer)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, "running")
        self.assertEqual(self.competition.roster_frozen_at, frozen)
        self.assertEqual(list(participation.players.values_list("pk", "user_id", "role")), original)

    def test_barrier_is_set_before_deployment(self):
        self.register_red()
        def deploy(current):
            current.refresh_from_db()
            self.assertFalse(current.registration_open)
            self.assertEqual(current.status, "registration")
            self.assertEqual(current.launch_state, "deploying")
            with self.assertRaises(ValidationError):
                register_team(current, self.blue, self.bob)
            with self.assertRaises(ValidationError):
                start_competition(current, self.organizer)
        with patch("competitions.services.deploy_competition_services", side_effect=deploy):
            start_competition(self.competition, self.organizer)

    def test_closed_registration_and_error_are_visible(self):
        self.start_red()
        self.client.force_login(self.bob)
        response = self.client.post(reverse("competition_register_team", args=[self.competition.pk, self.blue.pk]))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Регистрация закрыта", status_code=400)
        self.assertContains(response, "Регистрация на это соревнование закрыта", status_code=400)
        self.assertContains(response, "alice", status_code=400)


class RosterTests(LifecycleFixtures, TestCase):
    def test_invitation_conflict_message_is_plain_text_not_executable_html(self):
        self.red.name = '<script>alert("team")</script>'
        self.red.save(update_fields=["name"])
        self.register_red()
        register_team(self.competition, self.blue, self.bob)
        invitation = invite_user(self.blue, self.bob, self.alice.username)
        self.client.force_login(self.alice)
        response = self.client.post(reverse("team_invitation_accept", args=[invitation.pk]))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        self.assertContains(response, self.red.name, status_code=400)
        self.assertFalse(self.blue.memberships.filter(user=self.alice).exists())

    def test_reregistration_reuses_participation_and_deploys_only_active_teams(self):
        participation = self.register_red()
        withdraw_team(self.competition, self.red, self.alice)
        restored = register_team(self.competition, self.red, self.alice)
        self.assertEqual(restored.pk, participation.pk)
        self.assertEqual(restored.players.get().user, self.alice)
        register_team(self.competition, self.blue, self.bob)
        withdraw_team(self.competition, self.blue, self.bob)
        with patch("competitions.services.sync_team_service_status", return_value="created"):
            with patch("competitions.services.deploy_team_service") as deploy:
                start_competition(self.competition, self.organizer)
        deploy.assert_called_once()
        self.assertEqual(deploy.call_args.args[0].participation_id, participation.pk)
        self.assertEqual(TeamService.objects.count(), 1)

    def test_shared_player_blocks_second_team_atomically(self):
        TeamMembership.objects.create(team=self.blue, user=self.alice)
        self.register_red()
        with self.assertRaisesMessage(ValidationError, "alice"):
            register_team(self.competition, self.blue, self.bob)
        self.assertEqual(CompetitionParticipation.objects.count(), 1)
        self.assertEqual(PlayerParticipation.objects.count(), 1)

    def test_same_user_may_represent_another_team_in_another_competition(self):
        TeamMembership.objects.create(team=self.blue, user=self.alice)
        self.register_red()
        second = Competition.objects.create(name="Second", description="", status="registration")
        register_team(second, self.blue, self.bob)
        self.assertEqual(PlayerParticipation.objects.filter(user=self.alice).count(), 2)

    def test_invitation_conflict_rolls_back_membership_and_response(self):
        self.register_red()
        register_team(self.competition, self.blue, self.bob)
        invitation = invite_user(self.blue, self.bob, self.alice.username)
        with self.assertRaises(ValidationError):
            respond_to_invitation(invitation, self.alice, True)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, "pending")
        self.assertFalse(self.blue.memberships.filter(user=self.alice).exists())

    def test_prestart_changes_update_claims_and_captain_role(self):
        participation = self.register_red()
        invitation = invite_user(self.red, self.alice, self.charlie.username)
        respond_to_invitation(invitation, self.charlie, True)
        self.assertTrue(participation.players.filter(user=self.charlie).exists())
        transfer_captain(self.red, self.alice, self.charlie)
        self.assertEqual(participation.players.get(user=self.charlie).role, "captain")
        leave_team(self.red, self.alice)
        self.assertFalse(participation.players.filter(user=self.alice).exists())

    def test_start_blocks_accept_remove_leave_and_captain_transfer(self):
        TeamMembership.objects.create(team=self.red, user=self.charlie)
        invitation = invite_user(self.red, self.alice, self.bob.username)
        self.start_red()
        operations = (
            lambda: respond_to_invitation(invitation, self.bob, True),
            lambda: remove_member(self.red, self.alice, self.charlie),
            lambda: leave_team(self.red, self.charlie),
            lambda: transfer_captain(self.red, self.alice, self.charlie),
        )
        for operation in operations:
            with self.assertRaises(ValidationError):
                operation()
        self.assertEqual(self.red.memberships.count(), 2)

    def test_finished_snapshot_survives_later_membership_changes(self):
        TeamMembership.objects.create(team=self.red, user=self.charlie)
        participation = self.start_red()
        expected = list(participation.players.values_list("pk", "user_id", "role"))
        finish_competition(self.competition, self.organizer)
        transfer_captain(self.red, self.alice, self.charlie)
        leave_team(self.red, self.alice)
        self.assertEqual(list(participation.players.values_list("pk", "user_id", "role")), expected)

    def test_withdraw_releases_players_but_preserves_team_participation(self):
        TeamMembership.objects.create(team=self.blue, user=self.alice)
        participation = self.register_red()
        withdraw_team(self.competition, self.red, self.alice)
        participation.refresh_from_db()
        self.assertFalse(participation.is_active)
        self.assertFalse(participation.players.exists())
        register_team(self.competition, self.blue, self.bob)
        with self.assertRaises(ValidationError):
            register_team(self.competition, self.red, self.alice)
        participation.refresh_from_db()
        self.assertFalse(participation.is_active)

    def test_database_rejects_duplicate_player_and_validates_competition_link(self):
        self.register_red()
        blue = CompetitionParticipation.objects.create(team=self.blue, competition=self.competition)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PlayerParticipation.objects.create(competition=self.competition, participation=blue, user=self.alice, role="member")
        another = Competition.objects.create(name="Other", description="")
        invalid = PlayerParticipation(competition=another, participation=blue, user=self.charlie, role="member")
        with self.assertRaises(ValidationError):
            invalid.full_clean()


class OrganizerAdminTests(LifecycleFixtures, TestCase):
    def test_busy_database_in_admin_shows_retry_message_without_saving(self):
        self.client.force_login(self.organizer)
        url = reverse("admin:competitions_competition_change", args=[self.competition.pk])
        with patch("competitions.mutation.CompetitionMutationLock.objects.filter") as lock:
            lock.return_value.update.side_effect = OperationalError("database is locked")
            response = self.client.post(url, {
                "name": "Must not save", "description": "Test", "_save": "Save",
            }, follow=True)
        self.assertContains(response, "Повторите действие")
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.name, "CTF")

    def test_frozen_services_cannot_be_changed_through_admin(self):
        self.start_red()
        other = Service.objects.create(name="Other", description="")
        self.client.force_login(self.organizer)
        response = self.client.post(
            reverse("admin:competitions_competition_change", args=[self.competition.pk]),
            {"name": "CTF", "description": "Test", "services": [other.pk], "_save": "Save"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.competition.services.all()), [self.service])

    def test_admin_action_requires_post_and_csrf(self):
        from django.test import Client
        url = reverse("admin:competitions_competition_changelist")
        payload = {"action": "open_registrations", "_selected_action": [self.competition.pk]}
        self.client.force_login(self.organizer)
        self.client.get(url, payload)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, "draft")
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.organizer)
        self.assertEqual(strict.post(url, payload).status_code, 403)

    def test_admin_can_assign_captain_to_legacy_team_but_cannot_edit_membership(self):
        from .models import Team
        legacy = Team.objects.create(name="Legacy")
        for codename in ("add_teammembership", "change_teammembership", "view_teammembership"):
            self.organizer.user_permissions.add(Permission.objects.get(content_type__app_label="competitions", codename=codename))
        self.client.force_login(self.organizer)
        response = self.client.post(reverse("admin:competitions_teammembership_add"), {"team": legacy.pk, "user": self.charlie.pk, "role": "captain", "_save": "Save"})
        self.assertEqual(response.status_code, 302)
        member = legacy.memberships.get()
        self.assertEqual(member.role, "captain")
        response = self.client.post(reverse("admin:competitions_teammembership_change", args=[member.pk]), {"role": "member", "team": legacy.pk, "user": self.charlie.pk, "_save": "Save"})
        self.assertEqual(response.status_code, 403)
        member.refresh_from_db()
        self.assertEqual(member.role, "captain")

    def test_actions_use_permissions_and_status_cannot_be_posted(self):
        self.client.force_login(self.organizer)
        url = reverse("admin:competitions_competition_change", args=[self.competition.pk])
        response = self.client.post(url, {"name": "Changed", "description": "Test", "status": "running", "services": [self.service.pk], "_save": "Save"})
        self.assertEqual(response.status_code, 302)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, "draft")
        response = self.client.post(reverse("admin:competitions_competition_changelist"), {
            "action": "open_registrations", "_selected_action": [self.competition.pk],
        })
        self.assertEqual(response.status_code, 302)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, "registration")

    def test_view_only_staff_cannot_run_actions(self):
        self.alice.is_staff = True
        self.alice.save(update_fields=["is_staff"])
        self.alice.user_permissions.add(Permission.objects.get(content_type__app_label="competitions", codename="view_competition"))
        self.client.force_login(self.alice)
        with patch("competitions.services.deploy_competition_services") as deploy:
            self.client.post(reverse("admin:competitions_competition_changelist"), {"action": "start_competitions", "_selected_action": [self.competition.pk]})
            deploy.assert_not_called()
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, "draft")

    def test_admin_addition_uses_conflict_and_freeze_rules(self):
        self.organizer.user_permissions.add(Permission.objects.get(content_type__app_label="competitions", codename="add_teammembership"))
        self.register_red()
        register_team(self.competition, self.blue, self.bob)
        self.client.force_login(self.organizer)
        response = self.client.post(reverse("admin:competitions_teammembership_add"), {"team": self.blue.pk, "user": self.alice.pk, "role": "member", "_save": "Save"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.blue.memberships.filter(user=self.alice).exists())
        with patch("competitions.services.deploy_competition_services"):
            start_competition(self.competition, self.organizer)
        response = self.client.post(reverse("admin:competitions_teammembership_add"), {"team": self.red.pk, "user": self.charlie.pk, "role": "member", "_save": "Save"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.red.memberships.filter(user=self.charlie).exists())

    def test_direct_participation_and_roster_admin_edits_are_disabled(self):
        superuser = get_user_model().objects.create_superuser(username="root", password="unused")
        self.client.force_login(superuser)
        for model in ("competitionparticipation", "playerparticipation"):
            self.assertEqual(self.client.get(reverse(f"admin:competitions_{model}_add")).status_code, 403)


class ConcurrentRosterTests(LifecycleFixtures, TransactionTestCase):
    def worker(self, operation):
        try:
            operation()
            return "ok"
        except ValidationError:
            return "rejected"
        finally:
            connections.close_all()

    def test_concurrent_registration_shared_player_never_commits_two_teams(self):
        TeamMembership.objects.create(team=self.blue, user=self.alice)
        open_registration(self.competition, self.organizer)
        barrier = Barrier(2)
        def attempt(team, captain):
            barrier.wait(timeout=10)
            register_team(self.competition, team, captain)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.worker, lambda: attempt(self.red, self.alice)), pool.submit(self.worker, lambda: attempt(self.blue, self.bob))]
            results = [future.result(timeout=15) for future in futures]
        self.assertIn("ok", results)
        self.assertEqual(CompetitionParticipation.objects.filter(is_active=True).count(), 1)
        self.assertEqual(PlayerParticipation.objects.filter(competition=self.competition, user=self.alice).count(), 1)

    def test_concurrent_invitation_acceptance_cannot_claim_two_teams(self):
        self.register_red()
        register_team(self.competition, self.blue, self.bob)
        red_invite = invite_user(self.red, self.alice, self.charlie.username)
        blue_invite = invite_user(self.blue, self.bob, self.charlie.username)
        barrier = Barrier(2)
        def accept(invitation):
            barrier.wait(timeout=10)
            respond_to_invitation(invitation, self.charlie, True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.worker, lambda: accept(red_invite)), pool.submit(self.worker, lambda: accept(blue_invite))]
            results = [future.result(timeout=15) for future in futures]
        self.assertIn("ok", results)
        self.assertEqual(TeamMembership.objects.filter(user=self.charlie).count(), 1)
        self.assertEqual(PlayerParticipation.objects.filter(user=self.charlie).count(), 1)

    def test_deployment_barrier_blocks_parallel_start_registration_and_join(self):
        self.register_red()
        invitation = invite_user(self.red, self.alice, self.charlie.username)
        entered, release = Event(), Event()
        def deploy(current):
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError("test timeout")
        with patch("competitions.services.deploy_competition_services", side_effect=deploy) as mock_deploy:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.worker, lambda: start_competition(self.competition, self.organizer))
                try:
                    self.assertTrue(entered.wait(timeout=10))
                    with self.assertRaises(ValidationError):
                        register_team(self.competition, self.blue, self.bob)
                    with self.assertRaises(ValidationError):
                        respond_to_invitation(invitation, self.charlie, True)
                    with self.assertRaises(ValidationError):
                        start_competition(self.competition, self.organizer)
                finally:
                    release.set()
                self.assertEqual(future.result(timeout=15), "ok")
            mock_deploy.assert_called_once()
        self.assertEqual(PlayerParticipation.objects.count(), 1)
