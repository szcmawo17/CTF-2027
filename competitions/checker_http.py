"""Fixed loopback protocol, no Django/DB imports; also runs in a child process."""
import hashlib
import hmac
import http.client
import json
import multiprocessing
import secrets
import socket
from dataclasses import dataclass

from .checker_protocol import CheckerStatus

HTTP_BUDGET = 8  # Hard parent-enforced wall-clock limit, including headers/body.


class ProtocolFailure(Exception):
    def __init__(self, status, code, retryable=False):
        self.status, self.code, self.retryable = status, code, retryable


@dataclass(frozen=True)
class HTTPResult:
    status: str
    code: str = ""
    retryable: bool = False


class HTTPChecker:
    def __init__(self, port, instance_id, token, expires_at):
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ProtocolFailure("ERROR", "endpoint")
        self.port, self.instance_id = port, str(instance_id)
        self.token, self.expires_at = token, expires_at

    def _post(self, path, data, authenticated=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["Authorization"] = "Bearer " + self.token
            conn.request("POST", path, json.dumps(data), headers)
            response = conn.getresponse()
            body = response.read(4097)
            if len(body) > 4096:
                raise ProtocolFailure("MUMBLE", "response")
            if response.status in (401, 403):
                raise ProtocolFailure("ERROR", "authentication")
            if response.status != 200:
                raise ProtocolFailure("MUMBLE", "response")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except ConnectionRefusedError:
            raise ProtocolFailure("DOWN", "unreachable") from None
        except (socket.timeout, TimeoutError):
            # PUT may have reached the service. Retry the same idempotency key.
            raise ProtocolFailure("ERROR", "timeout", True) from None
        except (ConnectionError, http.client.HTTPException):
            raise ProtocolFailure("ERROR", "transport", True) from None
        except (ValueError, UnicodeError):
            raise ProtocolFailure("MUMBLE", "response") from None
        except OSError:
            raise ProtocolFailure("ERROR", "transport", True) from None
        finally:
            conn.close()

    def _identify(self):
        # Do not disclose bearer credentials/flags to a replaced host process.
        nonce = secrets.token_hex(32)
        data = self._post("/checker/challenge", {"nonce": nonce}, authenticated=False)
        proof = hmac.new(self.token.encode(), (self.instance_id + ":" + nonce).encode(), hashlib.sha256).hexdigest()
        if data.get("instance") != self.instance_id or not isinstance(data.get("proof"), str) or not hmac.compare_digest(data["proof"], proof):
            raise ProtocolFailure("ERROR", "identity")

    def put_flag(self, team_service_id, flag_id, value):
        self._identify()
        data = self._post("/checker/put", {"id": flag_id, "value": value, "expires_at": self.expires_at})
        return CheckerStatus.OK if data.get("stored") is True else CheckerStatus.MUMBLE

    def get_flag(self, team_service_id, flag_id, expected_digest):
        self._identify()
        data = self._post("/checker/get", {"id": flag_id})
        value = data.get("value")
        if not isinstance(value, str) or len(value) != 69:
            return CheckerStatus.MUMBLE
        digest = hashlib.sha256(value.encode()).hexdigest()
        return CheckerStatus.OK if hmac.compare_digest(digest, expected_digest) else CheckerStatus.MUMBLE

    def check_service(self, team_service_id):
        self._identify()
        data = self._post("/checker/health", {})
        return CheckerStatus.OK if data.get("healthy") is True else CheckerStatus.MUMBLE


def perform_http(payload):
    """Private worker entry point; payload must come from a claimed DB job."""
    try:
        checker = HTTPChecker(payload["port"], payload["instance"], payload["token"], payload["expires_at"])
        op = payload["operation"]
        if op == "put_flag":
            status = checker.put_flag(payload["team_service"], payload["flag"], payload["value"])
        elif op == "get_flag":
            status = checker.get_flag(payload["team_service"], payload["flag"], payload["digest"])
        elif op == "check_service":
            status = checker.check_service(payload["team_service"])
        else:
            return HTTPResult("ERROR", "operation")
        return HTTPResult(status.value)
    except ProtocolFailure as error:
        return HTTPResult(error.status, error.code, error.retryable)
    except Exception:
        return HTTPResult("ERROR", "checker")


def _child(send, payload):
    try:
        send.send(perform_http(payload))
    finally:
        send.close()


def bounded_http(payload):
    """A stalled socket/header cannot hold the worker indefinitely.

    Termination does not prove that a PUT did not occur. Recovery is bounded
    at-least-once with the same flag ID/value, never a fresh flag on retry.
    """
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(send, payload), daemon=True)
    try:
        process.start()
        send.close()
        if receive.poll(HTTP_BUDGET):
            try:
                result = receive.recv()
            except EOFError:
                return HTTPResult("ERROR", "worker_exit", True)
            return result if isinstance(result, HTTPResult) else HTTPResult("ERROR", "checker")
        return HTTPResult("ERROR", "timeout", True)
    finally:
        try:
            if process.pid is not None:
                process.join(timeout=0.2)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=2)
                if process.is_alive():
                    # Do not continue to the next job with an uncontained child.
                    raise RuntimeError("HTTP child did not stop; worker must stop")
                process.close()  # Release Windows process handles as well.
        finally:
            receive.close()
            send.close()
