terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.6"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  default_labels = {
    app        = "marketplace-recs"
    managed_by = "terraform"
  }
}

# The Budget API charges its quota to a project, and user credentials name none, so the
# budget goes through a provider that names this one.
provider "google" {
  alias                 = "billing"
  billing_project       = var.project_id
  user_project_override = true
}
