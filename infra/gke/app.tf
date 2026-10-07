# The API as the load test sees it: the Cloud Run image, one vCPU per pod, behind a
# ClusterIP service, scaled on CPU. k6 runs inside the cluster, so the test measures the
# service and not a path over the internet.
locals {
  labels = { app = "recs-api" }
}

resource "kubernetes_deployment_v1" "api" {
  metadata {
    name   = "api"
    labels = local.labels
  }

  spec {
    selector {
      match_labels = local.labels
    }

    template {
      metadata {
        labels = local.labels
      }

      spec {
        container {
          name  = "api"
          image = local.image

          port {
            container_port = 8080
          }

          env {
            name  = "WEB_CONCURRENCY"
            value = "1"
          }
          # LightGBM's OpenMP otherwise starts a thread per core of the node, and those
          # threads fight over the pod's one vCPU.
          env {
            name  = "OMP_NUM_THREADS"
            value = "1"
          }

          # Requests equal limits, as Autopilot bills them; it would add the 1Gi of
          # ephemeral storage itself if it were left out.
          resources {
            requests = {
              cpu                 = "1"
              memory              = "1Gi"
              "ephemeral-storage" = "1Gi"
            }
            limits = {
              cpu                 = "1"
              memory              = "1Gi"
              "ephemeral-storage" = "1Gi"
            }
          }

          # uvicorn listens only once the model is loaded, so an accepted TCP connection
          # means ready to rank, and the kernel completes the handshake even while the
          # event loop is busy ranking. Probes over HTTP, on /health or /ready, wait behind
          # that work, and took saturated replicas out of rotation (README, finding 8).
          readiness_probe {
            tcp_socket {
              port = 8080
            }
            period_seconds    = 2
            failure_threshold = 3
          }
        }
      }
    }
  }

  # The autoscaler owns the replica count. Autopilot's admission controller annotates what
  # it admitted and adds a security context and an architecture toleration to the pods.
  lifecycle {
    ignore_changes = [
      spec[0].replicas,
      metadata[0].annotations,
      spec[0].template[0].spec[0].security_context,
      spec[0].template[0].spec[0].toleration,
      spec[0].template[0].spec[0].container[0].security_context,
    ]
  }
}

resource "kubernetes_service_v1" "api" {
  metadata {
    name = "api"
  }

  spec {
    selector = local.labels
    port {
      port        = 80
      target_port = 8080
    }
  }
}

resource "kubernetes_horizontal_pod_autoscaler_v2" "api" {
  metadata {
    name = "api"
  }

  spec {
    min_replicas = var.min_pods
    max_replicas = var.max_pods

    scale_target_ref {
      api_version = "apps/v1"
      kind        = "Deployment"
      name        = kubernetes_deployment_v1.api.metadata[0].name
    }

    metric {
      type = "Resource"
      resource {
        name = "cpu"
        target {
          type                = "Utilization"
          average_utilization = var.cpu_target
        }
      }
    }

    # Scale-up keeps Kubernetes' defaults; scale-down waits a minute instead of five, so
    # a test's steps down are visible within it.
    behavior {
      scale_down {
        stabilization_window_seconds = 60
        select_policy                = "Max"
        policy {
          type           = "Percent"
          value          = 100
          period_seconds = 15
        }
      }
    }
  }
}
