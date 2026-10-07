# The real-time path as a public endpoint: scales to zero when idle, so it costs nothing
# at rest. Session state needs Redis, which a free demo does not pay for, so here the
# stateless POST /recommend is the one to call; the per-session routes keep their state
# per instance.
resource "google_service_account" "api" {
  account_id   = "recs-api"
  display_name = "marketplace-recs API on Cloud Run; calls no Google API, so holds no role"
  depends_on   = [google_project_service.api["iam.googleapis.com"]]
}

resource "google_cloud_run_v2_service" "api" {
  count               = var.api_image == "" ? 0 : 1
  name                = "recs-api"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.api.email
    timeout         = "10s"

    scaling {
      min_instance_count = 0
      max_instance_count = var.api_max_instances
    }

    containers {
      image = var.api_image
      ports {
        container_port = 8080
      }
      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        # CPU only while a request is in flight: billed per request, not per instance-hour.
        cpu_idle          = true
        startup_cpu_boost = true
      }
      # The model loads before the server listens, so a new instance takes traffic only
      # once it can rank.
      startup_probe {
        http_get {
          path = "/ready"
        }
        period_seconds    = 1
        timeout_seconds   = 1
        failure_threshold = 30
      }
    }
  }

  depends_on = [google_project_service.api["run.googleapis.com"]]
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = length(google_cloud_run_v2_service.api)
  name     = google_cloud_run_v2_service.api[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}
