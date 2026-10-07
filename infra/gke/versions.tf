terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.6"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.3"
    }
  }
}

# The project and the image come from the core stack, which stays up; this cluster exists
# only while a load test runs.
data "terraform_remote_state" "core" {
  backend = "local"
  config = {
    path = "${path.module}/../core/terraform.tfstate"
  }
}

locals {
  project = data.terraform_remote_state.core.outputs.project_id
  image   = data.terraform_remote_state.core.outputs.api_image
}

provider "google" {
  project = local.project
  region  = var.region
  default_labels = {
    app        = "marketplace-recs"
    managed_by = "terraform"
  }
}

data "google_client_config" "me" {}

provider "kubernetes" {
  host                   = "https://${google_container_cluster.load.endpoint}"
  token                  = data.google_client_config.me.access_token
  cluster_ca_certificate = base64decode(google_container_cluster.load.master_auth[0].cluster_ca_certificate)
}
