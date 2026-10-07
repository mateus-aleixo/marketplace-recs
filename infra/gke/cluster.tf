# Nodes run as an account of their own that can write logs and metrics and pull from the
# registry, not as the Compute Engine default account with Editor on the project.
resource "google_service_account" "nodes" {
  account_id   = "gke-nodes"
  display_name = "GKE Autopilot nodes: logs, metrics and image pulls"
}

resource "google_project_iam_member" "nodes" {
  project = local.project
  role    = "roles/container.defaultNodeServiceAccount"
  member  = google_service_account.nodes.member
}

resource "google_artifact_registry_repository_iam_member" "nodes" {
  location   = var.region
  repository = "recs"
  role       = "roles/artifactregistry.reader"
  member     = google_service_account.nodes.member
}

# Autopilot: Google runs the nodes and bills the pods' requests. Torn down after each test.
resource "google_container_cluster" "load" {
  name                = "recs-load"
  location            = var.region
  enable_autopilot    = true
  deletion_protection = false

  release_channel {
    channel = "REGULAR"
  }

  cluster_autoscaling {
    auto_provisioning_defaults {
      service_account = google_service_account.nodes.email
      oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    }
  }

  depends_on = [
    google_project_iam_member.nodes,
    google_artifact_registry_repository_iam_member.nodes,
  ]
}
