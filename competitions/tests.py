from unittest.mock import Mock, patch
from django.test import SimpleTestCase, TestCase
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse
from .models import (
    Competition, CompetitionParticipation, Team, TeamMembership, Service, TeamService
)
import docker

from .infrastructure import (
    validate_container_ports,
    restart_team_service,
    sync_team_service_status,
)
from .services import create_team_services, deploy_competition_services


class DockerPortSafetyTests(SimpleTestCase):
    def make_container(self, ip):
        container = Mock()
        container.status = "exited"
        container.attrs = {
            "HostConfig": {
                "PortBindings": {
                    "5000/tcp": [
                        {"HostIp": ip, "HostPort": "55000"}
                    ]
                }
            }
        }
        return container

    def test_localhost_is_allowed(self):
        container = self.make_container("127.0.0.1")

        validate_container_ports(container)

        container.reload.assert_called_once()

    def test_public_port_is_rejected(self):
        container = self.make_container("0.0.0.0")

        with self.assertRaises(RuntimeError):
            validate_container_ports(container)

    @patch("competitions.infrastructure.get_docker_client")
    def test_restart_rejects_public_port(self, mock_client):
        container = self.make_container("0.0.0.0")
        mock_client.return_value.containers.get.return_value = container

        team_service = Mock(container_id="test-container")

        with self.assertRaises(RuntimeError):
            restart_team_service(team_service)

        container.start.assert_not_called()
        team_service.save.assert_not_called()

    @patch("competitions.infrastructure.get_docker_client")
    def test_sync_stopped_container(self, mock_client):
        container = Mock()
        container.status = "exited"
        mock_client.return_value.containers.get.return_value = container

        service = Mock(container_id="test-id", status="running")

        result = sync_team_service_status(service)

        self.assertEqual(result, "stopped")
        self.assertEqual(service.status, "stopped")
        service.save.assert_called_once_with(
            update_fields=["status"]
        )

    @patch("competitions.infrastructure.get_docker_client")
    def test_sync_missing_container(self, mock_client):
        mock_client.return_value.containers.get.side_effect = (
            docker.errors.NotFound("Container not found")
        )

        service = Mock(container_id="missing-id", status="running")

        result = sync_team_service_status(service)

        self.assertEqual(result, "missing")
        self.assertEqual(service.status, "missing")
        service.save.assert_called_once_with(
            update_fields=["status"]
        )

class CompetitionDeploymentTests(SimpleTestCase):

    @patch("competitions.services.stop_team_service")
    @patch("competitions.services.deploy_team_service")
    @patch("competitions.services.sync_team_service_status")
    @patch("competitions.services.create_team_services")
    @patch("competitions.services.TeamService.objects.filter")
    def test_rollback_after_deployment_failure(
        self, mock_filter, mock_create, mock_sync,
        mock_deploy, mock_stop
    ):
        services = [
            Mock(pk=1, container_id="container-1"),
            Mock(pk=2, container_id="container-2"),
            Mock(pk=3, container_id=""),
        ]

        competition = Mock()
        competition.participations.exists.return_value = True
        competition.services.exists.return_value = True

        mock_filter.return_value = services
        mock_sync.side_effect = ["running", "stopped", "created"]

        mock_deploy.side_effect = [
            None,
            None,
            RuntimeError("Docker failed"),
        ]

        with self.assertRaisesRegex(RuntimeError, "Docker failed"):
            deploy_competition_services(competition)

        # Откатываем только второй контейнер.
        mock_stop.assert_called_once_with(services[1])

        # Первый контейнер работал до операции.
        self.assertNotEqual(
            mock_stop.call_args.args[0],
            services[0]
        )

class TeamServiceModelTests(TestCase):

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Test CTF",
            description="Test competition"
        )
        self.team = Team.objects.create(name="Red Team")
        self.participation = CompetitionParticipation.objects.create(
            team=self.team, competition=self.competition
        )
        self.service = Service.objects.create(
            name="VulnerableService",
            description="Test service"
        )

    def test_team_service_creation(self):
        team_service = TeamService.objects.create(
            participation=self.participation,
            service=self.service
        )

        self.assertEqual(team_service.status, "created")
        self.assertEqual(team_service.container_id, "")

    def test_duplicate_team_service_forbidden(self):
        TeamService.objects.create(
            participation=self.participation,
            service=self.service
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TeamService.objects.create(
                    participation=self.participation,
                    service=self.service
                )

    def test_same_service_for_same_team_in_another_competition(self):
        second_competition = Competition.objects.create(
            name="Second CTF", description="Another competition"
        )
        second_participation = CompetitionParticipation.objects.create(
            team=self.team, competition=second_competition
        )
        TeamService.objects.create(
            participation=self.participation, service=self.service
        )
        TeamService.objects.create(
            participation=second_participation, service=self.service
        )
        self.assertEqual(TeamService.objects.count(), 2)

    def test_participation_and_service_with_deployment_are_protected(self):
        TeamService.objects.create(
            participation=self.participation,
            service=self.service,
            container_id="existing-container",
        )
        with self.assertRaises(ProtectedError):
            self.participation.delete()
        with self.assertRaises(ProtectedError):
            self.team.delete()
        with self.assertRaises(ProtectedError):
            self.service.delete()

    def test_create_team_services_is_repeatable(self):
        self.competition.services.add(self.service)
        create_team_services(self.competition)
        create_team_services(self.competition)

        self.assertEqual(TeamService.objects.count(), 1)
        self.assertEqual(
            TeamService.objects.get().participation_id, self.participation.pk
        )

    @patch("competitions.services.deploy_team_service")
    @patch("competitions.services.sync_team_service_status", return_value="created")
    def test_deployment_targets_only_selected_competition(
        self, mock_sync, mock_deploy
    ):
        another_competition = Competition.objects.create(
            name="Another CTF", description="Another competition"
        )
        another_participation = CompetitionParticipation.objects.create(
            team=self.team, competition=another_competition
        )
        TeamService.objects.create(
            participation=another_participation, service=self.service
        )
        self.competition.services.add(self.service)

        deploy_competition_services(self.competition)

        selected = TeamService.objects.get(
            participation=self.participation, service=self.service
        )
        mock_sync.assert_called_once_with(selected)
        mock_deploy.assert_called_once_with(selected)


class TeamRelationTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Red Team")
        self.competition = Competition.objects.create(
            name="Test CTF", description="Test competition"
        )

    def test_team_can_join_multiple_competitions_but_not_same_one_twice(self):
        CompetitionParticipation.objects.create(
            team=self.team, competition=self.competition
        )
        another = Competition.objects.create(
            name="Another CTF", description="Another competition"
        )
        CompetitionParticipation.objects.create(team=self.team, competition=another)
        self.assertEqual(self.team.participations.count(), 2)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CompetitionParticipation.objects.create(
                    team=self.team, competition=self.competition
                )

    def test_membership_is_unique_and_has_at_most_one_captain(self):
        first = get_user_model().objects.create_user(username="first")
        second = get_user_model().objects.create_user(username="second")
        TeamMembership.objects.create(
            team=self.team, user=first, role="captain"
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TeamMembership.objects.create(team=self.team, user=first)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TeamMembership.objects.create(
                    team=self.team, user=second, role="captain"
                )

        TeamMembership.objects.create(team=self.team, user=second)
        self.assertEqual(self.team.memberships.count(), 2)


class CompetitionListAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.draft = Competition.objects.create(
            name="Private draft", description="Draft description", status="draft"
        )
        team = Team.objects.create(name="Secret team")
        CompetitionParticipation.objects.create(team=team, competition=cls.draft)
        Competition.objects.create(
            name="Public running", description="Running description", status="running"
        )
        Competition.objects.create(
            name="Public finished", description="Finished description", status="finished"
        )

    def test_anonymous_and_participant_see_only_public_competitions(self):
        response = self.client.get(reverse("competition_list"))
        self.assertContains(response, "Public running")
        self.assertContains(response, "Public finished")
        self.assertNotContains(response, "Private draft")
        self.assertNotContains(response, "Secret team")

        participant = get_user_model().objects.create_user(username="participant")
        self.client.force_login(participant)
        self.assertNotContains(
            self.client.get(reverse("competition_list")), "Private draft"
        )

    def test_staff_without_permission_cannot_see_drafts(self):
        staff = get_user_model().objects.create_user(
            username="staff_without_permission", is_staff=True
        )
        self.client.force_login(staff)
        self.assertNotContains(
            self.client.get(reverse("competition_list")), "Private draft"
        )

    def test_user_with_view_permission_can_see_drafts(self):
        staff = get_user_model().objects.create_user(
            username="organizer", is_staff=True
        )
        staff.user_permissions.add(
            Permission.objects.get(
                codename="view_competition", content_type__app_label="competitions"
            )
        )
        self.client.force_login(staff)
        response = self.client.get(reverse("competition_list"))
        self.assertContains(response, "Private draft")
        self.assertContains(response, "Secret team")
