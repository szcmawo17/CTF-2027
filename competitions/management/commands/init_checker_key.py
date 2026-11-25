import os
from pathlib import Path

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create a dedicated Fernet key outside Git; never print or overwrite it."

    def add_arguments(self, parser):
        parser.add_argument("path")

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.is_absolute() or path.resolve().is_relative_to(Path(settings.BASE_DIR).resolve()):
            raise CommandError("Use an absolute path outside the repository, in a private directory.")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            raise CommandError("Cannot create key; check private directory permissions and that the file does not exist.") from None
        with os.fdopen(fd, "wb") as stream:
            stream.write(Fernet.generate_key())
        self.stdout.write("Key created. Set CTF_CHECKER_KEY_FILE for Django and worker; restrict Windows ACL to their operator account.")
