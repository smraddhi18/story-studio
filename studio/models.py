from django.db import models
from django.db.models import Q


class Project(models.Model):
    title = models.CharField(max_length=160)
    premise = models.TextField()
    genre = models.CharField(max_length=100, blank=True)
    tone = models.CharField(max_length=100, blank=True)
    language = models.CharField(max_length=80, default="English")
    season_episodes = models.JSONField(default=list)
    duration_minutes = models.PositiveSmallIntegerField(default=15)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def episode_count(self):
        return sum(self.season_episodes)


class Job(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        SUCCEEDED = "succeeded"
        FAILED = "failed"
        BLOCKED = "blocked"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="jobs")
    kind = models.CharField(max_length=24)
    target_scope = models.CharField(max_length=100, default="series")
    source_signature = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    stage = models.CharField(max_length=160, blank=True)
    progress_current = models.PositiveIntegerField(default=0)
    progress_total = models.PositiveIntegerField(default=0)
    lease_until = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    usage = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["project", "kind", "target_scope", "source_signature"],
            condition=Q(status__in=["queued", "running"]), name="one_active_generation",
        )]


class Artifact(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft"
        APPROVED = "approved"
        NEEDS_REVIEW = "needs_review"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="artifacts")
    job = models.ForeignKey(Job, null=True, blank=True, on_delete=models.SET_NULL)
    kind = models.CharField(max_length=32)
    scope = models.CharField(max_length=100)
    version = models.PositiveIntegerField()
    content = models.JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    source_versions = models.JSONField(default=dict)
    model_name = models.CharField(max_length=100, blank=True)
    prompt_version = models.CharField(max_length=20, default="1")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "kind", "scope", "version"], name="artifact_version_unique")]
        ordering = ["kind", "scope", "-version"]


class CanonicalRecord(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="canonical_records")
    kind = models.CharField(max_length=32)
    key = models.CharField(max_length=160)
    state = models.CharField(max_length=16, choices=[("planned", "Planned"), ("established", "Established")])
    content = models.JSONField(default=dict)
    source_artifact = models.ForeignKey(Artifact, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "kind", "key", "source_artifact"], name="canonical_record_source_unique")]


class JobStep(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="steps")
    key = models.CharField(max_length=120)
    status = models.CharField(max_length=12, default="pending")
    attempt_count = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True)
    artifact = models.ForeignKey(Artifact, null=True, blank=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "key"], name="job_step_unique")]
