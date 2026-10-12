import json

from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from pydantic import ValidationError

from .artifacts import approve_artifact, edit_artifact, latest
from .models import Artifact, Job, Project
from .schemas import Bible, Brief, EpisodeOutline, Review, ScenePlan, ScriptSummary, SeasonArc, SeriesArc
from .workflow import all_episodes, episode_scope, retry_job, start_job


SCHEMAS = {
    "brief": Brief, "bible": Bible, "series_arc": SeriesArc, "season_arc": SeasonArc,
    "episode_outline": EpisodeOutline, "scene_plan": ScenePlan, "review": Review,
    "script_summary": ScriptSummary,
}


def _json(request):
    try:
        return json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Invalid JSON body.")


def _error(message, status=400):
    return JsonResponse({"error": message}, status=status)


def _counts(value):
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if not isinstance(value, list) or not value:
        raise ValueError("Enter at least one season's episode count.")
    try:
        result = [int(number) for number in value]
    except (TypeError, ValueError):
        raise ValueError("Episode counts must be whole numbers.")
    if any(number < 1 for number in result):
        raise ValueError("Each season must have at least one episode.")
    return result


def _project_values(data):
    title = str(data.get("title", "")).strip()
    premise = str(data.get("premise", "")).strip()
    if not title or not premise:
        raise ValueError("Title and premise are required.")
    if len(title) > 160:
        raise ValueError("Title is too long.")
    counts = _counts(data.get("season_episodes"))
    try:
        duration = int(data.get("duration_minutes", 15))
    except (TypeError, ValueError):
        raise ValueError("Episode duration must be a whole number.")
    if not 5 <= duration <= 120:
        raise ValueError("Episode duration must be between 5 and 120 minutes.")
    return dict(title=title, premise=premise, genre=str(data.get("genre", ""))[:100], tone=str(data.get("tone", ""))[:100], language=str(data.get("language", "English"))[:80] or "English", season_episodes=counts, duration_minutes=duration)


def _artifact_dict(artifact):
    return {"id": artifact.pk, "kind": artifact.kind, "scope": artifact.scope, "version": artifact.version, "status": artifact.status, "content": artifact.content, "source_versions": artifact.source_versions}


def _job_dict(job):
    return {
        "id": job.pk, "status": job.status, "kind": job.kind, "target_scope": job.target_scope,
        "stage": job.stage, "current": job.progress_current, "total": job.progress_total,
        "error": job.last_error, "usage": job.usage,
        "steps": list(job.steps.order_by("pk").values("key", "status", "attempt_count", "error")),
    }


def home(request):
    return render(request, "studio/home.html", {"projects": Project.objects.order_by("-updated_at"), "key_present": bool(settings.GEMINI_API_KEY), "model": settings.GEMINI_MODEL})


def setup_page(request):
    return render(request, "studio/setup.html", {"key_present": bool(settings.GEMINI_API_KEY), "model": settings.GEMINI_MODEL})


@require_http_methods(["GET", "POST"])
def new_project(request):
    if request.method == "GET":
        return render(request, "studio/new_project.html")
    try:
        values = _project_values(request.POST)
    except ValueError as exc:
        return render(request, "studio/new_project.html", {"error": str(exc), "values": request.POST}, status=400)
    project = Project.objects.create(**values)
    return redirect("project", project_id=project.pk)


def project_page(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    seasons = []
    for season, count in enumerate(project.season_episodes, 1):
        episodes = []
        for episode in range(1, count + 1):
            scope = episode_scope(season, episode)
            episodes.append({"number": episode, "scope": scope, "outline": latest(project, "episode_outline", scope), "script": latest(project, "script", scope), "review": latest(project, "review", scope)})
        seasons.append({"number": season, "arc": latest(project, "season_arc", f"s{season}"), "episodes": episodes})
    return render(request, "studio/project.html", {
        "project": project, "brief": latest(project, "brief", "series"),
        "bible": latest(project, "bible", "series"), "series_arc": latest(project, "series_arc", "series"),
        "seasons": seasons, "jobs": project.jobs.order_by("-created_at")[:8],
        "key_present": bool(settings.GEMINI_API_KEY),
    })


def artifact_page(request, artifact_id):
    artifact = get_object_or_404(Artifact, pk=artifact_id)
    is_text = artifact.kind in {"scene", "script"}
    value = artifact.content.get("text", "") if is_text else json.dumps(artifact.content, indent=2, ensure_ascii=False)
    scene_plan = latest(artifact.project, "scene_plan", artifact.scope) if artifact.kind == "script" else None
    scenes = list(enumerate(scene_plan.content.get("scenes", []), 1)) if scene_plan else []
    versions = Artifact.objects.filter(project=artifact.project, kind=artifact.kind, scope=artifact.scope).order_by("-version")
    return render(request, "studio/artifact.html", {"artifact": artifact, "value": value, "is_text": is_text, "latest": latest(artifact.project, artifact.kind, artifact.scope), "review": latest(artifact.project, "review", artifact.scope) if artifact.kind == "script" else None, "scenes": scenes, "versions": versions})


def _validate_artifact(artifact, raw):
    if artifact.kind in {"scene", "script"}:
        if not raw.strip():
            raise ValueError("Text cannot be empty.")
        return {"text": raw.strip()}
    try:
        value = json.loads(raw)
        return SCHEMAS[artifact.kind].model_validate(value).model_dump()
    except (json.JSONDecodeError, ValidationError, KeyError) as exc:
        raise ValueError(f"Content does not match the {artifact.kind} format: {exc}")


@require_http_methods(["POST"])
def artifact_save_page(request, artifact_id):
    artifact = get_object_or_404(Artifact, pk=artifact_id)
    if latest(artifact.project, artifact.kind, artifact.scope).pk != artifact.pk:
        return HttpResponseBadRequest("Edit the latest version.")
    try:
        content = _validate_artifact(artifact, request.POST.get("content", ""))
    except ValueError as exc:
        return render(request, "studio/artifact.html", {"artifact": artifact, "value": request.POST.get("content", ""), "is_text": artifact.kind in {"scene", "script"}, "latest": artifact, "error": str(exc)}, status=400)
    new = edit_artifact(artifact, content)
    return redirect("artifact", artifact_id=new.pk)


@require_http_methods(["POST"])
def artifact_approve_page(request, artifact_id):
    artifact = get_object_or_404(Artifact, pk=artifact_id)
    if latest(artifact.project, artifact.kind, artifact.scope).pk != artifact.pk:
        return HttpResponseBadRequest("Approve the latest version.")
    approve_artifact(artifact)
    if artifact.kind == "script":
        summary = latest(artifact.project, "script_summary", artifact.scope)
        source = summary.source_versions.get(f"script:{artifact.scope}") if summary else None
        if summary and source and source["id"] == artifact.pk:
            approve_artifact(summary)
        else:
            start_job(artifact.project, "summary", artifact.scope)
    return redirect("artifact", artifact_id=artifact.pk)


@require_http_methods(["POST"])
def start_job_page(request, project_id, kind):
    project = get_object_or_404(Project, pk=project_id)
    scope = request.POST.get("scope", "series")
    try:
        job = start_job(project, kind, scope)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    return redirect("job", job_id=job.pk)


def job_page(request, job_id):
    return render(request, "studio/job.html", {"job": get_object_or_404(Job, pk=job_id)})


@require_http_methods(["POST"])
def retry_job_page(request, job_id):
    job = get_object_or_404(Job, pk=job_id)
    try:
        retry_job(job)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    return redirect("job", job_id=job.pk)


@require_http_methods(["POST"])
def api_projects(request):
    try:
        project = Project.objects.create(**_project_values(_json(request)))
    except ValueError as exc:
        return _error(str(exc))
    return JsonResponse({"id": project.pk, "episode_count": project.episode_count}, status=201)


@require_http_methods(["PATCH"])
def api_project(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    try:
        values = _project_values({**{
            "title": project.title, "premise": project.premise, "genre": project.genre, "tone": project.tone,
            "language": project.language, "season_episodes": project.season_episodes,
            "duration_minutes": project.duration_minutes,
        }, **_json(request)})
    except ValueError as exc:
        return _error(str(exc))
    if project.artifacts.exists() and values["season_episodes"] != project.season_episodes:
        return _error("Season episode counts cannot change after generation starts. Create a new project.", 409)
    changed = any(getattr(project, key) != value for key, value in values.items())
    for key, value in values.items():
        setattr(project, key, value)
    project.save()
    if changed:
        for artifact in project.artifacts.all():
            if latest(project, artifact.kind, artifact.scope).pk == artifact.pk:
                Artifact.objects.filter(pk=artifact.pk).update(status=Artifact.Status.NEEDS_REVIEW)
    return JsonResponse({"id": project.pk, "episode_count": project.episode_count})


@require_http_methods(["POST"])
def api_start_job(request, project_id, kind):
    project = get_object_or_404(Project, pk=project_id)
    try:
        scope = _json(request).get("scope", "series")
        job = start_job(project, kind, scope)
    except ValueError as exc:
        return _error(str(exc))
    return JsonResponse(_job_dict(job), status=202)


@require_http_methods(["GET"])
def api_job(request, job_id):
    return JsonResponse(_job_dict(get_object_or_404(Job, pk=job_id)))


@ensure_csrf_cookie
def api_csrf(request):
    return JsonResponse({"csrf_token": get_token(request)})


@require_http_methods(["GET"])
def api_artifacts(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    artifacts = Artifact.objects.filter(project=project)
    if request.GET.get("kind"):
        artifacts = artifacts.filter(kind=request.GET["kind"])
    if request.GET.get("scope"):
        artifacts = artifacts.filter(scope=request.GET["scope"])
    return JsonResponse({"artifacts": [
        {"id": item.pk, "kind": item.kind, "scope": item.scope, "version": item.version, "status": item.status, "created_at": item.created_at.isoformat()}
        for item in artifacts.order_by("kind", "scope", "-version")
    ]})


@require_http_methods(["POST"])
def api_retry_job(request, job_id):
    job = get_object_or_404(Job, pk=job_id)
    try:
        retry_job(job)
    except ValueError as exc:
        return _error(str(exc))
    return JsonResponse(_job_dict(job), status=202)


@require_http_methods(["GET", "PATCH"])
def api_artifact(request, artifact_id):
    artifact = get_object_or_404(Artifact, pk=artifact_id)
    if request.method == "GET":
        return JsonResponse(_artifact_dict(artifact))
    if latest(artifact.project, artifact.kind, artifact.scope).pk != artifact.pk:
        return _error("Edit the latest version.", 409)
    try:
        data = _json(request)
        content = data.get("content")
        raw = content if isinstance(content, str) and artifact.kind in {"scene", "script"} else json.dumps(content)
        new = edit_artifact(artifact, _validate_artifact(artifact, raw))
    except ValueError as exc:
        return _error(str(exc))
    return JsonResponse(_artifact_dict(new), status=201)


@require_http_methods(["POST"])
def api_approve_artifact(request, artifact_id):
    artifact = get_object_or_404(Artifact, pk=artifact_id)
    if latest(artifact.project, artifact.kind, artifact.scope).pk != artifact.pk:
        return _error("Approve the latest version.", 409)
    approve_artifact(artifact)
    if artifact.kind == "script":
        summary = latest(artifact.project, "script_summary", artifact.scope)
        source = summary.source_versions.get(f"script:{artifact.scope}") if summary else None
        if summary and source and source["id"] == artifact.pk:
            approve_artifact(summary)
        else:
            start_job(artifact.project, "summary", artifact.scope)
    return JsonResponse(_artifact_dict(artifact))


@require_http_methods(["GET"])
def export_project(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    fmt = request.GET.get("format", "md")
    scope = request.GET.get("scope", "plan")
    if fmt not in {"md", "txt"}:
        return _error("format must be md or txt")
    lines = [project.title, "", project.premise, ""]
    if scope == "plan":
        for kind, label in [("brief", "Creative brief"), ("bible", "Series bible"), ("series_arc", "Series arc")]:
            artifact = latest(project, kind, "series")
            if artifact:
                lines += [f"# {label}" if fmt == "md" else label, json.dumps(artifact.content, indent=2, ensure_ascii=False), ""]
        for season, episode in all_episodes(project):
            if episode == 1:
                arc = latest(project, "season_arc", f"s{season}")
                if arc:
                    lines += [f"# Season {season}" if fmt == "md" else f"Season {season}", json.dumps(arc.content, indent=2, ensure_ascii=False), ""]
            outline = latest(project, "episode_outline", episode_scope(season, episode))
            if outline:
                lines += [f"## Episode {season}.{episode}: {outline.content.get('title', '')}" if fmt == "md" else f"Episode {season}.{episode}: {outline.content.get('title', '')}", json.dumps(outline.content, indent=2, ensure_ascii=False), ""]
    else:
        script = latest(project, "script", scope)
        if not script:
            raise Http404("Script not found")
        lines += [script.content.get("text", "")]
    content = "\n".join(lines)
    response = HttpResponse(content, content_type="text/markdown; charset=utf-8" if fmt == "md" else "text/plain; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="story-studio-{project.pk}-{scope}.{fmt}"'
    return response
