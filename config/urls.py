from django.urls import path

from studio import views


urlpatterns = [
    path("", views.home, name="home"),
    path("setup", views.setup_page, name="setup"),
    path("projects/new", views.new_project, name="new_project"),
    path("projects/<int:project_id>", views.project_page, name="project"),
    path("projects/<int:project_id>/jobs/<str:kind>", views.start_job_page, name="start_job_page"),
    path("artifacts/<int:artifact_id>", views.artifact_page, name="artifact"),
    path("artifacts/<int:artifact_id>/save", views.artifact_save_page, name="artifact_save"),
    path("artifacts/<int:artifact_id>/approve", views.artifact_approve_page, name="artifact_approve"),
    path("jobs/<int:job_id>", views.job_page, name="job"),
    path("jobs/<int:job_id>/retry", views.retry_job_page, name="retry_job_page"),
    path("api/projects", views.api_projects),
    path("api/projects/<int:project_id>", views.api_project),
    path("api/projects/<int:project_id>/artifacts", views.api_artifacts),
    path("api/csrf", views.api_csrf),
    path("api/projects/<int:project_id>/jobs/<str:kind>", views.api_start_job),
    path("api/jobs/<int:job_id>", views.api_job),
    path("api/jobs/<int:job_id>/retry", views.api_retry_job),
    path("api/artifacts/<int:artifact_id>", views.api_artifact),
    path("api/artifacts/<int:artifact_id>/approve", views.api_approve_artifact),
    path("api/projects/<int:project_id>/export", views.export_project),
]
