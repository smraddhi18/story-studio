import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from studio.workflow import claim_job, run_job


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the single local Story Studio generation worker."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process one available job and exit")

    def handle(self, *args, **options):
        self.stdout.write("Story Studio worker ready")
        while True:
            job = claim_job()
            if job:
                self.stdout.write(f"Running job {job.pk}: {job.kind} {job.target_scope}")
                try:
                    run_job(job)
                except Exception:
                    logger.exception("Job %s failed unexpectedly", job.pk)
            elif options["once"]:
                return
            else:
                time.sleep(settings.WORKER_POLL_SECONDS)
            if options["once"]:
                return
