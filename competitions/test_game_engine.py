from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connections, transaction
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.views.debug import SafeExceptionReporterFilter

from .checkers import CheckerStatus, MockChecker, flag_digest
from .game_services import audit_scores, finish_round, open_round, scoreboard, submit_flag
from .models import (
    Capture, CheckerRun, Competition, CompetitionParticipation, Flag,
    MockFlagStorage, PlayerParticipation, Round, ScoreEvent, Service,
    SubmissionWindow, TeamMembership, TeamService,
)
from .participation_services import register_team
from .services import create_team_services, finish_competition, open_registration, start_competition
from .test_lifecycle import LifecycleFixtures


class GameFixtures(LifecycleFixtures):
    def setUp(self):
        super().setUp()
        open_registration(self.competition, self.organizer)
        self.red_p = register_team(self.competition, self.red, self.alice)
        self.blue_p = register_team(self.competition, self.blue, self.bob)
        with patch("competitions.services.deploy_competition_services", side_effect=create_team_services):
            start_competition(self.competition, self.organizer)
        self.values = {}

    def open(self, number=1):
        original = MockChecker.put_flag

        def remember(checker, service_id, flag_id, value):
            # Test-only interception at the checker boundary; no production export.
            self.values[flag_id] = value
            return original(checker, service_id, flag_id, value)

        with patch.object(MockChecker, "put_flag", remember):
            return open_round(self.competition, self.organizer, number=number)

    def value_for(self, participation):
        flag = Flag.objects.filter(team_service__participation=participation).latest("pk")
        return self.values[flag.pk]


class GameEngineTests(GameFixtures, TestCase):
    def test_score_audit_reconstructs_history_and_detects_missing_event(self):
        game_round = self.open()
        submit_flag(self.competition, self.alice, self.value_for(self.blue_p))
        self.assertTrue(audit_scores(self.competition)["ok"])
        finish_round(game_round, self.organizer)
        self.assertTrue(audit_scores(self.competition)["ok"])
        ScoreEvent.objects.filter(kind="attack").delete()
        self.assertEqual(audit_scores(self.competition), {"ok": False, "missing": 1, "unexpected": 0})

    def test_collision_rolls_back_round_instead_of_reusing_flag(self):
        with patch("competitions.game_services.secrets.token_hex", return_value="a" * 64):
            with self.assertRaises(ValidationError):
                self.open()
        self.assertFalse(Round.objects.exists())
        self.assertFalse(MockFlagStorage.objects.exists())

    def test_full_round_two_teams_reproducible_score_and_preserved_after_finish(self):
        game_round = self.open()
        self.assertTrue(submit_flag(self.competition, self.alice, self.value_for(self.blue_p)).accepted)
        finish_round(game_round, self.organizer)
        self.assertEqual([(r["team"].pk, r["attack"], r["defense"], r["availability"], r["total"]) for r in scoreboard(self.competition)], [
            (self.red.pk, 10, 10, 1, 21), (self.blue.pk, 0, 0, 1, 1),
        ])
        history = list(ScoreEvent.objects.values())
        finish_competition(self.competition, self.organizer)
        self.assertEqual(history, list(ScoreEvent.objects.values()))
        self.assertEqual(scoreboard(self.competition)[0]["total"], 21)
        self.assertTrue(all(run.get_status == "OK" for run in CheckerRun.objects.all()))

    def test_unique_flags_across_teams_services_and_rounds_only_hashes_stored(self):
        other = Service.objects.create(name="Other", description="")
        self.competition.services.add(other)  # Fixture only; production admin freezes this.
        create_team_services(self.competition)
        first = self.open()
        finish_round(first, self.organizer)
        self.open(2)
        self.assertEqual(len(self.values), 8)
        self.assertEqual(len(set(self.values.values())), 8)
        self.assertEqual(Flag.objects.values("digest").distinct().count(), 8)
        for flag in Flag.objects.all():
            self.assertEqual(flag.digest, flag_digest(self.values[flag.pk]))
            self.assertNotEqual(flag.digest, self.values[flag.pk])
            self.assertRegex(self.values[flag.pk], r"^CTF\{[0-9a-f]{64}\}$")
        self.assertEqual(MockFlagStorage.objects.count(), 8)

    def test_open_replay_skip_and_overlap_rejected(self):
        with self.assertRaises(ValidationError):
            self.open(2)
        game_round = self.open()
        for number in (1, 2):
            with self.assertRaises(ValidationError):
                self.open(number)
        finish_round(game_round, self.organizer)
        with self.assertRaises(ValidationError):
            self.open(1)
        self.assertEqual(self.open(2).number, 2)

    def test_finished_round_cannot_be_changed(self):
        game_round = self.open()
        finish_round(game_round, self.organizer)
        before = list(CheckerRun.objects.values()), list(ScoreEvent.objects.values())
        with self.assertRaises(ValidationError):
            finish_round(game_round, self.organizer)
        self.assertFalse(submit_flag(self.competition, self.alice, self.value_for(self.blue_p)).accepted)
        self.assertEqual(before, (list(CheckerRun.objects.values()), list(ScoreEvent.objects.values())))

    def test_competition_finish_requires_round_close(self):
        self.open()
        with self.assertRaises(ValidationError):
            finish_competition(self.competition, self.organizer)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, "running")

    def test_open_requires_verified_frozen_roster_two_teams_and_exact_services(self):
        for changes in ({"roster_verified": False}, {"roster_frozen_at": None}, {"status": "registration"}, {"launch_state": "review"}):
            with transaction.atomic():
                Competition.objects.filter(pk=self.competition.pk).update(**changes)
                with self.assertRaises(ValidationError):
                    self.open()
                transaction.set_rollback(True)
        self.blue_p.is_active = False
        self.blue_p.save()
        with self.assertRaises(ValidationError):
            self.open()
        self.blue_p.is_active = True
        self.blue_p.save()
        TeamService.objects.filter(participation=self.blue_p).delete()
        with self.assertRaises(ValidationError):
            self.open()
        self.assertFalse(Round.objects.exists())

    def test_open_rejects_empty_or_inconsistent_snapshot(self):
        self.blue_p.players.all().delete()
        with self.assertRaises(ValidationError):
            self.open()
        self.assertFalse(Flag.objects.exists())

    def test_duration_validated_by_service(self):
        for duration in (0, 29, 3601, "300", True):
            with self.assertRaises(ValidationError):
                open_round(self.competition, self.organizer, number=1, duration=duration)

    def test_round_operations_require_organizer(self):
        for actor in (self.alice, AnonymousUser(), None):
            with self.assertRaises(PermissionDenied):
                open_round(self.competition, actor, number=1)
        game_round = self.open()
        with self.assertRaises(PermissionDenied):
            finish_round(game_round, self.alice)

    def test_self_unknown_and_duplicate_flags_share_rejection(self):
        self.open()
        own = submit_flag(self.competition, self.alice, self.value_for(self.red_p))
        unknown = submit_flag(self.competition, self.alice, "CTF{" + "0" * 64 + "}")
        success = submit_flag(self.competition, self.alice, self.value_for(self.blue_p))
        duplicate = submit_flag(self.competition, self.alice, self.value_for(self.blue_p))
        self.assertTrue(success.accepted)
        self.assertFalse(own.accepted or unknown.accepted or duplicate.accepted)
        self.assertEqual(own.message, unknown.message)
        self.assertEqual(own.message, duplicate.message)
        self.assertEqual(Capture.objects.count(), 1)
        self.assertEqual(ScoreEvent.objects.filter(kind="attack").count(), 1)

    def test_expiry_exact_boundary_and_old_flags_in_next_round(self):
        game_round = self.open()
        value = self.value_for(self.blue_p)
        with patch("competitions.game_services.timezone.now", return_value=game_round.expires_at):
            self.assertFalse(submit_flag(self.competition, self.alice, value).accepted)
            finish_round(game_round, self.organizer)
        self.open(2)
        self.assertFalse(submit_flag(self.competition, self.alice, value).accepted)

    def test_uninstalled_and_issued_flags_cannot_be_submitted(self):
        self.open()
        flag = Flag.objects.get(team_service__participation=self.blue_p)
        for state in ("issued", "rejected"):
            Flag.objects.filter(pk=flag.pk).update(state=state)
            self.assertFalse(submit_flag(self.competition, self.alice, self.values[flag.pk]).accepted)

    def test_auth_frozen_player_and_active_participation_are_required(self):
        self.open()
        value = self.value_for(self.blue_p)
        TeamMembership.objects.create(team=self.red, user=self.charlie)
        for actor in (AnonymousUser(), self.charlie):
            with self.assertRaises(PermissionDenied):
                submit_flag(self.competition, actor, value)
        self.alice.is_active = False
        with self.assertRaises(PermissionDenied):
            submit_flag(self.competition, self.alice, value)
        self.alice.is_active = True
        CompetitionParticipation.objects.filter(pk=self.red_p.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            submit_flag(self.competition, self.alice, value)
        self.assertFalse(Capture.objects.exists())

    def test_foreign_competition_flag_rejected(self):
        self.open()
        other = Competition.objects.create(name="Other", status="running")
        p = CompetitionParticipation.objects.create(competition=other, team=self.red)
        PlayerParticipation.objects.create(competition=other, participation=p, user=self.alice, role="captain")
        self.assertFalse(submit_flag(other, self.alice, self.value_for(self.blue_p)).accepted)

    def test_finished_competition_refuses_submissions_and_new_rounds(self):
        game_round = self.open()
        finish_round(game_round, self.organizer)
        finish_competition(self.competition, self.organizer)
        with self.assertRaises(ValidationError):
            submit_flag(self.competition, self.alice, self.value_for(self.blue_p))
        with self.assertRaises(ValidationError):
            self.open(2)

    def test_rate_limit_counts_bad_attempts_and_resets_after_minute(self):
        self.open()
        for _ in range(30):
            self.assertFalse(submit_flag(self.competition, self.alice, "bad").limited)
        self.assertTrue(submit_flag(self.competition, self.alice, self.value_for(self.blue_p)).limited)
        self.assertEqual(SubmissionWindow.objects.get(participation=self.red_p).count, 30)
        future = timezone.now() + timedelta(seconds=61)
        with patch("competitions.game_services.timezone.now", return_value=future):
            self.assertTrue(submit_flag(self.competition, self.alice, self.value_for(self.blue_p)).accepted)

    def test_team_players_share_rate_limit_and_capture(self):
        PlayerParticipation.objects.create(competition=self.competition, participation=self.red_p, user=self.charlie, role="member")
        self.open()
        value = self.value_for(self.blue_p)
        self.assertTrue(submit_flag(self.competition, self.alice, value).accepted)
        self.assertFalse(submit_flag(self.competition, self.charlie, value).accepted)
        self.assertEqual(SubmissionWindow.objects.get(participation=self.red_p).count, 2)

    def test_put_down_is_not_successful_installation(self):
        with patch.object(MockChecker, "put_flag", return_value=CheckerStatus.DOWN):
            game_round = self.open()
        self.assertFalse(Flag.objects.exclude(state="rejected").exists())
        self.assertFalse(MockFlagStorage.objects.exists())
        finish_round(game_round, self.organizer)
        self.assertFalse(ScoreEvent.objects.filter(kind="defense").exists())
        self.assertTrue(all(r["availability"] == 1 for r in scoreboard(self.competition)))

    def test_down_service_and_lost_flags_have_distinct_results(self):
        game_round = self.open()
        MockFlagStorage.objects.all().delete()  # Simulate flag loss, not infrastructure.
        with patch.object(MockChecker, "check_service", return_value=CheckerStatus.DOWN):
            finish_round(game_round, self.organizer)
        self.assertTrue(all(r.get_status == "MUMBLE" and r.service_status == "DOWN" for r in CheckerRun.objects.all()))
        self.assertFalse(ScoreEvent.objects.exists())

    def test_invalid_result_and_exception_become_error_without_raw_text(self):
        game_round = self.open()
        with patch.object(MockChecker, "get_flag", return_value="OK"), patch.object(MockChecker, "check_service", side_effect=RuntimeError("private checker payload")):
            finish_round(game_round, self.organizer)
        self.assertTrue(all(r.get_status == "ERROR" and r.service_status == "ERROR" for r in CheckerRun.objects.all()))
        self.assertNotIn("private", str(list(CheckerRun.objects.values())))
        self.assertFalse(ScoreEvent.objects.exists())

    @override_settings(DEBUG=False)
    def test_score_write_failure_rolls_back_capture(self):
        self.open()
        value = self.value_for(self.blue_p)
        with patch("competitions.game_services.ScoreEvent.objects.create", side_effect=RuntimeError("storage")):
            try:
                submit_flag(self.competition, self.alice, value)
            except RuntimeError as error:
                reporter_filter = SafeExceptionReporterFilter()
                request = RequestFactory().get("/")
                trace = error.__traceback__
                checked = 0
                while trace:
                    filename = trace.tb_frame.f_code.co_filename.replace("\\", "/")
                    if filename.endswith(("/competitions/game_services.py", "/competitions/mutation.py")):
                        variables = dict(reporter_filter.get_traceback_frame_variables(request, trace.tb_frame))
                        self.assertNotIn(value, str(variables))
                        checked += 1
                    trace = trace.tb_next
                self.assertGreaterEqual(checked, 2)
            else:
                self.fail("Expected a storage failure")
        self.assertFalse(Capture.objects.exists())
        self.assertTrue(submit_flag(self.competition, self.alice, self.value_for(self.blue_p)).accepted)

    def test_finish_failure_rolls_back_results_and_allows_safe_retry(self):
        game_round = self.open()
        with patch("competitions.game_services.ScoreEvent.objects.create", side_effect=RuntimeError("storage")):
            with self.assertRaises(RuntimeError):
                finish_round(game_round, self.organizer)
        game_round.refresh_from_db()
        self.assertEqual(game_round.state, "open")
        self.assertFalse(CheckerRun.objects.exclude(get_status="").exists())
        finish_round(game_round, self.organizer)
        self.assertEqual(ScoreEvent.objects.count(), 4)

    def test_database_uniqueness_and_state_constraints(self):
        game_round = self.open()
        flag = Flag.objects.get(team_service__participation=self.blue_p)
        submit_flag(self.competition, self.alice, self.value_for(self.blue_p))
        capture = Capture.objects.get()
        actions = [
            lambda: Round.objects.create(competition=self.competition, number=2, started_at=game_round.started_at, expires_at=game_round.expires_at, state="open"),
            lambda: Round.objects.filter(pk=game_round.pk).update(state="finished"),
            lambda: Flag.objects.create(round=game_round, team_service=flag.team_service, digest="1" * 64, state="issued"),
            lambda: Flag.objects.create(round=game_round, team_service=TeamService.objects.get(participation=self.red_p), digest=flag.digest, state="issued"),
            lambda: Capture.objects.create(flag=flag, attacker=self.red_p, submitted_by=capture.submitted_by),
            lambda: ScoreEvent.objects.create(flag=flag, participation=self.red_p, capture=capture, kind="attack", points=10),
            lambda: ScoreEvent.objects.create(flag=flag, participation=self.blue_p, kind="defense", points=999),
        ]
        for action in actions:
            with self.assertRaises(IntegrityError), transaction.atomic():
                action()


class GameHTTPTests(GameFixtures, TestCase):
    def test_anonymous_get_post_and_outsider_access(self):
        self.open()
        detail = reverse("game_detail", args=[self.competition.pk])
        submit = reverse("game_submit_flag", args=[self.competition.pk])
        self.assertEqual(self.client.get(detail).status_code, 302)
        self.assertEqual(self.client.post(submit, {"flag": self.value_for(self.blue_p)}).status_code, 302)
        self.client.force_login(self.charlie)
        self.assertEqual(self.client.get(detail).status_code, 403)
        self.assertEqual(self.client.post(submit, {"flag": self.value_for(self.blue_p)}).status_code, 403)

    def test_operations_post_only_csrf_and_organizer_permissions(self):
        game_round = self.open()
        urls = [reverse("game_open_round", args=[self.competition.pk]), reverse("game_finish_round", args=[self.competition.pk, game_round.pk]), reverse("game_submit_flag", args=[self.competition.pk])]
        self.client.force_login(self.alice)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.organizer)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 405)
            self.assertEqual(strict.post(url, {"number": 2}).status_code, 403)
        for url in urls[:2]:
            self.assertEqual(self.client.post(url, {"number": 2}).status_code, 403)

    def test_pages_never_expose_flags_hashes_or_other_team_checker_results(self):
        self.open()
        CheckerRun.objects.filter(flag__team_service__participation=self.blue_p).update(put_status="DOWN")
        self.client.force_login(self.alice)
        response = self.client.get(reverse("game_detail", args=[self.competition.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["own_results"]), 1)
        self.assertEqual(response.context["own_results"][0]["put_status"], "OK")
        scores = self.client.get(reverse("game_scoreboard", args=[self.competition.pk]))
        for flag in Flag.objects.all():
            for page in (response, scores):
                self.assertNotContains(page, self.values[flag.pk])
                self.assertNotContains(page, flag.digest)

    def test_submission_result_no_echo_and_http_rate_limit(self):
        self.open()
        self.client.force_login(self.alice)
        url = reverse("game_submit_flag", args=[self.competition.pk])
        value = self.value_for(self.blue_p)
        response = self.client.post(url, {"flag": value}, follow=True)
        self.assertContains(response, "Флаг принят")
        self.assertNotContains(response, value)
        SubmissionWindow.objects.filter(participation=self.red_p).update(count=30)
        response = self.client.post(url, {"flag": value})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "60")
        self.assertNotContains(response, value, status_code=429)

    def test_organizer_can_run_cycle_and_readonly_admin_rejects_changes(self):
        self.client.force_login(self.organizer)
        self.assertEqual(self.client.post(reverse("game_open_round", args=[self.competition.pk]), {"number": 1}).status_code, 302)
        game_round = Round.objects.get()
        admin_url = reverse("admin:competitions_round_change", args=[game_round.pk])
        self.assertEqual(self.client.get(admin_url).status_code, 200)
        self.assertEqual(self.client.post(admin_url, {"state": "finished"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("game_finish_round", args=[self.competition.pk, game_round.pk])).status_code, 302)
        game_round.refresh_from_db()
        self.assertEqual(game_round.state, "finished")


class GameConcurrencyTests(GameFixtures, TransactionTestCase):
    def run_together(self, operations):
        barrier = Barrier(len(operations))

        def run(operation):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                try:
                    return operation()
                except ValidationError:
                    return "busy_or_rejected"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(operations)) as executor:
            return list(executor.map(run, operations))

    def test_parallel_submissions_never_award_twice(self):
        self.open()
        value = self.value_for(self.blue_p)
        results = self.run_together([lambda: submit_flag(self.competition, self.alice, value)] * 2)
        # SQLite can reject a busy writer; an explicit retry is safe.
        if not Capture.objects.exists():
            self.assertTrue(submit_flag(self.competition, self.alice, value).accepted)
        self.assertLessEqual(sum(getattr(r, "accepted", False) for r in results), 1)
        self.assertEqual(Capture.objects.count(), 1)
        self.assertEqual(ScoreEvent.objects.filter(kind="attack").count(), 1)
        self.assertFalse(submit_flag(self.competition, self.alice, value).accepted)

    def test_parallel_open_creates_one_round_and_one_set_of_flags(self):
        self.run_together([lambda: open_round(self.competition, self.organizer, number=1)] * 2)
        if not Round.objects.exists():
            self.open()
        self.assertEqual(Round.objects.count(), 1)
        self.assertEqual(Flag.objects.count(), 2)

    def test_close_and_capture_produce_consistent_defense(self):
        game_round = self.open()
        value = self.value_for(self.blue_p)
        self.run_together([
            lambda: submit_flag(self.competition, self.alice, value),
            lambda: finish_round(game_round, self.organizer),
        ])
        game_round.refresh_from_db()
        if game_round.state == "open":
            finish_round(game_round, self.organizer)
        captured = Capture.objects.exists()
        self.assertEqual(ScoreEvent.objects.filter(participation=self.blue_p, kind="defense").count(), 0 if captured else 1)
        self.assertFalse(submit_flag(self.competition, self.alice, value).accepted)
