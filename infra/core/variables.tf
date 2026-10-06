variable "project_id" {
  description = "ID of the project this stack creates and deploys into"
  type        = string
}

variable "billing_account" {
  description = "Billing account the project is charged to, and the budget watches"
  type        = string
}

variable "region" {
  description = "Region for every regional resource"
  type        = string
  default     = "europe-west1"
}

variable "api_image" {
  description = "The API image, by digest. Empty until the first push: the service is created by the apply after it"
  type        = string
  default     = ""
}

variable "api_max_instances" {
  description = "Cloud Run instances at most: the public endpoint's ceiling on cost"
  type        = number
  default     = 2
}
