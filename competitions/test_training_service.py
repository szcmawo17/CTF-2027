import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TransactionTestCase

from training_service.app import create_app
from .models import CheckerEndpoint, TeamService
from .test_real_checker import HTTPFixtures
from .training_infrastructure import deploy_training_service


class TeachingServiceTests(SimpleTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="ctf-notes-unit-")
        self.addCleanup(self.folder.cleanup)
        self.token = "a" * 64
        self.instance = uuid.uuid4()
        self.path = Path(self.folder.name) / "one.sqlite3"
        self.client = create_app(database=self.path, token=self.token, instance_id=self.instance).test_client()
        self.headers = {"Authorization": "Bearer " + self.token}
        self.record = {"id": 1, "value": "CTF{" + "b" * 64 + "}", "expires_at": time.time() + 60}

    def test_trusted_put_get_health_and_public_idor(self):
        self.assertEqual(self.client.post("/checker/put", json=self.record, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post("/checker/get", json={"id": 1}, headers=self.headers).json["value"], self.record["value"])
        self.assertTrue(self.client.post("/checker/health", headers=self.headers).json["healthy"])
        self.assertEqual(self.client.get("/notes").json, {"notes": [1]})
        self.assertEqual(self.client.get("/notes/1").json["value"], self.record["value"])

    def test_checker_operations_require_token_but_public_idor_does_not_expose_it(self):
        for route in ("put", "get", "health"):
            self.assertEqual(self.client.post("/checker/" + route, json=self.record).status_code, 403)
        self.assertNotIn(self.token, self.client.get("/notes").get_data(as_text=True))
        self.assertEqual(self.client.get("/checker/put").status_code, 403)

    def test_same_put_is_idempotent_and_conflicting_put_cannot_overwrite(self):
        for _ in range(2):
            self.assertEqual(self.client.post("/checker/put", json=self.record, headers=self.headers).status_code, 200)
        changed = {**self.record, "value": "CTF{" + "c" * 64 + "}"}
        self.assertEqual(self.client.post("/checker/put", json=changed, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.get("/notes/1").json["value"], self.record["value"])

    def test_instances_are_isolated_and_data_survives_new_app_instance(self):
        self.client.post("/checker/put", json=self.record, headers=self.headers)
        second = create_app(database=Path(self.folder.name) / "two.sqlite3", token="d" * 64, instance_id=uuid.uuid4()).test_client()
        self.assertEqual(second.get("/notes/1").status_code, 404)
        restarted = create_app(database=self.path, token=self.token, instance_id=self.instance).test_client()
        self.assertEqual(restarted.get("/notes/1").json["value"], self.record["value"])

    def test_expired_flags_hidden_from_attackers_then_purged(self):
        self.client.post("/checker/put", json=self.record, headers=self.headers)
        with patch("training_service.app.time.time", return_value=self.record["expires_at"] + 1):
            self.assertEqual(self.client.get("/notes/1").status_code, 404)
            self.assertEqual(self.client.post("/checker/get", json={"id": 1}, headers=self.headers).status_code, 200)
        with patch("training_service.app.time.time", return_value=self.record["expires_at"] + 121):
            self.assertEqual(self.client.post("/checker/get", json={"id": 1}, headers=self.headers).status_code, 404)

    def test_invalid_and_oversized_input(self):
        self.assertEqual(self.client.post("/checker/put", json={**self.record, "id": 2**64}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post("/checker/put", data="x" * 3000, headers=self.headers, content_type="application/json").status_code, 413)


class TrainingProvisionTests(HTTPFixtures, TransactionTestCase):
    def test_new_container_has_dedicated_credentials_and_restricted_options(self):
        service = TeamService.objects.first()
        CheckerEndpoint.objects.get(team_service=service).delete()
        service.container_id, service.host_port = "", None
        service.save()
        client = Mock()

        def run(image, **kwargs):
            from django.db import connection
            self.assertFalse(connection.in_atomic_block)
            container = Mock(id="training-container", status="running")
            container.attrs = {"Config": {"Labels": kwargs["labels"], "User": kwargs["user"]},
                               "HostConfig": {"ReadonlyRootfs": True, "Privileged": False},
                               "NetworkSettings": {"Ports": {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55004"}]}}}
            return container

        client.containers.run.side_effect = run
        with patch("competitions.infrastructure.get_docker_client", return_value=client):
            deploy_training_service(service)
        options = client.containers.run.call_args.kwargs
        self.assertEqual(options["user"], "10001:10001")
        self.assertEqual(options["cap_drop"], ["ALL"])
        self.assertTrue(options["read_only"])
        self.assertNotIn("volumes", options)
        self.assertEqual(set(options["environment"]), {"CHECKER_TOKEN", "CHECKER_INSTANCE"})
        endpoint = CheckerEndpoint.objects.get(team_service=service)
        self.assertNotIn(options["environment"]["CHECKER_TOKEN"], endpoint.token_ciphertext)
        self.assertEqual((endpoint.container_id, endpoint.port), ("training-container", 55004))

    def test_missing_existing_container_is_not_automatically_recreated(self):
        import docker
        service = TeamService.objects.first()
        client = Mock()
        client.containers.get.side_effect = docker.errors.NotFound("missing")
        with patch("competitions.infrastructure.get_docker_client", return_value=client):
            with self.assertRaises(docker.errors.NotFound):
                deploy_training_service(service)
        client.containers.run.assert_not_called()
