"""LangGraph routing for the persisted Story Studio generation steps.

The Django JobStep and Artifact records are the durable source of truth. A worker
replays the graph from START after a retry; completed steps return their stored
artifacts without another model call.
"""

from typing import TypedDict

from django.conf import settings
from langgraph.graph import END, START, StateGraph

from .artifacts import approve_artifact, create_version, latest
from .gemini import GenerationError
from .models import JobStep
from .schemas import Bible, Brief, EpisodeOutline, Review, ScenePlan, ScriptSummary, SeasonArc, SeriesArc
from .workflow import _canon, _make_structured, _make_text, _prior, _prompt, _step, episode_scope, season_scope


class StoryState(TypedDict, total=False):
    ok: bool
    season: int
    episode: int
    scene_index: int
    pass_number: int
    revision_indexes: list[int]
    revision_cursor: int
    next_node: str


def _step_artifact(job, key):
    return JobStep.objects.get(job=job, key=key).artifact


def _plan_history(project, season):
    history = []
    for previous, count in enumerate(project.season_episodes[:season - 1], 1):
        arc = latest(project, "season_arc", season_scope(previous))
        outlines = [latest(project, "episode_outline", episode_scope(previous, number)) for number in range(1, count + 1)]
        history.append({
            "season": previous,
            "ending_state": arc.content.get("ending_state", "") if arc else "",
            "recent_episodes": [
                {"episode": number, "summary": outline.content.get("summary", ""), "ending": outline.content.get("ending", "")}
                for number, outline in list(enumerate(outlines, 1))[-2:] if outline
            ],
        })
    return history


def _draft_context(job):
    project, scope = job.project, job.target_scope
    brief = latest(project, "brief", "series")
    bible = latest(project, "bible", "series")
    outline = latest(project, "episode_outline", scope)
    common = {"brief": brief.content, "canon": _canon(project, scope), "episode_outline": outline.content, "prior_episodes": _prior(project, scope)}
    return brief, bible, outline, common


def _scene_artifacts(job, count, through_pass):
    artifacts = [_step_artifact(job, f"scene:{index}") for index in range(1, count + 1)]
    for pass_number in range(1, through_pass + 1):
        for step in JobStep.objects.filter(job=job, key__startswith=f"revise:{pass_number}:", status="succeeded").select_related("artifact"):
            index = int(step.key.rsplit(":", 1)[1])
            artifacts[index - 1] = step.artifact
    return artifacts


def build_generation_graph(job, gateway):
    """Compile the graph for one job; its nodes use the existing durable step store."""
    graph = StateGraph(StoryState)

    def brief(state):
        project = job.project
        context = {"premise": project.premise, "genre": project.genre, "tone": project.tone, "seasons": project.season_episodes}
        artifact = _step(job, "brief", lambda: _make_structured(job, "brief", "series", Brief, _prompt("Expand the premise into an editable creative brief. Identify invented details in assumptions.", project, context), [], gateway))
        return {"ok": bool(artifact)}

    def bible(state):
        project = job.project
        source = latest(project, "brief", "series")
        artifact = _step(job, "bible", lambda: _make_structured(job, "bible", "series", Bible, _prompt("Create a consistent series bible. Include stable IDs.", project, {"brief": source.content}), [source], gateway))
        return {"ok": bool(artifact)}

    def series_arc(state):
        project = job.project
        source, story_bible = latest(project, "brief", "series"), _step_artifact(job, "bible")
        artifact = _step(job, "series_arc", lambda: _make_structured(job, "series_arc", "series", SeriesArc, _prompt("Plan an overall series arc covering the requested seasons.", project, {"brief": source.content, "bible": story_bible.content, "season_episodes": project.season_episodes}), [source, story_bible], gateway))
        return {"ok": bool(artifact), "season": 1, "episode": 1}

    def season_arc(state):
        project, season = job.project, state["season"]
        brief_source, story_bible, arc = latest(project, "brief", "series"), _step_artifact(job, "bible"), _step_artifact(job, "series_arc")
        count = project.season_episodes[season - 1]
        artifact = _step(job, f"season:{season}", lambda: _make_structured(job, "season_arc", season_scope(season), SeasonArc, _prompt(f"Plan season {season} with exactly {count} episode slots. Use the overall series arc.", project, {"brief": brief_source.content, "bible": story_bible.content, "series_arc": arc.content, "prior_seasons": _plan_history(project, season)}), [brief_source, story_bible, arc], gateway))
        return {"ok": bool(artifact)}

    def episode_outline(state):
        project, season, episode = job.project, state["season"], state["episode"]
        brief_source, story_bible, arc = latest(project, "brief", "series"), _step_artifact(job, "bible"), _step_artifact(job, "series_arc")
        season_artifact = _step_artifact(job, f"season:{season}")
        prior_episodes = []
        for number in range(1, episode):
            prior = _step_artifact(job, f"episode:{season}:{number}")
            prior_episodes.append({"episode": number, "summary": prior.content.get("summary", ""), "ending": prior.content.get("ending", "")})
        count = project.season_episodes[season - 1]
        artifact = _step(job, f"episode:{season}:{episode}", lambda: _make_structured(job, "episode_outline", episode_scope(season, episode), EpisodeOutline, _prompt(f"Plan episode {episode} of season {season}. Give distinct progression and an ending that leads into the next episode. This is one of {count} episodes in the season.", project, {"brief": brief_source.content, "bible": story_bible.content, "series_arc": arc.content, "season_arc": season_artifact.content, "prior_seasons": _plan_history(project, season), "prior_episodes": prior_episodes[-6:]}), [brief_source, story_bible, arc, season_artifact], gateway))
        if not artifact:
            return {"ok": False}
        if episode < count:
            return {"ok": True, "episode": episode + 1, "next_node": "episode_outline"}
        return {"ok": True, "season": season + 1, "episode": 1, "next_node": "season_arc" if season < len(project.season_episodes) else END}

    def scene_plan(state):
        project, scope = job.project, job.target_scope
        brief_source, story_bible, outline, common = _draft_context(job)
        artifact = _step(job, "scene_plan", lambda: _make_structured(job, "scene_plan", scope, ScenePlan, _prompt("Break this episode into 6 to 10 screenplay scenes. Each scene must change the situation. Plan enough substance for a complete 10 to 15 minute script, roughly 1600 to 2200 words overall.", project, common), [brief_source, story_bible, outline], gateway))
        if not artifact:
            return {"ok": False}
        count = len(artifact.content.get("scenes", []))
        if not 2 <= count <= 20:
            raise GenerationError("Scene plan must contain between 2 and 20 scenes.", invalid=True)
        job.progress_total = count + 4
        job.save(update_fields=["progress_total", "updated_at"])
        return {"ok": True, "scene_index": 1, "pass_number": 0}

    def scene(state):
        project, scope, index = job.project, job.target_scope, state["scene_index"]
        _, _, outline, common = _draft_context(job)
        plan = _step_artifact(job, "scene_plan")
        scenes = plan.content["scenes"]
        previous = [artifact.content["text"][-1200:] for artifact in _scene_artifacts(job, index - 1, 0)[-2:]]
        artifact = _step(job, f"scene:{index}", lambda: _make_text(job, "scene", f"{scope}-scene{index}", _prompt(f"Write screenplay scene {index} of {len(scenes)}. Start with the scene heading. Write action and dialogue, not an outline. Aim for 180 to 300 words of meaningful action and dialogue. End after this scene. Keep continuity with previous scenes.", project, {**common, "scene_beat": scenes[index - 1], "previous_scene_endings": previous}), [outline, plan], gateway))
        if not artifact:
            return {"ok": False}
        next_index = index + 1
        return {"ok": True, "scene_index": next_index, "next_node": "scene" if next_index <= len(scenes) else "script"}

    def script(state):
        project, scope, pass_number = job.project, job.target_scope, state["pass_number"]
        plan = _step_artifact(job, "scene_plan")
        outline = latest(project, "episode_outline", scope)
        artifacts = _scene_artifacts(job, len(plan.content["scenes"]), pass_number)
        script_text = "\n\n".join(artifact.content["text"] for artifact in artifacts)
        key = "script" if pass_number == 0 else f"script:{pass_number}"
        artifact = _step(job, key, lambda: (create_version(project, "script", scope, {"text": script_text}, sources=[outline, plan, *artifacts], job=job, model_name=settings.GEMINI_MODEL), {}))
        return {"ok": bool(artifact)}

    def review(state):
        project, scope, pass_number = job.project, job.target_scope, state["pass_number"]
        _, _, _, common = _draft_context(job)
        script_key = "script" if pass_number == 0 else f"script:{pass_number}"
        script_artifact = _step_artifact(job, script_key)
        task = "Review the script. Report specific continuity, timeline, missing-beat, pacing, and open-thread issues as suggestions. Set severity to major for problems that need revision and include a 1-based scene_index when possible. Do not invent problems." if pass_number == 0 else "Review the revised script. Report remaining specific issues as suggestions. Mark truly blocking issues major with a 1-based scene_index."
        key = "review" if pass_number == 0 else f"review:{pass_number}"
        artifact = _step(job, key, lambda: _make_structured(job, "review", scope, Review, _prompt(task, project, {**common, "script": script_artifact.content["text"]}), [script_artifact], gateway))
        if not artifact:
            return {"ok": False}
        scene_count = len(_step_artifact(job, "scene_plan").content["scenes"])
        indexes = sorted({
            finding.get("scene_index") for finding in artifact.content.get("findings", [])
            if str(finding.get("severity", "")).lower() in {"major", "high", "critical"}
            and isinstance(finding.get("scene_index"), int)
            and 1 <= finding["scene_index"] <= scene_count
        })[:3]
        if indexes and pass_number < 2:
            completed_revisions = sum(
                1 for key in JobStep.objects.filter(job=job, key__startswith="revise:").values_list("key", flat=True)
                if int(key.split(":")[1]) <= pass_number
            )
            job.progress_total = scene_count + 4 + completed_revisions + 2 * pass_number + len(indexes) + 2
            job.save(update_fields=["progress_total", "updated_at"])
            return {"ok": True, "pass_number": pass_number + 1, "revision_indexes": indexes, "revision_cursor": 0, "next_node": "revise"}
        return {"ok": True, "next_node": "summary"}

    def revise(state):
        project, scope, pass_number = job.project, job.target_scope, state["pass_number"]
        cursor, indexes = state["revision_cursor"], state["revision_indexes"]
        index = indexes[cursor]
        _, _, outline, common = _draft_context(job)
        plan = _step_artifact(job, "scene_plan")
        previous = _scene_artifacts(job, len(plan.content["scenes"]), pass_number - 1)[index - 1]
        review_key = "review" if pass_number == 1 else f"review:{pass_number - 1}"
        review_artifact = _step_artifact(job, review_key)
        findings = [finding for finding in review_artifact.content["findings"] if finding.get("scene_index") == index]
        artifact = _step(job, f"revise:{pass_number}:{index}", lambda: _make_text(job, "scene", f"{scope}-scene{index}", _prompt(f"Revise screenplay scene {index} to address these specific major findings. Keep the scene heading and continuity. Return only the revised scene.", project, {**common, "scene_beat": plan.content["scenes"][index - 1], "previous_scene": previous.content["text"], "findings": findings}), [outline, plan, previous, review_artifact], gateway))
        if not artifact:
            return {"ok": False}
        next_cursor = cursor + 1
        return {"ok": True, "revision_cursor": next_cursor, "next_node": "revise" if next_cursor < len(indexes) else "script"}

    def summary(state):
        project, scope = job.project, job.target_scope
        pass_number = state["pass_number"]
        script_key = "script" if pass_number == 0 else f"script:{pass_number}"
        script_artifact = _step_artifact(job, script_key)
        outline = latest(project, "episode_outline", scope)
        artifact = _step(job, "summary", lambda: _make_structured(job, "script_summary", scope, ScriptSummary, _prompt("Summarize only events and character changes that actually occur in this screenplay. This will be continuity context for later episodes.", project, {"outline": outline.content, "script": script_artifact.content["text"]}), [script_artifact], gateway))
        return {"ok": bool(artifact)}

    def regenerate_scene(state):
        project, scope = job.project, job.target_scope
        base, index_text = scope.rsplit("-scene", 1)
        index = int(index_text)
        plan, outline = latest(project, "scene_plan", base), latest(project, "episode_outline", base)
        beat = plan.content["scenes"][index - 1]
        artifact = _step(job, "scene", lambda: _make_text(job, "scene", scope, _prompt(f"Rewrite screenplay scene {index}. Preserve continuity and fulfill this scene beat. Start with a scene heading.", project, {"canon": _canon(project, base), "outline": outline.content, "scene_beat": beat}), [outline, plan], gateway))
        return {"ok": bool(artifact)}

    def regenerate_script(state):
        project, scope = job.project, job.target_scope
        base = scope.rsplit("-scene", 1)[0]
        plan, outline = latest(project, "scene_plan", base), latest(project, "episode_outline", base)
        scenes = [latest(project, "scene", f"{base}-scene{index}") for index in range(1, len(plan.content["scenes"]) + 1)]
        script_text = "\n\n".join(artifact.content["text"] for artifact in scenes if artifact)
        artifact = _step(job, "script", lambda: (create_version(project, "script", base, {"text": script_text}, sources=[outline, plan, *scenes], job=job, model_name=settings.GEMINI_MODEL), {}))
        return {"ok": bool(artifact)}

    def approved_summary(state):
        script_artifact = latest(job.project, "script", job.target_scope)
        artifact = _step(job, "summary", lambda: _make_structured(job, "script_summary", job.target_scope, ScriptSummary, _prompt("Summarize established events, character changes, and open threads in this approved screenplay.", job.project, {"script": script_artifact.content["text"]}), [script_artifact], gateway))
        if artifact:
            approve_artifact(artifact)
        return {"ok": bool(artifact)}

    nodes = {
        "brief": brief, "bible": bible, "series_arc": series_arc, "season_arc": season_arc,
        "episode_outline": episode_outline, "scene_plan": scene_plan, "scene": scene,
        "script": script, "review": review, "revise": revise, "summary": summary,
        "regenerate_scene": regenerate_scene, "regenerate_script": regenerate_script,
        "approved_summary": approved_summary,
    }
    for name, node in nodes.items():
        graph.add_node(name, node)

    starts = {"brief": "brief", "plan": "bible", "draft": "scene_plan", "scene": "regenerate_scene", "summary": "approved_summary"}
    graph.add_conditional_edges(START, lambda state: starts[job.kind], list(starts.values()))
    graph.add_conditional_edges("bible", lambda state: "series_arc" if state["ok"] else END, ["series_arc", END])
    graph.add_conditional_edges("series_arc", lambda state: "season_arc" if state["ok"] else END, ["season_arc", END])
    graph.add_conditional_edges("season_arc", lambda state: "episode_outline" if state["ok"] else END, ["episode_outline", END])
    graph.add_conditional_edges("episode_outline", lambda state: state["next_node"] if state["ok"] else END, ["episode_outline", "season_arc", END])
    graph.add_conditional_edges("scene_plan", lambda state: "scene" if state["ok"] else END, ["scene", END])
    graph.add_conditional_edges("scene", lambda state: state["next_node"] if state["ok"] else END, ["scene", "script", END])
    graph.add_conditional_edges("script", lambda state: "review" if state["ok"] else END, ["review", END])
    graph.add_conditional_edges("review", lambda state: state["next_node"] if state["ok"] else END, ["revise", "summary", END])
    graph.add_conditional_edges("revise", lambda state: state["next_node"] if state["ok"] else END, ["revise", "script", END])
    graph.add_conditional_edges("regenerate_scene", lambda state: "regenerate_script" if state["ok"] else END, ["regenerate_script", END])
    for terminal in ("brief", "summary", "regenerate_script", "approved_summary"):
        graph.add_edge(terminal, END)
    return graph.compile(name="story_studio_generation")
