from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse

from .models import (
    Competition, CompetitionParticipation, Service, Team, TeamMembership, TeamService
)
from .services import start_competition


class CompetitionRegistrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.captain = User.objects.create_user(username="captain")
        cls.member = User.objects.create_user(username="member")
        cls.outsider = User.objects.create_user(username="outsider")
        cls.team = Team.objects.create(name="Red Team")
        TeamMembership.objects.create(
            team=cls.team, user=cls.captain, role="captain"
        )
        TeamMembership.objects.create(team=cls.team, user=cls.member)
        cls.competition = Competition.objects.create(
            name="Open CTF", description="Registration is open", status="registration"
        )

    def register_url(self, team=None, competition=None):
        return reverse(
            "competition_register_team",
            args=[
                (competition or self.competition).pk,
                (team or self.team).pk,
            ],
        )

    def withdraw_url(self, team=None, competition=None):
        return reverse(
            "competition_withdraw_team",
            args=[
                (competition or self.competition).pk,
                (team or self.team).pk,
            ],
        )

    def test_registration_stage_is_public_but_draft_remains_private(self):
        draft = Competition.objects.create(
            name="Private CTF", description="Hidden", status="draft"
        )
        response = self.client.get(reverse("competition_list"))
        self.assertContains(response, "Open CTF")
        self.assertNotContains(response, "Private CTF")
        self.assertEqual(
            self.client.get(reverse("competition_detail", args=[draft.pk])).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("competition_detail", args=[self.competition.pk])
            ).status_code,
            200,
        )

        organizer = get_user_model().objects.create_user(
            username="organizer", is_staff=True
        )
        organizer.user_permissions.add(
            Permission.objects.get(
                codename="view_competition", content_type__app_label="competitions"
            )
        )
        self.client.force_login(organizer)
        self.assertContains(
            self.client.get(reverse("competition_detail", args=[draft.pk])),
            "Private CTF",
        )

    def test_captain_registers_team_without_creating_services(self):
        self.client.force_login(self.captain)
        detail_url = reverse("competition_detail", args=[self.competition.pk])
        self.assertContains(self.client.get(detail_url), self.register_url())

        response = self.client.post(self.register_url())
        self.assertRedirects(response, detail_url)
        self.assertTrue(
            CompetitionParticipation.objects.filter(
                competition=self.competition, team=self.team
            ).exists()
        )
        self.assertEqual(TeamService.objects.count(), 0)
        self.assertNotContains(self.client.get(detail_url), self.register_url())
        self.assertContains(self.client.get(detail_url), self.withdraw_url())

    def test_only_captain_can_register_or_withdraw(self):
        for user in (self.member, self.outsider):
            self.client.force_login(user)
            self.assertEqual(self.client.post(self.register_url()).status_code, 403)
        self.assertFalse(CompetitionParticipation.objects.exists())

        self.client.force_login(self.captain)
        self.client.post(self.register_url())
        for user in (self.member, self.outsider):
            self.client.force_login(user)
            self.assertEqual(self.client.post(self.withdraw_url()).status_code, 403)
        self.assertEqual(CompetitionParticipation.objects.count(), 1)

    def test_duplicate_registration_is_rejected(self):
        self.client.force_login(self.captain)
        self.assertEqual(self.client.post(self.register_url()).status_code, 302)
        self.assertEqual(self.client.post(self.register_url()).status_code, 400)
        self.assertEqual(CompetitionParticipation.objects.count(), 1)

    def test_registration_requires_registration_status(self):
        self.client.force_login(self.captain)
        for status in ("draft", "running", "finished"):
            competition = Competition.objects.create(
                name=f"CTF {status}", description="Closed", status=status
            )
            self.assertEqual(
                self.client.post(self.register_url(competition=competition)).status_code,
                400,
            )
        self.assertFalse(CompetitionParticipation.objects.exists())

    def test_withdraw_before_services_are_created(self):
        self.client.force_login(self.captain)
        self.client.post(self.register_url())
        self.assertRedirects(
            self.client.post(self.withdraw_url()),
            reverse("competition_detail", args=[self.competition.pk]),
        )
        self.assertFalse(CompetitionParticipation.objects.exists())
        self.assertEqual(self.client.post(self.withdraw_url()).status_code, 400)

    def test_withdraw_protects_existing_team_services(self):
        self.client.force_login(self.captain)
        participation = CompetitionParticipation.objects.create(
            competition=self.competition, team=self.team
        )
        service = Service.objects.create(name="Service", description="Existing")
        team_service = TeamService.objects.create(
            participation=participation,
            service=service,
            container_id="existing-container-id",
            host_port=55000,
            status="running",
        )
        detail = self.client.get(
            reverse("competition_detail", args=[self.competition.pk])
        )
        self.assertNotContains(detail, self.withdraw_url())
        self.assertEqual(self.client.post(self.withdraw_url()).status_code, 400)
        team_service.refresh_from_db()
        self.assertEqual(team_service.container_id, "existing-container-id")
        self.assertEqual(team_service.host_port, 55000)
        self.assertTrue(CompetitionParticipation.objects.filter(pk=participation.pk).exists())

    @patch("competitions.models.CompetitionParticipation.delete")
    def test_withdraw_handles_service_created_after_initial_check(self, mock_delete):
        CompetitionParticipation.objects.create(
            competition=self.competition, team=self.team
        )
        mock_delete.side_effect = ProtectedError("Service appeared", set())
        self.client.force_login(self.captain)
        self.assertEqual(self.client.post(self.withdraw_url()).status_code, 400)
        self.assertEqual(CompetitionParticipation.objects.count(), 1)

    def test_closing_registration_blocks_withdrawal(self):
        self.client.force_login(self.captain)
        participation = CompetitionParticipation.objects.create(
            competition=self.competition, team=self.team
        )
        self.competition.status = "running"
        self.competition.save(update_fields=["status"])
        self.assertEqual(self.client.post(self.withdraw_url()).status_code, 400)
        self.assertTrue(CompetitionParticipation.objects.filter(pk=participation.pk).exists())

    def test_only_own_eligible_teams_are_offered(self):
        second_team = Team.objects.create(name="Blue Team")
        TeamMembership.objects.create(
            team=second_team, user=self.captain, role="captain"
        )
        CompetitionParticipation.objects.create(
            competition=self.competition, team=self.team
        )
        self.client.force_login(self.captain)
        response = self.client.get(
            reverse("competition_detail", args=[self.competition.pk])
        )
        self.assertContains(response, self.register_url(team=second_team))
        self.assertNotContains(response, self.register_url())

    def test_registration_requires_post_and_csrf(self):
        url = self.register_url()
        self.assertRedirects(self.client.get(url), reverse("login") + "?next=" + url)
        self.client.force_login(self.captain)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.get(self.withdraw_url()).status_code, 405)

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.captain)
        self.assertEqual(client.post(url).status_code, 403)
        client.get(reverse("competition_detail", args=[self.competition.pk]))
        token = client.cookies["csrftoken"].value
        self.assertRedirects(
            client.post(url, HTTP_X_CSRFTOKEN=token),
            reverse("competition_detail", args=[self.competition.pk]),
        )

    @patch("competitions.services.deploy_competition_services")
    def test_admin_can_start_after_registration(self, mock_deploy):
        start_competition(self.competition)
        self.competition.refresh_from_db()
        self.assertEqual(self.competition.status, "running")
        mock_deploy.assert_called_once_with(self.competition)
