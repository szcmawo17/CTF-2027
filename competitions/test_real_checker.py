import json
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .checker_http import HTTPChecker, HTTPResult, bounded_http, perform_http
from .checker_jobs import claim_job, maintain_jobs, record_result
from .checker_secrets import seal, unseal
from .checker_worker import execute_claim, run_one
from .game_services import audit_scores, finish_round, open_round, scoreboard, submit_flag
from .models import CheckerEndpoint, CheckerJob, CheckerRun, Flag, FlagMaterial, Round, ScoreEvent, TeamService
from .services import finish_competition
from .test_game_engine import GameFixtures


class HTTPFixtures(GameFixtures):
    def setUp(self):
        super().setUp()
        self.keydir = tempfile.TemporaryDirectory(prefix="ctf-checker-tests-")
        self.addCleanup(self.keydir.cleanup)
        self.keyfile = Path(self.keydir.name) / "checker.key"
        self.keyfile.write_bytes(Fernet.generate_key())
        override = override_settings(CHECKER_KEY_FILE=str(self.keyfile))
        override.enable()
        self.addCleanup(override.disable)
        self.service.runtime = "training_http"
        self.service.save()
        for n, service in enumerate(TeamService.objects.all()):
            service.container_id, service.host_port = f"test-container-{n}", 55000 + n
            service.save()
            endpoint = CheckerEndpoint(team_service=service, container_id=service.container_id, port=service.host_port)
            endpoint.token_ciphertext = seal("a" * 64, f"endpoint:{endpoint.instance_id}")
            endpoint.save()

    def open_http(self):
        return open_round(self.competition, self.organizer, number=1, backend="http")

    def plaintext(self, participation):
        flag = Flag.objects.get(team_service__participation=participation)
        return unseal(flag.material.ciphertext, f"flag:{flag.pk}")

    def drain(self, status="OK"):
        count = 0
        with patch("competitions.checker_worker.bounded_http", return_value=HTTPResult(status)):
            while run_one():
                count += 1
                self.assertLess(count, 30)
        return count


class CheckerJobTests(HTTPFixtures, TransactionTestCase):
    def test_last_result_and_scores_roll_back_together_on_storage_failure(self):
        game_round = self.open_http()
        self.drain()
        finish_round(game_round, self.organizer)
        for _ in range(3):
            record_result(claim_job(), HTTPResult("OK"))
        last = claim_job()
        with patch("competitions.game_services.ScoreEvent.objects.create", side_effect=RuntimeError("storage")):
            with self.assertRaises(RuntimeError):
                record_result(last, HTTPResult("OK"))
        self.assertEqual(CheckerJob.objects.get(pk=last.job_id).state, "running")
        self.assertFalse(ScoreEvent.objects.exists())
        self.assertFalse(CheckerRun.objects.exclude(get_status="").exists())
        self.assertTrue(record_result(last, HTTPResult("OK")))
        self.assertFalse(record_result(last, HTTPResult("OK")))
        self.assertEqual(ScoreEvent.objects.count(), 4)
        self.assertTrue(audit_scores(self.competition)["ok"])

    def test_async_full_cycle_preserves_scoring_and_no_network_in_transactions(self):
        game_round = self.open_http()
        value = self.plaintext(self.blue_p)
        expected_values = {value, self.plaintext(self.red_p)}
        self.assertFalse(submit_flag(self.competition, self.alice, value).accepted)
        self.assertEqual(CheckerJob.objects.count(), 2)
        self.assertEqual(FlagMaterial.objects.count(), 2)
        self.assertFalse(CheckerRun.objects.exclude(put_status="").exists())
        payloads = []

        def network(payload):
            self.assertFalse(connections["default"].in_atomic_block)
            payloads.append(payload)
            return HTTPResult("OK")

        with patch("competitions.checker_worker.bounded_http", side_effect=network):
            self.assertTrue(run_one())
            self.assertTrue(run_one())
        self.assertEqual({p["value"] for p in payloads}, expected_values)
        self.assertEqual(len({p["instance"] for p in payloads}), 2)
        self.assertFalse(FlagMaterial.objects.exists())
        self.assertTrue(submit_flag(self.competition, self.alice, value).accepted)
        self.assertFalse(submit_flag(self.competition, self.alice, value).accepted)
        self.assertEqual(finish_round(game_round, self.organizer).state, "checking")
        self.assertFalse(ScoreEvent.objects.exclude(kind="attack").exists())
        with self.assertRaises(ValidationError):
            finish_competition(self.competition, self.organizer)
        self.assertEqual(self.drain(), 4)
        game_round.refresh_from_db()
        self.assertEqual(game_round.state, "finished")
        self.assertEqual([r["total"] for r in scoreboard(self.competition)], [21, 1])
        self.assertTrue(audit_scores(self.competition)["ok"])
        finish_competition(self.competition, self.organizer)
        self.assertTrue(audit_scores(self.competition)["ok"])

    def test_material_is_encrypted_bound_to_flag_and_survives_worker_restart(self):
        self.open_http()
        value = self.plaintext(self.red_p)
        flag = Flag.objects.get(team_service__participation=self.red_p)
        self.assertNotIn(value, flag.material.ciphertext)
        with self.assertRaises(ValidationError):
            unseal(flag.material.ciphertext, "flag:wrong")
        claim = claim_job()
        with patch("competitions.checker_worker.bounded_http", return_value=HTTPResult("OK")) as network:
            self.assertEqual(execute_claim(claim).status, "OK")
            self.assertEqual(len(network.call_args.args[0]["value"]), 69)

    def test_missing_key_or_endpoint_rolls_back_open(self):
        with override_settings(CHECKER_KEY_FILE=""):
            with self.assertRaises(ValidationError):
                self.open_http()
        CheckerEndpoint.objects.all().delete()
        with self.assertRaises(ValidationError):
            self.open_http()
        self.assertFalse(Round.objects.exists())
        self.assertFalse(FlagMaterial.objects.exists())

    def test_bad_key_does_not_send_http(self):
        self.open_http()
        claim = claim_job()
        self.keyfile.write_bytes(Fernet.generate_key())
        with patch("competitions.checker_worker.bounded_http") as network:
            self.assertEqual(execute_claim(claim), HTTPResult("ERROR", "secret"))
            network.assert_not_called()

    def test_finish_during_put_is_rejected(self):
        game_round = self.open_http()
        claim_job()
        with self.assertRaises(ValidationError):
            finish_round(game_round, self.organizer)
        self.assertFalse(CheckerJob.objects.exclude(operation="put_flag").exists())

    def test_crash_after_claim_reclaims_same_job_with_new_lease(self):
        self.open_http()
        first = claim_job()
        job = CheckerJob.objects.get(pk=first.job_id)
        with patch("competitions.checker_jobs.timezone.now", return_value=job.lease_until + timedelta(seconds=1)):
            second = claim_job()
            self.assertEqual(first.job_id, second.job_id)
            self.assertNotEqual(first.lease, second.lease)
            self.assertFalse(record_result(first, HTTPResult("OK")))
            self.assertTrue(record_result(second, HTTPResult("OK")))
            self.assertFalse(record_result(second, HTTPResult("OK")))
        job.refresh_from_db()
        self.assertEqual(job.attempts, 2)

    def test_lost_put_result_retries_same_payload(self):
        self.open_http()
        first = claim_job()
        seen = []
        with patch("competitions.checker_worker.bounded_http", side_effect=lambda p: seen.append(p) or HTTPResult("OK")):
            execute_claim(first)  # Response lost before record_result.
            job = CheckerJob.objects.get(pk=first.job_id)
            with patch("competitions.checker_jobs.timezone.now", return_value=job.lease_until + timedelta(seconds=1)):
                second = claim_job()
                execute_claim(second)
                record_result(second, HTTPResult("OK"))
        self.assertEqual(seen[0]["value"], seen[1]["value"])
        self.assertEqual(seen[0]["flag"], seen[1]["flag"])
        self.assertEqual(seen[0]["instance"], seen[1]["instance"])

    def test_timeout_retry_is_bounded_and_not_down(self):
        self.open_http()
        job_id = None
        for _ in range(3):
            claim = claim_job()
            job_id = claim.job_id
            record_result(claim, HTTPResult("ERROR", "timeout", True))
        job = CheckerJob.objects.get(pk=job_id)
        self.assertEqual((job.state, job.attempts, job.result), ("done", 3, "ERROR"))
        self.assertEqual(job.flag.state, "rejected")
        self.assertFalse(FlagMaterial.objects.filter(flag=job.flag).exists())

    def test_expired_round_terminal_policy_no_fake_success(self):
        game_round = self.open_http()
        old_claim = claim_job()
        with patch("competitions.checker_jobs.timezone.now", return_value=game_round.expires_at + timedelta(seconds=61)):
            maintain_jobs()
            self.assertFalse(record_result(old_claim, HTTPResult("OK")))
            self.assertIsNone(claim_job())
        game_round.refresh_from_db()
        self.assertEqual(game_round.state, "finished")
        self.assertFalse(ScoreEvent.objects.exists())
        self.assertFalse(FlagMaterial.objects.exists())
        self.assertFalse(CheckerJob.objects.exclude(state__in=["done", "expired"]).exists())

    def test_worker_starts_checks_at_deadline_and_preserves_get_grace(self):
        game_round = self.open_http()
        self.drain()
        with patch("competitions.checker_jobs.timezone.now", return_value=game_round.expires_at):
            maintain_jobs()
        game_round.refresh_from_db()
        self.assertEqual(game_round.state, "checking")
        self.assertEqual(CheckerJob.objects.filter(state="pending").count(), 4)

    def test_endpoint_replacement_blocks_request_and_result(self):
        self.open_http()
        claim = claim_job()
        job = CheckerJob.objects.get(pk=claim.job_id)
        TeamService.objects.filter(pk=job.flag.team_service_id).update(host_port=12345)
        with patch("competitions.checker_worker.bounded_http") as network:
            self.assertEqual(execute_claim(claim).code, "endpoint")
            network.assert_not_called()
        record_result(claim, HTTPResult("OK"))
        job.refresh_from_db()
        self.assertEqual(job.result, "ERROR")
        self.assertEqual(job.flag.state, "rejected")

    def test_unknown_result_is_sanitized(self):
        self.open_http()
        claim = claim_job()
        record_result(claim, HTTPResult("invalid secret", "private raw error"))
        job = CheckerJob.objects.get(pk=claim.job_id)
        self.assertEqual((job.result, job.error_code), ("ERROR", "checker"))

    def test_worker_rejects_outer_transaction_before_network(self):
        self.open_http()
        with transaction.atomic(), patch("competitions.checker_worker.bounded_http") as network:
            with self.assertRaises(RuntimeError):
                run_one()
            network.assert_not_called()

    def test_job_constraint_and_checking_round_prevent_overlap(self):
        game_round = self.open_http()
        job = CheckerJob.objects.first()
        with self.assertRaises(IntegrityError), transaction.atomic():
            CheckerJob.objects.create(flag=job.flag, endpoint=job.endpoint, instance_id=job.instance_id, port=job.port, container_id=job.container_id, operation=job.operation, deadline=job.deadline)
        self.drain()
        finish_round(game_round, self.organizer)
        with self.assertRaises(ValidationError):
            open_round(self.competition, self.organizer, number=2, backend="http")

    def test_pages_jobs_and_secret_material_access(self):
        self.open_http()
        value = self.plaintext(self.blue_p)
        self.client.force_login(self.alice)
        response = self.client.get(reverse("game_detail", args=[self.competition.pk]))
        self.assertNotContains(response, value)
        self.assertEqual(self.client.get(reverse("admin:competitions_checkerjob_changelist")).status_code, 302)
        self.assertEqual(self.client.get("/admin/competitions/flagmaterial/").status_code, 302)
        self.client.force_login(self.organizer)
        self.assertEqual(self.client.get(reverse("admin:competitions_checkerjob_changelist")).status_code, 200)
        job = CheckerJob.objects.first()
        self.assertEqual(self.client.post(reverse("admin:competitions_checkerjob_change", args=[job.pk]), {"result": "OK"}).status_code, 403)
        self.assertEqual(self.client.get("/admin/competitions/flagmaterial/").status_code, 404)

    def test_two_workers_cannot_own_the_same_lease(self):
        self.open_http()
        barrier = Barrier(2)

        def claim():
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                try:
                    return claim_job()
                except ValidationError:
                    return None
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = [c for c in executor.map(lambda _: claim(), range(2)) if c]
        self.assertGreaterEqual(len(claims), 1)
        self.assertEqual(len({c.job_id for c in claims}), len(claims))


def _sleeping_checker_child(send, payload):
    import time
    time.sleep(60)


class HTTPProtocolTests(SimpleTestCase):
    def test_real_stalled_child_is_terminated_without_network(self):
        import multiprocessing
        import time
        before = {p.pid for p in multiprocessing.active_children()}
        started = time.monotonic()
        with patch("competitions.checker_http._child", _sleeping_checker_child), patch("competitions.checker_http.HTTP_BUDGET", 0.2):
            result = bounded_http({})
        self.assertEqual(result, HTTPResult("ERROR", "timeout", True))
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)

    def test_real_child_exits_without_network_for_invalid_operation(self):
        import multiprocessing
        before = {p.pid for p in multiprocessing.active_children()}
        # The operation is rejected before any socket is opened. This verifies
        # spawn/IPC/join/close on this OS without starting test infrastructure.
        result = bounded_http({"port": 55000, "instance": str(uuid.uuid4()), "token": "a" * 64,
                               "expires_at": 0, "operation": "invalid"})
        self.assertEqual(result, HTTPResult("ERROR", "operation"))
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)

    def test_timeout_terminates_child_and_closes_handles(self):
        context, process, receive, send = Mock(), Mock(), Mock(), Mock()
        context.Pipe.return_value = (receive, send)
        context.Process.return_value = process
        process.pid = 123
        process.is_alive.side_effect = [True, False, False]
        receive.poll.return_value = False
        with patch("competitions.checker_http.multiprocessing.get_context", return_value=context):
            self.assertEqual(bounded_http({}), HTTPResult("ERROR", "timeout", True))
        process.terminate.assert_called_once()
        process.close.assert_called_once()
        receive.close.assert_called_once()
        self.assertTrue(send.close.called)

    def test_unstoppable_child_stops_worker_and_closes_pipes(self):
        context, process, receive, send = Mock(), Mock(), Mock(), Mock()
        context.Pipe.return_value = (receive, send)
        context.Process.return_value = process
        process.pid, process.is_alive.return_value = 123, True
        receive.poll.return_value = False
        with patch("competitions.checker_http.multiprocessing.get_context", return_value=context):
            with self.assertRaises(RuntimeError):
                bounded_http({})
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        receive.close.assert_called_once()

    def test_success_waits_for_child_exit_and_releases_handles(self):
        context, process, receive, send = Mock(), Mock(), Mock(), Mock()
        context.Pipe.return_value = (receive, send)
        context.Process.return_value = process
        process.pid, process.is_alive.return_value = 123, False
        receive.poll.return_value = True
        receive.recv.return_value = HTTPResult("OK")
        with patch("competitions.checker_http.multiprocessing.get_context", return_value=context):
            self.assertEqual(bounded_http({}), HTTPResult("OK"))
        process.terminate.assert_not_called()
        process.close.assert_called_once()

    def test_identity_mismatch_sends_no_credentials_or_flag(self):
        response = Mock(status=200)
        response.read.return_value = b'{"instance":"wrong","proof":"wrong"}'
        connection = Mock()
        connection.getresponse.return_value = response
        with patch("competitions.checker_http.http.client.HTTPConnection", return_value=connection):
            result = perform_http({"port": 55000, "instance": str(uuid.uuid4()), "token": "a" * 64, "expires_at": 1, "operation": "put_flag", "team_service": 1, "flag": 1, "value": "secret"})
        self.assertEqual(result, HTTPResult("ERROR", "identity"))
        self.assertEqual(connection.request.call_count, 1)
        self.assertNotIn("Authorization", connection.request.call_args.args[3])
        self.assertNotIn("secret", connection.request.call_args.args[2])

    def test_connection_refused_timeout_bad_json_and_redirect(self):
        payload = {"port": 55000, "instance": str(uuid.uuid4()), "token": "a" * 64, "expires_at": 1, "operation": "check_service", "team_service": 1}
        for exception, expected in [(ConnectionRefusedError(), "DOWN"), (TimeoutError(), "ERROR")]:
            with patch("competitions.checker_http.http.client.HTTPConnection") as factory:
                factory.return_value.request.side_effect = exception
                self.assertEqual(perform_http(payload).status, expected)
        for status, body in [(200, b'not json'), (302, b'{}'), (200, b'x' * 4097)]:
            with patch("competitions.checker_http.http.client.HTTPConnection") as factory:
                factory.return_value.getresponse.return_value.status = status
                factory.return_value.getresponse.return_value.read.return_value = body
                self.assertEqual(perform_http(payload).status, "MUMBLE")

    def test_http_get_compares_returned_flag_not_expected_hash(self):
        checker = HTTPChecker(55000, str(uuid.uuid4()), "a" * 64, 1)
        with patch.object(checker, "_identify"), patch.object(checker, "_post", return_value={"value": "wrong"}):
            self.assertEqual(checker.get_flag(1, 1, "0" * 64).value, "MUMBLE")
