from django.core.management.base import BaseCommand, CommandError

from competitions.game_services import audit_scores
from competitions.models import Competition


class Command(BaseCommand):
    help = "Read-only comparison of score events against captures and checker results."

    def add_arguments(self, parser):
        parser.add_argument("competition_id", type=int)

    def handle(self, *args, **options):
        try:
            competition = Competition.objects.get(pk=options["competition_id"])
        except Competition.DoesNotExist:
            raise CommandError("Competition not found.") from None
        result = audit_scores(competition)
        if not result["ok"]:
            raise CommandError(f"Score mismatch: missing={result['missing']}, unexpected={result['unexpected']}. No data changed.")
        self.stdout.write(self.style.SUCCESS("Score history matches captures and checker results. No data changed."))
