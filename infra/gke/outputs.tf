output "cluster" {
  value = google_container_cluster.load.name
}

output "credentials" {
  description = "Points kubectl at the cluster"
  value       = "gcloud container clusters get-credentials ${google_container_cluster.load.name} --region ${var.region} --project ${local.project}"
}
