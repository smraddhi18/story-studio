from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from studio.artifacts import approve_artifact, latest
from studio.models import Job, Project
from studio.workflow import start_job, run_job


class Command(BaseCommand):
    help = "Opt-in real Gemini end-to-end smoke test; consumes API usage."

    def add_arguments(self, parser):
        parser.add_argument("--confirm-api-usage", action="store_true")
        parser.add_argument("--keep-project", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm_api_usage"]:
            raise CommandError("Pass --confirm-api-usage to run real Gemini calls.")
        if not settings.GEMINI_API_KEY:
            raise CommandError("GEMINI_API_KEY is missing.")
        project = Project.objects.create(
            title="Story Studio smoke test", premise="A lighthouse keeper hears tomorrow's radio broadcast tonight.",
            genre="Mystery", tone="Hopeful suspense", season_episodes=[1], duration_minutes=10,
        )
        jobs = []
        try:
            for kind, scope in [("brief", "series"), ("plan", "series"), ("draft", "s1e1")]:
                if kind == "plan":
                    approve_artifact(latest(project, "brief", "series"))
                if kind == "draft":
                    approve_artifact(latest(project, "episode_outline", "s1e1"))
                job = start_job(project, kind, scope)
                Job.objects.filter(pk=job.pk).update(status=Job.Status.RUNNING)
                job.refresh_from_db()
                run_job(job)
                job.refresh_from_db()
                jobs.append(job)
                if job.status != Job.Status.SUCCEEDED:
                    raise CommandError(f"{kind} failed: {job.last_error}")
            script = latest(project, "script", "s1e1")
            if not script or not script.content.get("text"):
                raise CommandError("No screenplay was generated.")
            usage = sum(job.usage.get("total_tokens", 0) for job in jobs)
            self.stdout.write(self.style.SUCCESS(f"Smoke passed: {len(script.content['text'])} script characters; {usage} reported tokens; project {project.pk}"))
        finally:
            if not options["keep_project"]:
                project.delete()
