from enum import Enum
from typing import Protocol


class CheckerStatus(str, Enum):
    OK = "OK"
    DOWN = "DOWN"
    MUMBLE = "MUMBLE"
    ERROR = "ERROR"


class Checker(Protocol):
    def put_flag(self, team_service_id: int, flag_id: int, value: str) -> CheckerStatus: ...
    def get_flag(self, team_service_id: int, flag_id: int, expected_digest: str) -> CheckerStatus: ...
    def check_service(self, team_service_id: int) -> CheckerStatus: ...
