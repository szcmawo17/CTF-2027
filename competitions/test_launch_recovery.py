import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import Mock, patch

import docker
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from .launch_recovery import diagnose_launch, request_manual_review
from .models import (
    Competition, CompetitionLaunchAttempt, CompetitionMutationLock,
    CompetitionRecoveryAction, PlayerParticipation, Service, TeamService,
)
from .services import _prepare_start, _record_launch_result, open_registration, start_competition
from .test_lifecycle import LifecycleFixtures


class RecoveryFixtures(LifecycleFixtures):
    def prepare(self):
        self.register_red()
        current, attempt, ready = _prepare_start(self.competition, self.organizer)
        self.assertTrue(ready)
        return current, attempt

    def service_row(self):
        return TeamService.objects.create(
            participation=self.competition.participations.get(), service=self.service,
            container_id="known-container", host_port=55000, status="running",
        )

    def docker_client(self, state="running"):
        client = Mock()
        container = Mock()
        container.status = state
        container.attrs = {
            "NetworkSettings": {"Ports": {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55000"}]}},
            "Config": {"Env": ["PASSWORD=never-store-this"]},
        }
        client.containers.get.return_value = container
        return client

    def unchanged_data(self):
        return {
            model.__name__: list(model.objects.order_by("pk").values())
            for model in (Competition, CompetitionLaunchAttempt, CompetitionMutationLock,
                          CompetitionRecoveryAction, PlayerParticipation, TeamService)
        }


class LaunchJournalTests(RecoveryFixtures, TestCase):
    def test_existing_running_container_is_reused_without_creation_or_restart(self):
        self.register_red()
        self.service_row()
        client = self.docker_client()
        container = client.containers.get.return_value
        container.attrs["HostConfig"] = {"PortBindings": {"5000/tcp": [{"HostIp": "127.0.0.1"}]}}
        with patch("competitions.infrastructure.get_docker_client", return_value=client):
            with patch("builtins.print"):
                start_competition(self.competition, self.organizer)
        client.containers.run.assert_not_called()
        container.start.assert_not_called()
        container.stop.assert_not_called()
        container.remove.assert_not_called()
        self.assertEqual(CompetitionLaunchAttempt.objects.get().state, "succeeded")

    def test_success_records_actor_timestamps_and_committed_attempt_before_docker(self):
        self.register_red()
        def deploy(current):
            attempt = CompetitionLaunchAttempt.objects.get()
            current.refresh_from_db()
            self.assertEqual(current.current_launch_attempt_id, attempt.pk)
            self.assertEqual(attempt.state, "in_progress")
            self.assertIsNone(attempt.finished_at)
            self.assertEqual(attempt.initiated_by, self.organizer)
            self.assertFalse(current.registration_open)
        with patch("competitions.services.deploy_competition_services", side_effect=deploy):
            start_competition(self.competition, self.organizer)
        attempt = CompetitionLaunchAttempt.objects.get()
        self.assertEqual(attempt.state, "succeeded")
        self.assertGreaterEqual(attempt.finished_at, attempt.started_at)
        with self.assertRaises(ValidationError):
            _record_launch_result(self.competition, attempt.pk)

    def test_preconditions_failure_is_logged_and_can_be_retried_before_docker(self):
        open_registration(self.competition, self.organizer)
        with patch("competitions.services.deploy_competition_services") as deploy:
            with self.assertRaises(ValidationError):
                start_competition(self.competition, self.organizer)
            deploy.assert_not_called()
        failed = CompetitionLaunchAttempt.objects.get()
        self.assertEqual(failed.state, "failed")
        self.assertIsNotNone(failed.finished_at)
        self.competition.refresh_from_db()
        self.assertTrue(self.competition.registration_open)
        from .participation_services import register_team
        register_team(self.competition, self.red, self.alice)
        with patch("competitions.services.deploy_competition_services"):
            start_competition(self.competition, self.organizer)
        self.assertEqual(CompetitionLaunchAttempt.objects.count(), 2)
        failed.refresh_from_db()
        self.assertEqual(failed.state, "failed")

    def test_partial_deployment_failure_preserves_rows_and_sanitizes_history_and_logs(self):
        self.register_red()
        self.competition.services.add(Service.objects.create(name="Second", description=""))
        players = list(PlayerParticipation.objects.values())
        def deploy(service):
            service.container_id = "container-" + str(service.pk)
            service.save(update_fields=["container_id"])
            if service.service_id != self.service.pk:
                raise RuntimeError("PASSWORD=never-store-this")
        with patch("competitions.services.sync_team_service_status", return_value="created"):
            with patch("competitions.services.deploy_team_service", side_effect=deploy):
                with patch("competitions.services.stop_team_service") as stop:
                    with self.assertLogs("competitions.services", level="ERROR") as logs:
                        with self.assertRaises(ValidationError) as error:
                            start_competition(self.competition, self.organizer)
                    self.assertEqual(stop.call_count, 2)
        attempt = CompetitionLaunchAttempt.objects.get()
        self.assertEqual(attempt.state, "review")
        self.assertEqual(TeamService.objects.count(), 2)
        self.assertEqual(list(PlayerParticipation.objects.values()), players)
        self.assertNotIn("never-store-this", attempt.message + str(error.exception) + str(logs.output))
        with patch("competitions.services.deploy_competition_services") as deploy:
            with self.assertRaises(ValidationError):
                start_competition(self.competition, self.organizer)
            deploy.assert_not_called()

    def test_process_crash_leaves_unfinished_attempt(self):
        self.register_red()
        with patch("competitions.services.deploy_competition_services", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                start_competition(self.competition, self.organizer)
        attempt = CompetitionLaunchAttempt.objects.get()
        self.assertEqual(attempt.state, "in_progress")
        self.assertIsNone(attempt.finished_at)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.launch_state, "deploying")

    def test_result_write_failure_cannot_claim_running(self):
        self.register_red()
        with patch("competitions.services.deploy_competition_services"):
            with patch("competitions.services._record_launch_result", side_effect=OperationalError("secret")):
                with self.assertLogs("competitions.services", level="ERROR"):
                    with self.assertRaises(ValidationError):
                        start_competition(self.competition, self.organizer)
        self.competition.refresh_from_db()
        self.assertEqual((self.competition.status, self.competition.launch_state), ("registration", "review"))
        self.assertEqual(CompetitionLaunchAttempt.objects.get().state, "review")

    def test_unavailable_database_still_leaves_original_committed_attempt(self):
        self.register_red()
        with patch("competitions.services.deploy_competition_services", side_effect=RuntimeError):
            with patch("competitions.services._mark_launch_uncertain", side_effect=OperationalError):
                with self.assertLogs("competitions.services", level="ERROR"):
                    with self.assertRaises(ValidationError):
                        start_competition(self.competition, self.organizer)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.launch_state, "deploying")
        self.assertEqual(CompetitionLaunchAttempt.objects.get().state, "in_progress")

    def test_attempt_and_competition_result_roll_back_together(self):
        current, attempt = self.prepare()
        with patch.object(Competition, "save", side_effect=OperationalError("write failed")):
            with self.assertRaises(OperationalError):
                _record_launch_result(current, attempt.pk)
        attempt.refresh_from_db()
        current.refresh_from_db()
        self.assertEqual(attempt.state, "in_progress")
        self.assertEqual(current.launch_state, "deploying")

    def test_old_attempt_cannot_overwrite_new_attempt(self):
        open_registration(self.competition, self.organizer)
        with self.assertRaises(ValidationError):
            start_competition(self.competition, self.organizer)
        old = CompetitionLaunchAttempt.objects.get()
        from .participation_services import register_team
        register_team(self.competition, self.red, self.alice)
        current, new, _ = _prepare_start(self.competition, self.organizer)
        with self.assertRaises(ValidationError):
            _record_launch_result(current, old.pk)
        self.assertEqual(CompetitionLaunchAttempt.objects.get(pk=new.pk).state, "in_progress")
        _record_launch_result(current, new.pk)

    def test_outer_transaction_cannot_hold_database_lock_during_docker(self):
        self.register_red()
        with patch("competitions.services.deploy_competition_services") as deploy:
            with transaction.atomic(), self.assertRaises(RuntimeError):
                start_competition(self.competition, self.organizer)
            deploy.assert_not_called()
        self.assertFalse(CompetitionLaunchAttempt.objects.exists())

    def test_database_prevents_two_unresolved_attempts(self):
        self.prepare()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CompetitionLaunchAttempt.objects.create(competition=self.competition, initiated_by=self.organizer)


class LaunchDiagnosisTests(RecoveryFixtures, TestCase):
    def test_manual_review_keeps_existing_team_service_records(self):
        self.prepare()
        self.service_row()
        before = list(TeamService.objects.values())
        with patch("competitions.launch_recovery.docker.from_env", return_value=self.docker_client()):
            _, token = diagnose_launch(self.competition, self.organizer)
        request_manual_review(self.competition, self.organizer, token)
        self.assertEqual(list(TeamService.objects.values()), before)

    def test_inspection_is_read_only_and_detects_state_and_port_mismatch(self):
        self.prepare()
        self.service_row()
        client = self.docker_client("exited")
        client.containers.get.return_value.attrs["NetworkSettings"]["Ports"]["5000/tcp"][0]["HostIp"] = "0.0.0.0"
        before = self.unchanged_data()
        with patch("competitions.launch_recovery.docker.from_env", return_value=client):
            report, _ = diagnose_launch(self.competition, self.organizer)
        self.assertEqual(self.unchanged_data(), before)
        item = report["containers"][0]
        self.assertEqual(item["state"], "exited")
        self.assertFalse(item["status_matches"])
        self.assertFalse(item["localhost_port_matches"])
        self.assertNotIn("never-store-this", json.dumps(report))
        self.assertEqual([str(call) for call in client.method_calls], ["call.containers.get('known-container')", "call.close()"])
        self.assertEqual(client.containers.get.return_value.method_calls, [])

    def test_missing_container_and_unavailable_docker_are_safe_diagnostics(self):
        self.prepare()
        row = self.service_row()
        for error, expected in ((docker.errors.NotFound("secret"), "missing"), (RuntimeError("secret"), "unavailable")):
            client = self.docker_client()
            client.containers.get.side_effect = error
            with patch("competitions.launch_recovery.docker.from_env", return_value=client):
                report, _ = diagnose_launch(self.competition, self.organizer)
            self.assertEqual(report["containers"][0]["state"], expected)
            self.assertNotIn("secret", json.dumps(report))
            row.refresh_from_db()
            self.assertEqual(row.status, "running")

    def test_missing_service_record_and_legacy_attempt_are_reported_without_docker(self):
        self.register_red()
        Competition.objects.filter(pk=self.competition.pk).update(launch_state="deploying")
        with patch("competitions.launch_recovery.docker.from_env") as docker_client:
            report, token = diagnose_launch(self.competition, self.organizer)
            docker_client.assert_not_called()
        self.assertIsNone(report["database"]["attempt"])
        self.assertEqual(len(report["missing_services"]), 1)
        event = request_manual_review(self.competition, self.organizer, token)
        self.assertIsNone(event.attempt_id)
        self.assertFalse(CompetitionLaunchAttempt.objects.exists())

    def test_permissions_checked_before_docker(self):
        with patch("competitions.launch_recovery.docker.from_env") as docker_client:
            with self.assertRaises(PermissionDenied):
                diagnose_launch(self.competition, self.alice)
            with self.assertRaises(PermissionDenied):
                request_manual_review(self.competition, self.alice, "")
            docker_client.assert_not_called()

    def test_changed_database_during_diagnosis_invalidates_report(self):
        self.prepare()
        row = self.service_row()
        client = self.docker_client()
        container = client.containers.get.return_value
        def inspect(_):
            TeamService.objects.filter(pk=row.pk).update(status="stopped")
            return container
        client.containers.get.side_effect = inspect
        with patch("competitions.launch_recovery.docker.from_env", return_value=client):
            report, token = diagnose_launch(self.competition, self.organizer)
        self.assertFalse(report["database_unchanged"])
        with self.assertRaises(ValidationError):
            request_manual_review(self.competition, self.organizer, token)

    def test_manual_review_preserves_roster_blocks_old_result_and_is_single_use(self):
        current, attempt = self.prepare()
        before = list(PlayerParticipation.objects.values())
        report, token = diagnose_launch(current, self.organizer)
        with patch("competitions.launch_recovery.docker.from_env") as docker_client:
            request_manual_review(current, self.organizer, token)
            docker_client.assert_not_called()
        self.assertEqual(list(PlayerParticipation.objects.values()), before)
        attempt.refresh_from_db()
        self.assertEqual(attempt.state, "review")
        self.assertIsNone(attempt.finished_at)
        self.assertFalse(report["process_stopped_proven"])
        for operation in (
            lambda: request_manual_review(current, self.organizer, token),
            lambda: _record_launch_result(current, attempt.pk),
            lambda: start_competition(current, self.organizer),
        ):
            with self.assertRaises(ValidationError):
                operation()
        _, new_token = diagnose_launch(current, self.organizer)
        with self.assertRaises(ValidationError):
            request_manual_review(current, self.organizer, new_token)
        self.assertEqual(CompetitionRecoveryAction.objects.count(), 1)

    def test_stale_diagnosis_after_success_cannot_change_result(self):
        current, attempt = self.prepare()
        _, token = diagnose_launch(current, self.organizer)
        _record_launch_result(current, attempt.pk)
        with self.assertRaises(ValidationError):
            request_manual_review(current, self.organizer, token)
        self.assertFalse(CompetitionRecoveryAction.objects.exists())

    def test_tokens_expire_are_signed_and_are_bound_to_actor_and_competition(self):
        self.prepare()
        _, token = diagnose_launch(self.competition, self.organizer)
        with self.assertRaises(ValidationError):
            request_manual_review(self.competition, self.organizer, token + "tampered")
        with patch("django.core.signing.time.time", return_value=time.time() + 301):
            with self.assertRaises(ValidationError):
                request_manual_review(self.competition, self.organizer, token)
        other = get_user_model().objects.create_superuser(username="other-admin")
        with self.assertRaises(PermissionDenied):
            request_manual_review(self.competition, other, token)
        other_competition = Competition.objects.create(name="Other", description="")
        with self.assertRaises(ValidationError):
            request_manual_review(other_competition, self.organizer, token)


class RecoveryAdminTests(RecoveryFixtures, TestCase):
    def test_infrastructure_admin_actions_are_blocked_during_launch_and_review(self):
        self.prepare()
        row = self.service_row()
        admin_user = get_user_model().objects.create_superuser(username="infra-admin")
        self.client.force_login(admin_user)
        url = reverse("admin:competitions_teamservice_changelist")
        for state in ("deploying", "failed", "review"):
            Competition.objects.filter(pk=self.competition.pk).update(launch_state=state)
            for action, function in (("restart_services", "restart_team_service"), ("stop_services", "stop_team_service")):
                with patch("competitions.admin." + function) as operation:
                    response = self.client.post(url, {"action": action, "_selected_action": [row.pk]})
                    self.assertEqual(response.status_code, 302)
                    operation.assert_not_called()

    def test_admin_get_never_inspects_docker_and_post_requires_csrf_and_rights(self):
        url = reverse("admin:competitions_launch_diagnostics", args=[self.competition.pk])
        self.client.force_login(self.organizer)
        with patch("competitions.admin.diagnose_launch") as diagnose:
            self.assertEqual(self.client.get(url).status_code, 200)
            diagnose.assert_not_called()
            strict = Client(enforce_csrf_checks=True)
            strict.force_login(self.organizer)
            self.assertEqual(strict.post(url, {"operation": "diagnose"}).status_code, 403)
        self.alice.is_staff = True
        self.alice.save(update_fields=["is_staff"])
        self.alice.user_permissions.add(Permission.objects.get(codename="view_competition", content_type__app_label="competitions"))
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"operation": "diagnose"}).status_code, 403)

    def test_admin_diagnosis_and_explicit_action_create_readonly_history(self):
        self.prepare()
        self.client.force_login(self.organizer)
        url = reverse("admin:competitions_launch_diagnostics", args=[self.competition.pk])
        response = self.client.post(url, {"operation": "diagnose"})
        self.assertContains(response, "Зафиксировать ручное вмешательство")
        token = response.context["diagnosis_token"]
        self.assertEqual(self.client.post(url, {"operation": "manual_review", "diagnosis_token": token}).status_code, 302)
        self.assertEqual(CompetitionRecoveryAction.objects.count(), 1)
        for model in ("competitionlaunchattempt", "competitionrecoveryaction"):
            self.assertEqual(self.client.get(reverse(f"admin:competitions_{model}_changelist")).status_code, 200)
            self.assertEqual(self.client.get(reverse(f"admin:competitions_{model}_add")).status_code, 403)


class RecoveryConcurrencyTests(RecoveryFixtures, TransactionTestCase):
    def worker(self, operation):
        try:
            operation()
            return "ok"
        except ValidationError:
            return "rejected"
        finally:
            connections.close_all()

    def test_two_organizers_cannot_apply_two_recovery_actions(self):
        self.prepare()
        _, token1 = diagnose_launch(self.competition, self.organizer)
        _, token2 = diagnose_launch(self.competition, self.organizer)
        barrier = Barrier(2)
        def apply(token):
            barrier.wait(timeout=10)
            request_manual_review(self.competition, self.organizer, token)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.worker, lambda: apply(token1)), pool.submit(self.worker, lambda: apply(token2))]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(sorted(results), ["ok", "rejected"])
        self.assertEqual(CompetitionRecoveryAction.objects.count(), 1)

    def test_live_worker_cannot_overwrite_manual_review_or_enable_retry(self):
        self.register_red()
        entered, release = Event(), Event()
        def deploy(current):
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError("test timeout")
        with patch("competitions.services.deploy_competition_services", side_effect=deploy) as deploy_mock:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self.worker, lambda: start_competition(self.competition, self.organizer))
                try:
                    self.assertTrue(entered.wait(timeout=10))
                    _, token = diagnose_launch(self.competition, self.organizer)
                    request_manual_review(self.competition, self.organizer, token)
                    with self.assertRaises(ValidationError):
                        start_competition(self.competition, self.organizer)
                finally:
                    with self.assertLogs("competitions.services", level="ERROR"):
                        release.set()
                        self.assertEqual(future.result(timeout=15), "rejected")
            deploy_mock.assert_called_once()
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.launch_state, "review")
        self.assertEqual(self.competition.status, "registration")
