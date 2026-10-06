resource "google_artifact_registry_repository" "images" {
  repository_id = "recs"
  location      = var.region
  format        = "DOCKER"
  description   = "The API image, served by Cloud Run and GKE, and the k6 image that loads it"

  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-five-per-image"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }
  cleanup_policies {
    id     = "delete-older"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "604800s"
    }
  }

  depends_on = [google_project_service.api["artifactregistry.googleapis.com"]]
}
