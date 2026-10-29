from django.core.management.base import BaseCommand

from apps.jobs.worker import Worker


class Command(BaseCommand):
    help = "Run the job worker (N job threads) plus the scheduler loop. Ctrl+C stops it gracefully."

    def add_arguments(self, parser):
        parser.add_argument("--threads", type=int, default=None, help="Job threads (default 4).")
        parser.add_argument(
            "--no-scheduler",
            action="store_true",
            help="Only run jobs; leave dispatching, reaping and retention to other workers.",
        )

    def handle(self, *args, threads, no_scheduler, **options):
        clean = Worker(threads=threads, scheduler=not no_scheduler).run()
        if not clean:
            raise SystemExit(1)
