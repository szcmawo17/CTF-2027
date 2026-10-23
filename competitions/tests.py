from unittest.mock import Mock, patch
from django.test import SimpleTestCase, TestCase
from django.db import IntegrityError, transaction
from .models import Competition, Team, Service, TeamService
import docker

from .infrastructure import (
    validate_container_ports,
    restart_team_service,
    sync_team_service_status,
)
from .services import deploy_competition_services


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
        competition.teams.exists.return_value = True
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
        self.team = Team.objects.create(
            name="Red Team",
            competition=self.competition
        )
        self.service = Service.objects.create(
            name="VulnerableService",
            description="Test service"
        )

    def test_team_service_creation(self):
        team_service = TeamService.objects.create(
            team=self.team,
            service=self.service
        )

        self.assertEqual(team_service.status, "created")
        self.assertEqual(team_service.container_id, "")

    def test_duplicate_team_service_forbidden(self):
        TeamService.objects.create(
            team=self.team,
            service=self.service
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TeamService.objects.create(
                    team=self.team,
                    service=self.service
                )