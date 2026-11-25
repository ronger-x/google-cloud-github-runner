# Periodic reconcile (app/routes/reconcile.py): retry queued jobs whose runner VM could not be
# created (for example QUOTA_EXCEEDED) and delete runner VMs that never registered with GitHub.
# Only created when reconcile_token is set.
resource "google_project_service" "cloudscheduler" {
  count              = var.reconcile_token == "" ? 0 : 1
  project            = module.project.project_id
  service            = "cloudscheduler.googleapis.com"
  disable_on_destroy = false
}

resource "google_cloud_scheduler_job" "reconcile" {
  count       = var.reconcile_token == "" ? 0 : 1
  project     = module.project.project_id
  region      = var.region
  name        = "github-runners-reconcile-${local.region_shortnames[var.region]}"
  description = "Retry stranded queued GitHub jobs and delete orphaned runner VMs"
  schedule    = var.reconcile_schedule
  time_zone   = "Etc/UTC"

  attempt_deadline = "120s"
  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${module.cloud_run_github_runners_manager.service_uri}/reconcile"
    headers = {
      "X-Reconcile-Token" = var.reconcile_token
    }
  }

  depends_on = [google_project_service.cloudscheduler]
}
