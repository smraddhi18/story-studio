from pydantic import BaseModel, Field


class Character(BaseModel):
    id: str = Field(description="Stable short identifier, e.g. mara")
    name: str
    goal: str
    flaw: str
    voice: str


class Brief(BaseModel):
    title: str
    logline: str
    setting: str
    central_conflict: str
    themes: list[str]
    assumptions: list[str]
    characters: list[Character]


class NamedFact(BaseModel):
    id: str
    name: str
    description: str


class PlotThread(BaseModel):
    id: str
    description: str
    intended_payoff: str


class Bible(BaseModel):
    series_promise: str
    characters: list[Character]
    locations: list[NamedFact]
    world_rules: list[NamedFact]
    plot_threads: list[PlotThread]


class SeriesArc(BaseModel):
    beginning: str
    middle: str
    ending: str
    character_arcs: list[str]
    season_turns: list[str]


class SeasonArc(BaseModel):
    title: str
    premise: str
    opening_state: str
    midpoint: str
    climax: str
    ending_state: str
    continuing_threads: list[str]


class EpisodeOutline(BaseModel):
    title: str
    logline: str
    conflict: str
    character_changes: list[str]
    beats: list[str]
    reveal: str
    ending: str
    thread_ids: list[str]
    timeline_events: list[str]
    summary: str


class SceneBeat(BaseModel):
    heading: str
    purpose: str
    characters: list[str]
    turn: str


class ScenePlan(BaseModel):
    scenes: list[SceneBeat] = Field(min_length=2, max_length=20)


class ReviewFinding(BaseModel):
    category: str
    severity: str
    scene_index: int | None = None
    description: str
    suggested_fix: str


class Review(BaseModel):
    strengths: list[str]
    findings: list[ReviewFinding]
    unresolved_threads: list[str]


class ScriptSummary(BaseModel):
    summary: str
    established_events: list[str]
    changed_characters: list[str]
    open_threads: list[str]
