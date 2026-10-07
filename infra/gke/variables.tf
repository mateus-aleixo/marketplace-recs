variable "region" {
  description = "Region of the cluster: the same as the registry and Cloud Run"
  type        = string
  default     = "europe-west1"
}

variable "min_pods" {
  description = "API pods the autoscaler keeps at least"
  type        = number
  default     = 1
}

# On the free trial, the project gets two nodes at most (README), which hold five API pods
# beside the k6 Job.
variable "max_pods" {
  description = "API pods the autoscaler may run at most"
  type        = number
  default     = 5
}

variable "cpu_target" {
  description = "Average CPU utilisation, in percent of the request, the autoscaler holds"
  type        = number
  default     = 70
}
