variable "repository_name" {
  description = "ECR repository name."
  type        = string
}

variable "environment" {
  description = "Environment tag, e.g. \"dev\" or \"prod\"."
  type        = string
}

variable "max_image_count" {
  description = "Number of tagged images to retain before older ones expire."
  type        = number
  default     = 20
}
