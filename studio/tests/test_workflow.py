import json
from datetime import timedelta
from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from studio.artifacts import approve_artifact, create_version, edit_artifact, latest
from studio.gemini import GeminiGateway, GenerationError
from studio.models import Artifact, Job, Project
from studio.schemas import Bible, Brief, EpisodeOutline, Review, ScenePlan, ScriptSummary, SeasonArc, SeriesArc
from studio.workflow import _prior, claim_job, run_job, start_job


class FakeGateway:
    def __init__(self, fail_once=None, fail_at=None, major_review_once=False):
        self.fail_once = fail_once
        self.fail_at = fail_at
        self.major_review_once = major_review_once
        self.calls = []

    def _record(self, name, prompt):
        self.calls.append((name, prompt))
        if self.fail_once and (self.fail_at is None or len(self.calls) == self.fail_at):
            error = self.fail_once
            self.fail_once = None
            raise error

    def generate_structured(self, prompt, schema):
        self._record(schema.__name__, prompt)
        data = {
            Brief: {"title": "Echo Town", "logline": "A detective searches for an erased brother.", "setting": "A coastal town", "central_conflict": "Memory versus truth", "themes": ["memory"], "assumptions": ["The town is fictional"], "characters": [{"id": "mara", "name": "Mara", "goal": "Find her brother", "flaw": "Distrust", "voice": "Direct"}]},
            Bible: {"series_promise": "Every answer changes the town.", "characters": [{"id": "mara", "name": "Mara", "goal": "Find her brother", "flaw": "Distrust", "voice": "Direct"}], "locations": [{"id": "harbor", "name": "Harbor", "description": "Quiet docks"}], "world_rules": [{"id": "memories", "name": "Memory", "description": "Memories can change"}], "plot_threads": [{"id": "brother", "description": "The missing brother", "intended_payoff": "The town remembers"}]},
            SeriesArc: {"beginning": "A clue", "middle": "A cover-up", "ending": "The truth", "character_arcs": ["Mara trusts others"], "season_turns": ["The first reveal", "The final reveal"]},
            SeasonArc: {"title": "The search", "premise": "Mara investigates", "opening_state": "Alone", "midpoint": "She finds a witness", "climax": "She confronts the town", "ending_state": "With allies", "continuing_threads": ["brother"]},
            EpisodeOutline: {"title": "The clue", "logline": "Mara finds a clue", "conflict": "No one believes her", "character_changes": ["Mara asks for help"], "beats": ["Find a photo", "Question a witness"], "reveal": "The photo is recent", "ending": "A new lead", "thread_ids": ["brother"], "timeline_events": ["Mara finds a photo"], "summary": "Mara discovers a photo."},
            ScenePlan: {"scenes": [{"heading": "INT. POLICE STATION - DAY", "purpose": "Find clue", "characters": ["mara"], "turn": "The photo appears"}, {"heading": "EXT. HARBOR - NIGHT", "purpose": "Find witness", "characters": ["mara"], "turn": "The witness runs"}]},
            Review: {"strengths": ["Clear mystery"], "findings": [], "unresolved_threads": ["brother"]},
            ScriptSummary: {"summary": "Mara finds the photo.", "established_events": ["Mara finds a photo"], "changed_characters": ["Mara"], "open_threads": ["brother"]},
        }[schema]
        if schema is Review and self.major_review_once:
            data = {**data, "findings": [{"category": "continuity", "severity": "major", "scene_index": 1, "description": "The clue appears without setup", "suggested_fix": "Show the clue being found"}]}
            self.major_review_once = False
        return data, {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}

    def generate_text(self, prompt):
        self._record("text", prompt)
        return "INT. POLICE STATION - DAY\n\nMara studies the photograph.\n\nMARA\nI know him.", {"input_tokens": 120, "output_tokens": 80, "total_tokens": 200}


class WorkflowTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(title="Echo Town", premise="Everyone remembers a brother she never had.", season_episodes=[2, 1], duration_minutes=15)

    def run_queued(self, fake):
        job = claim_job()
        self.assertIsNotNone(job)
        run_job(job, fake)
        job.refresh_from_db()
        return job

    def plan(self):
        start_job(self.project, "brief")
        self.assertEqual(self.run_queued(FakeGateway()).status, Job.Status.SUCCEEDED)
        approve_artifact(latest(self.project, "brief", "series"))
        start_job(self.project, "plan")
        self.assertEqual(self.run_queued(FakeGateway()).status, Job.Status.SUCCEEDED)

    def test_variable_season_counts_and_job_deduplication(self):
        first = start_job(self.project, "brief")
        self.assertEqual(first.pk, start_job(self.project, "brief").pk)
        self.run_queued(FakeGateway())
        approve_artifact(latest(self.project, "brief", "series"))
        start_job(self.project, "plan")
        job = self.run_queued(FakeGateway())
        self.assertEqual(job.progress_current, 7)
        self.assertEqual(job.progress_total, 7)
        scopes = list(Artifact.objects.filter(project=self.project, kind="episode_outline").values_list("scope", flat=True))
        self.assertEqual(sorted(scopes), ["s1e1", "s1e2", "s2e1"])

    def test_draft_later_episode_uses_planned_prior_then_exports(self):
        self.plan()
        outline = latest(self.project, "episode_outline", "s1e2")
        approve_artifact(outline)
        fake = FakeGateway()
        start_job(self.project, "draft", "s1e2")
        job = self.run_queued(fake)
        self.assertEqual(job.status, Job.Status.SUCCEEDED)
        self.assertTrue(any('"state": "planned"' in prompt for _, prompt in fake.calls))
        script = latest(self.project, "script", "s1e2")
        self.assertIn("MARA", script.content["text"])
        response = self.client.get(f"/api/projects/{self.project.pk}/export?scope=s1e2&format=md")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"MARA", response.content)
        approve_artifact(script)
        summary = latest(self.project, "script_summary", "s1e2")
        approve_artifact(summary)
        self.assertEqual(summary.status, Artifact.Status.APPROVED)

    def test_editing_brief_creates_version_and_flags_downstream(self):
        self.plan()
        brief = latest(self.project, "brief", "series")
        edited = edit_artifact(brief, {**brief.content, "central_conflict": "A different secret"})
        self.assertEqual(edited.version, 2)
        self.assertEqual(edited.status, Artifact.Status.DRAFT)
        self.assertEqual(latest(self.project, "episode_outline", "s2e1").status, Artifact.Status.NEEDS_REVIEW)
        self.assertEqual(Artifact.objects.filter(project=self.project, kind="brief").count(), 2)

    def test_transient_failure_resumes_after_completed_step(self):
        start_job(self.project, "brief")
        self.run_queued(FakeGateway())
        approve_artifact(latest(self.project, "brief", "series"))
        job = start_job(self.project, "plan")
        fake = FakeGateway(fail_once=GenerationError("Temporary", transient=True), fail_at=2)
        first = self.run_queued(fake)
        self.assertEqual(first.status, Job.Status.QUEUED)
        self.assertEqual(first.steps.get(key="bible").status, "succeeded")
        self.assertEqual(first.steps.get(key="series_arc").attempt_count, 1)
        first.next_run_at = timezone.now() - timedelta(seconds=1)
        first.save(update_fields=["next_run_at"])
        resumed = self.run_queued(FakeGateway())
        self.assertEqual(resumed.status, Job.Status.SUCCEEDED)
        self.assertEqual(Artifact.objects.filter(project=self.project, kind="bible").count(), 1)

    def test_major_finding_triggers_bounded_scene_revision(self):
        self.plan()
        approve_artifact(latest(self.project, "episode_outline", "s1e1"))
        start_job(self.project, "draft", "s1e1")
        job = self.run_queued(FakeGateway(major_review_once=True))
        self.assertEqual(job.status, Job.Status.SUCCEEDED)
        self.assertEqual(job.progress_current, 9)
        self.assertEqual(job.progress_total, 9)
        self.assertEqual(Artifact.objects.filter(project=self.project, kind="script", scope="s1e1").count(), 2)
        self.assertEqual(Artifact.objects.filter(project=self.project, kind="review", scope="s1e1").count(), 2)

    def test_two_review_passes_stop_after_second_revision(self):
        class TwoPassGateway(FakeGateway):
            reviews = 0

            def generate_structured(self, prompt, schema):
                if schema is Review:
                    self.reviews += 1
                    self.major_review_once = self.reviews <= 3
                return super().generate_structured(prompt, schema)

        self.plan()
        approve_artifact(latest(self.project, "episode_outline", "s1e1"))
        start_job(self.project, "draft", "s1e1")
        job = self.run_queued(TwoPassGateway())
        self.assertEqual(job.status, Job.Status.SUCCEEDED)
        self.assertEqual(job.progress_current, 12)
        self.assertEqual(job.progress_total, 12)
        self.assertEqual(Artifact.objects.filter(project=self.project, kind="script", scope="s1e1").count(), 3)
        self.assertEqual(Artifact.objects.filter(project=self.project, kind="review", scope="s1e1").count(), 3)

    def test_retry_during_revision_reuses_completed_graph_steps(self):
        self.plan()
        approve_artifact(latest(self.project, "episode_outline", "s1e1"))
        start_job(self.project, "draft", "s1e1")
        failed = self.run_queued(FakeGateway(fail_once=GenerationError("Temporary", transient=True), fail_at=5, major_review_once=True))
        self.assertEqual(failed.status, Job.Status.QUEUED)
        self.assertEqual(failed.steps.get(key="review").status, "succeeded")
        failed.next_run_at = timezone.now() - timedelta(seconds=1)
        failed.save(update_fields=["next_run_at"])
        resumed = self.run_queued(FakeGateway())
        self.assertEqual(resumed.status, Job.Status.SUCCEEDED)
        self.assertEqual(resumed.progress_current, 9)
        self.assertEqual(resumed.progress_total, 9)
        self.assertEqual(Artifact.objects.filter(project=self.project, kind="scene", scope="s1e1-scene1").count(), 2)

    def test_full_series_plan_exceeds_default_graph_recursion_limit(self):
        self.project.season_episodes = [26]
        self.project.save(update_fields=["season_episodes"])
        start_job(self.project, "brief")
        self.run_queued(FakeGateway())
        approve_artifact(latest(self.project, "brief", "series"))
        start_job(self.project, "plan")
        job = self.run_queued(FakeGateway())
        self.assertEqual(job.status, Job.Status.SUCCEEDED)
        self.assertEqual(job.progress_current, 29)

    def test_scene_regeneration_creates_new_script_version(self):
        self.plan()
        approve_artifact(latest(self.project, "episode_outline", "s1e1"))
        start_job(self.project, "draft", "s1e1")
        self.run_queued(FakeGateway())
        start_job(self.project, "scene", "s1e1-scene1")
        self.assertEqual(self.run_queued(FakeGateway()).status, Job.Status.SUCCEEDED)
        self.assertEqual(latest(self.project, "script", "s1e1").version, 2)
        self.assertContains(self.client.get(f"/artifacts/{latest(self.project, 'script', 's1e1').pk}"), "Regenerate scene")
        versions = self.client.get(f"/api/projects/{self.project.pk}/artifacts?kind=script&scope=s1e1").json()["artifacts"]
        self.assertEqual([item["version"] for item in versions], [2, 1])

    def test_prior_context_uses_last_approved_script_while_edit_is_draft(self):
        self.plan()
        approve_artifact(latest(self.project, "episode_outline", "s1e1"))
        start_job(self.project, "draft", "s1e1")
        self.run_queued(FakeGateway())
        script = latest(self.project, "script", "s1e1")
        approve_artifact(script)
        approve_artifact(latest(self.project, "script_summary", "s1e1"))
        edit_artifact(script, {"text": script.content["text"] + "\nA new line."})
        self.assertEqual(_prior(self.project, "s1e2")[0]["state"], "established")

    def test_malformed_gemini_json_is_classified_as_invalid(self):
        gateway = object.__new__(GeminiGateway)
        gateway._call = lambda prompt, schema: (SimpleNamespace(parsed=None, text="{broken"), {})
        with self.assertRaises(GenerationError) as caught:
            gateway.generate_structured("brief", Brief)
        self.assertTrue(caught.exception.invalid)

    def test_invalid_and_blocked_responses_surface_job_status(self):
        start_job(self.project, "brief")
        invalid = FakeGateway(fail_once=GenerationError("Invalid JSON", invalid=True))
        job = self.run_queued(invalid)
        self.assertEqual(job.status, Job.Status.QUEUED)
        job.next_run_at = timezone.now() - timedelta(seconds=1)
        job.save(update_fields=["next_run_at"])
        self.assertEqual(self.run_queued(FakeGateway()).status, Job.Status.SUCCEEDED)
        start_job(self.project, "brief")
        blocked = self.run_queued(FakeGateway(fail_once=GenerationError("Blocked", blocked=True)))
        self.assertEqual(blocked.status, Job.Status.BLOCKED)
        self.assertEqual(blocked.last_error, "Blocked")

    def test_project_api_and_screen(self):
        response = self.client.post("/api/projects", data=json.dumps({"title": "New", "premise": "A secret", "season_episodes": [3, 2]}), content_type="application/json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["episode_count"], 5)
        self.assertContains(self.client.get(f"/projects/{self.project.pk}"), "Echo Town")
        self.assertContains(self.client.get("/setup"), "GEMINI_API_KEY is missing")
