"""Trusted, bounded, local simulator. No imports/scripts/URLs supplied by users."""
import hashlib
import hmac
from enum import Enum
from typing import Protocol

from django.views.decorators.debug import sensitive_variables

from .models import MockFlagStorage


class CheckerStatus(str, Enum):
    OK = "OK"
    DOWN = "DOWN"      # Service cannot be reached.
    MUMBLE = "MUMBLE"  # Reachable, but protocol/data verification failed.
    ERROR = "ERROR"    # Checker/platform fault; not evidence of service failure.


@sensitive_variables()
def flag_digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class Checker(Protocol):
    def put_flag(self, team_service_id: int, flag_id: int, value: str) -> CheckerStatus: ...
    def get_flag(self, team_service_id: int, flag_id: int, expected_digest: str) -> CheckerStatus: ...
    def check_service(self, team_service_id: int) -> CheckerStatus: ...


class MockChecker:
    """Persists digests across requests. Does not contact or inspect Docker.

    A real adapter must retrieve bytes and compare their digest, never treat the
    expected digest itself as proof. Such adapters require a separate worker.
    """
    @sensitive_variables()
    def put_flag(self, team_service_id, flag_id, value):
        MockFlagStorage.objects.create(flag_id=flag_id, digest=flag_digest(value))
        return CheckerStatus.OK

    def get_flag(self, team_service_id, flag_id, expected_digest):
        stored = MockFlagStorage.objects.filter(
            flag_id=flag_id, flag__team_service_id=team_service_id
        ).first()
        if stored and hmac.compare_digest(stored.digest, expected_digest):
            return CheckerStatus.OK
        return CheckerStatus.MUMBLE

    def check_service(self, team_service_id):
        return CheckerStatus.OK


@sensitive_variables()
def checked_call(operation, *args):
    """Only enum values cross the boundary; never persist exception text."""
    try:
        result = operation(*args)
    except Exception:
        return CheckerStatus.ERROR.value
    return result.value if isinstance(result, CheckerStatus) else CheckerStatus.ERROR.value
