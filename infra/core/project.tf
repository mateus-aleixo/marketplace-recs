# A project of its own, so the deployment is billed, listed and removed as one.
resource "google_project" "this" {
  project_id      = var.project_id
  name            = "marketplace-recs"
  billing_account = var.billing_account
  deletion_policy = "PREVENT"
}

locals {
  services = [
    "artifactregistry.googleapis.com",
    "bigquery.googleapis.com",
    "bigquerystorage.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudbilling.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
  ]
}

resource "google_project_service" "api" {
  for_each           = toset(local.services)
  project            = google_project.this.project_id
  service            = each.value
  disable_on_destroy = false
}
