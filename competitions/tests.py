from unittest.mock import Mock, patch
from django.test import SimpleTestCase
import docker

from .infrastructure import (
    validate_container_ports,
    restart_team_service,
    sync_team_service_status,
)


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