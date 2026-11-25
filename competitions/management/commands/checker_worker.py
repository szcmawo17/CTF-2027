from django.core.management.base import BaseCommand, CommandError
from competitions.checker_worker import run_one


class Command(BaseCommand):
    help = "Process a bounded number of checker jobs; no Docker operations."

    def add_arguments(self, parser):
        parser.add_argument("--max-jobs", type=int, default=1)

    def handle(self, *args, **options):
        limit = options["max_jobs"]
        if not 1 <= limit <= 1000:
            raise CommandError("max-jobs must be between 1 and 1000")
        count = 0
        try:
            for _ in range(limit):
                if not run_one():
                    break
                count += 1
        except Exception:
            raise CommandError("Worker stopped. Check job states; expired leases can be recovered. No raw error logged.") from None
        self.stdout.write(f"Processed jobs: {count}")
