output "project_number" {
  value = google_project.this.number
}

output "budget" {
  value = google_billing_budget.guard.name
}

output "registry" {
  description = "Where images are pushed"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "api_image" {
  value = var.api_image
}

output "api_url" {
  value = one(google_cloud_run_v2_service.api[*].uri)
}

output "dataset" {
  value = "${var.project_id}.${google_bigquery_dataset.recs.dataset_id}"
}

output "project_id" {
  value = google_project.this.project_id
}
