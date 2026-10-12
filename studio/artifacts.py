import hashlib
import json

from django.db import transaction
from django.db.models import Max

from .models import Artifact, CanonicalRecord


def latest(project, kind, scope):
    return Artifact.objects.filter(project=project, kind=kind, scope=scope).order_by("-version").first()


def signature(project, sources):
    payload = {
        "project": project.pk,
        "premise": project.premise,
        "genre": project.genre,
        "tone": project.tone,
        "language": project.language,
        "seasons": project.season_episodes,
        "duration": project.duration_minutes,
        "sources": {f"{a.kind}:{a.scope}": [a.pk, a.version] for a in sources if a},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def source_map(sources):
    return {f"{a.kind}:{a.scope}": {"id": a.pk, "version": a.version} for a in sources if a}


@transaction.atomic
def create_version(project, kind, scope, content, *, sources=(), job=None, model_name="", status="draft"):
    previous = latest(project, kind, scope)
    number = (Artifact.objects.filter(project=project, kind=kind, scope=scope).aggregate(Max("version"))["version__max"] or 0) + 1
    artifact = Artifact.objects.create(
        project=project, kind=kind, scope=scope, version=number, content=content,
        source_versions=source_map(sources), job=job, model_name=model_name, status=status,
    )
    if previous:
        mark_dependents_needing_review(previous)
    return artifact


def _dependent(edited, candidate):
    if candidate.pk == edited.pk:
        return False
    if candidate.kind == edited.kind and candidate.scope == edited.scope:
        return False
    key = f"{edited.kind}:{edited.scope}"
    if key in candidate.source_versions:
        return True
    if edited.kind == "brief":
        return candidate.kind != "brief"
    if edited.kind in {"bible", "series_arc"}:
        return candidate.kind in {"season_arc", "episode_outline", "scene_plan", "scene", "script", "review", "script_summary"}
    if edited.kind == "season_arc":
        try:
            number = int(edited.scope[1:])
            other = int(candidate.scope[1:].split("e", 1)[0])
            return other >= number and candidate.kind in {"season_arc", "episode_outline", "scene_plan", "scene", "script", "review", "script_summary"}
        except ValueError:
            return False
    if edited.kind == "episode_outline":
        try:
            season, episode = edited.scope[1:].split("e", 1)
            other_season, other_episode = candidate.scope[1:].split("e", 1)
            other_episode = other_episode.split("-", 1)[0]
            return (int(other_season), int(other_episode)) >= (int(season), int(episode)) and candidate.kind in {"episode_outline", "scene_plan", "scene", "script", "review", "script_summary"}
        except ValueError:
            return False
    if edited.kind in {"scene_plan", "scene"}:
        return candidate.scope == edited.scope.split("-scene")[0] and candidate.kind in {"script", "review", "script_summary"}
    if edited.kind == "script":
        if candidate.kind in {"review", "script_summary"} and candidate.scope == edited.scope:
            return True
        try:
            season, episode = edited.scope[1:].split("e", 1)
            other_season, other_episode = candidate.scope[1:].split("e", 1)
            other_episode = other_episode.split("-", 1)[0]
            return (int(other_season), int(other_episode)) > (int(season), int(episode)) and candidate.kind in {"episode_outline", "scene_plan", "scene", "script", "review", "script_summary"}
        except ValueError:
            return False
    return False


def mark_dependents_needing_review(edited):
    project = edited.project
    candidates = Artifact.objects.filter(project=project).exclude(pk=edited.pk)
    latest_ids = {}
    for candidate in candidates.order_by("kind", "scope", "-version"):
        latest_ids.setdefault((candidate.kind, candidate.scope), candidate)
    for candidate in latest_ids.values():
        if _dependent(edited, candidate):
            Artifact.objects.filter(pk=candidate.pk).update(status=Artifact.Status.NEEDS_REVIEW)


@transaction.atomic
def edit_artifact(artifact, content):
    return create_version(artifact.project, artifact.kind, artifact.scope, content, sources=[artifact])


@transaction.atomic
def approve_artifact(artifact):
    artifact.status = Artifact.Status.APPROVED
    artifact.save(update_fields=["status"])
    CanonicalRecord.objects.filter(source_artifact__project=artifact.project, source_artifact__kind=artifact.kind, source_artifact__scope=artifact.scope).delete()
    records = []
    if artifact.kind == "bible":
        for kind, field in [("character", "characters"), ("location", "locations"), ("world_rule", "world_rules"), ("plot_thread", "plot_threads")]:
            for item in artifact.content.get(field, []):
                records.append((kind, item.get("id", ""), "planned", item))
    elif artifact.kind == "episode_outline":
        for index, event in enumerate(artifact.content.get("timeline_events", []), 1):
            records.append(("timeline_event", f"{artifact.scope}:{index}", "planned", {"description": event}))
    elif artifact.kind == "script_summary":
        for index, event in enumerate(artifact.content.get("established_events", []), 1):
            records.append(("timeline_event", f"{artifact.scope}:{index}", "established", {"description": event}))
    CanonicalRecord.objects.bulk_create([
        CanonicalRecord(project=artifact.project, kind=kind, key=key, state=state, content=content, source_artifact=artifact)
        for kind, key, state, content in records if key
    ])
    return artifact
