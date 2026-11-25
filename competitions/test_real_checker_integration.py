"""Opt-in real loopback HTTP. Never starts Docker; ordinary suite skips it."""
import json
import os
import threading
import urllib.request
from pathlib import Path
from unittest import skipUnless

from django.core.management import call_command
from django.test import TransactionTestCase
from werkzeug.serving import WSGIRequestHandler, make_server

from training_service.app import create_app
from .checker_secrets import unseal
from .game_services import audit_scores, finish_round, scoreboard, submit_flag
from .models import CheckerEndpoint, FlagMaterial
from .test_real_checker import HTTPFixtures


@skipUnless(os.environ.get("CTF_RUN_HTTP_INTEGRATION") == "1", "Requires explicit permission for local HTTP test infrastructure")
class RealHTTPIntegrationTests(HTTPFixtures, TransactionTestCase):
    def test_two_real_flask_instances_worker_idor_attack_and_scoring(self):
        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        for endpoint in CheckerEndpoint.objects.select_related("team_service"):
            token = unseal(endpoint.token_ciphertext, f"endpoint:{endpoint.instance_id}")
            app = create_app(database=Path(self.keydir.name) / f"service-{endpoint.pk}.sqlite3", token=token, instance_id=endpoint.instance_id)
            server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
            # Register before start: a thread-start failure must release the port.
            self.addCleanup(server.server_close)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()

            def cleanup(server=server, thread=thread):
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive(), "HTTP test server did not stop")

            self.addCleanup(cleanup)
            endpoint.port = server.server_port
            endpoint.save(update_fields=["port"])
            endpoint.team_service.host_port = endpoint.port
            endpoint.team_service.save(update_fields=["host_port"])
        game_round = self.open_http()
        # The real worker launches its bounded HTTP subprocess for every job.
        call_command("checker_worker", max_jobs=8, verbosity=0)
        self.assertFalse(FlagMaterial.objects.exists())
        blue = CheckerEndpoint.objects.get(team_service__participation=self.blue_p)
        base = f"http://127.0.0.1:{blue.port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base + "/notes", timeout=3) as response:
            record_id = json.load(response)["notes"][0]
        with opener.open(base + f"/notes/{record_id}", timeout=3) as response:
            stolen = json.load(response)["value"]
        self.assertTrue(submit_flag(self.competition, self.alice, stolen).accepted)
        self.assertFalse(submit_flag(self.competition, self.alice, stolen).accepted)
        finish_round(game_round, self.organizer)
        call_command("checker_worker", max_jobs=8, verbosity=0)
        game_round.refresh_from_db()
        self.assertEqual(game_round.state, "finished")
        self.assertEqual([r["total"] for r in scoreboard(self.competition)], [21, 1])
        self.assertTrue(audit_scores(self.competition)["ok"])
