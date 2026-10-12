# Story Studio

Story Studio is a local, single-user web app for developing a television series from a short premise. It uses Gemini to create an editable creative brief, a series plan, and screenplay drafts for episodes you select. You choose the number of seasons and the episode count in each season; there is no fixed 30-episode structure.

The current release handles **text only** and **series screenplays only**. Planning generates outlines for the whole requested series. It does **not** draft every episode automatically. Each episode defaults to a 15-minute target, and you can choose a different duration when creating the project.

## Quick start

Requires Python 3.11 or newer and a Gemini API key from Google AI Studio.

~~~sh
cd /path/to/story-studio
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
~~~

Set GEMINI_API_KEY in .env. The default model is gemini-3.8-flash; set GEMINI_MODEL in .env to change it. Keep the key on the server. The browser never receives it.

Start the two processes in separate terminals:

~~~sh
./run-web.sh
~~~

~~~sh
./run-worker.sh
~~~

Open http://127.0.0.1:8000. The web script applies migrations and binds to localhost. The worker processes generation jobs independently, so a job can continue after you close its browser page. SQLite data is stored in data/story_studio.sqlite3 by default; STORY_DB can change the path.

On Macs where Homebrew Python reports a pyexpat symbol error during virtual environment creation, first run:

~~~sh
export DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib
~~~

The run scripts set that path automatically when it exists.

## Architecture

~~~mermaid
flowchart LR
    U[Browser: forms, editor, progress] --> W[Django views and JSON API]
    W --> DB[(SQLite)]
    DB --> WK[Single background worker]
    WK --> OR[LangGraph generation graph]
    OR --> G[Gemini gateway]
    G --> API[Gemini API]
    OR --> DB
    DB --> W
~~~

- **Django web process:** Serves the project list, setup page, brief and plan review, episode pages, script editor, exports, and JSON API. It creates jobs and reads stored results; it does not make the long Gemini calls.
- **Worker and orchestrator:** The runworker command claims one due job at a time. studio/graph_workflow.py uses LangGraph nodes and conditional edges for planning, scene generation, review, and bounded revision. studio/workflow.py handles job claims, durable step execution, retries, and progress.
- **Gemini gateway:** studio/gemini.py calls the Google GenAI SDK. It counts input tokens, requests schema-constrained JSON for planning and review, requests text for screenplay scenes, classifies failures, and records token usage on the job. Pydantic models in studio/schemas.py validate structured results.
- **SQLite store:** studio/models.py defines projects, versioned artifacts, canonical story records, jobs, and job steps. The web and worker processes share this database.
- **Artifact and continuity logic:** studio/artifacts.py creates new versions, tracks source versions, marks affected later work for review, and updates canonical records when relevant artifacts are approved.

### Stored objects

- **Project:** Premise, title, genre, tone, language, episode counts by season, and duration target. For example, 8, 10, 8 means three seasons with 26 episodes in total.
- **Artifact:** A brief, bible, arc, outline, scene plan, scene, script, review, or script summary. Each has a kind, scope, version, content, status, source versions, model name, and originating job. Scopes identify the series, a season such as s2, an episode such as s2e3, or a scene such as s2e3-scene1.
- **CanonicalRecord:** Approved character, location, world-rule, plot-thread, and timeline facts. Timeline records distinguish **planned** events from episode outlines and **established** events from approved script summaries.
- **Job and JobStep:** A job tracks the operation, target, status, progress, retry timing, lease, error, and token usage. Steps point to completed artifacts so the worker can resume without regenerating completed work.

Artifact statuses are **draft**, **approved**, and **needs_review**. Saving an edit creates a new version; it does not overwrite the old one. An upstream change flags affected downstream artifacts for review. Older versions remain available from the artifact page or API.

## Writing workflows

### 1. Create and approve a creative brief

1. On **New series**, enter a working title, a few lines of premise, optional genre and tone, language, episode counts for each season, and target minutes per episode.
2. Start **Generate brief**. Gemini expands the input into a logline, setting, central conflict, themes, characters, and assumptions it introduced.
3. Edit the JSON in the brief editor if needed, save a new version, and approve the version you want to use.

Approving the brief is required before the full series plan can start.

### 2. Plan the complete series

The **Plan entire series** job creates artifacts in this order:

1. Series bible: characters, locations, world rules, and open plot threads with stable IDs.
2. Overall series arc.
3. One arc for each season.
4. One outline for every requested episode, in season and episode order.

An episode outline contains its conflict, character changes, beats, reveal, ending, plot-thread IDs, timeline events, and a short summary. Planning uses the current season arc, recent episode summaries, and prior season endings so later outlines continue the story. Results are saved after each step and can be reviewed or edited individually.

Approve an episode outline before drafting that episode. You can draft episode 7 before episode 1; the app will treat events in undrafted earlier outlines as planned.

### 3. Draft and review an episode

The **Draft episode** job uses the approved episode outline, creative brief, series bible, relevant canonical facts, and selected earlier episode context. It then:

1. Generates a scene plan, normally aiming for 6–10 scenes.
2. Generates screenplay text one scene at a time, with scene headings, action, and dialogue.
3. Assembles the scenes into a script.
4. Generates a continuity and quality review covering chronology, character behavior, missing beats, pacing, and open threads.
5. If the review identifies a major issue tied to a scene, revises up to three affected scenes per pass, with at most two passes.
6. Generates a concise script summary for later episodes.

The duration and word targets guide Gemini; they are not a guarantee of runtime or page count. Review findings are suggestions for the writer to inspect. The job page shows the current step, completed step count, errors, and reported Gemini token usage.

### 4. Approve a script and carry continuity forward

Open the script, edit it if needed, and approve the latest version. If its generated summary still matches that exact script version, the summary is approved too. If you edited the script after its summary was generated, a new summary job is queued.

When drafting a later episode, the app uses approved earlier script summaries as **established** events. For earlier episodes without an approved script, it uses outline summaries as **planned** events. It sends relevant recent summaries and matching plot-thread history, not every previous full script.

### 5. Edit, regenerate, and export

- **Edit an artifact:** Save from its editor to create a new version. The latest downstream artifacts affected by the change receive needs_review status. Approve the revised source before using it for a dependent generation step.
- **Regenerate one scene:** Use the scene control on a script page. The worker writes a new scene version and assembles a new script version. Recheck the new script and its review state before approval.
- **Export:** Download the latest series plan or one episode script as Markdown or plain text from the project or script page. Exports use the latest version, which may still be a draft.

A manual edit to the assembled script does not rewrite its individual stored scenes. If you later regenerate a scene, the new script is assembled from stored scene versions, so compare it with any manual script edits before approving it.

## Job and recovery workflow

Jobs move through **queued → running → succeeded**. A missing key or Gemini safety block produces **blocked**; a persistent error produces **failed**. The job page has a retry action for blocked or failed jobs.

The worker leases a job and saves each finished graph node as a JobStep and Artifact. A transient Gemini error is retried with capped backoff, up to the configured retry limit (three by default). Invalid structured output receives one targeted retry. If the worker stops, another run can reclaim the job after its lease expires. The graph starts again, and completed nodes reuse their persisted artifacts without calling Gemini again. Django records are the durable checkpoint; no separate LangGraph checkpointer is configured. Starting the same operation twice against the same source versions returns the existing active job.

The setup page reports whether the server has a Gemini key and shows the configured model. It does not validate the key with Google. Token usage is recorded per job; the app does not calculate a currency cost.

## API

The web UI uses Django forms. The same core operations are available through JSON endpoints:

- POST /api/projects creates a project; PATCH /api/projects/{id} edits project settings. Episode counts cannot change after generation has started.
- POST /api/projects/{id}/jobs/brief or /plan starts planning. POST /api/projects/{id}/jobs/draft accepts an episode scope such as {"scope":"s1e2"}. /scene accepts a scene scope such as {"scope":"s1e2-scene1"}.
- GET /api/jobs/{id} returns status, progress, steps, errors, and usage. POST /api/jobs/{id}/retry retries a blocked or failed job.
- GET /api/projects/{id}/artifacts lists versions, with optional kind and scope filters. GET/PATCH /api/artifacts/{id} reads or edits one version. POST /api/artifacts/{id}/approve approves the latest version.
- GET /api/projects/{id}/export?scope=plan&format=md exports the plan. Use an episode scope such as s1e2 for a script; format may be md or txt.

Django CSRF protection applies to write endpoints. For an API client, GET /api/csrf, retain its cookie, and send the returned token in the X-CSRFToken header on writes. Browser forms handle this automatically.

## Tests and real Gemini check

~~~sh
.venv/bin/python manage.py test studio
~~~

Automated tests use a fake Gemini adapter and need no API key. They cover variable season sizes, planning order, on-demand drafting, versioning, later-episode context, scene revision, export, failure states, and resume behavior.

With a configured key, run the optional end-to-end check:

~~~sh
./run-smoke.sh
~~~

It creates a one-episode project, generates a brief, plan, and script, reports token usage, and removes that project. Add --keep-project to inspect the generated result in the web app. This command makes real Gemini calls and incurs API usage.

## Current boundaries

Story Studio runs locally with one worker and SQLite. It has no user accounts or collaboration. It does not generate movies, prose stories, images, audio, or video. It plans every requested episode, then drafts scripts only when selected. Generated prose and continuity findings require human review before use.
