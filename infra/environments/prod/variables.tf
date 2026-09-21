variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "desired_count" {
  description = "Prod defaults to 2 for basic availability across AZs."
  type        = number
  default     = 2
}

variable "task_cpu" {
  type    = number
  default = 512
}

variable "task_memory" {
  type    = number
  default = 1024
}

variable "bedrock_model_ids" {
  description = "Must match policies/route_sets.yaml so the task role can invoke every model a route set might select."
  type        = list(string)
  default     = ["us.amazon.nova-micro-v1:0"]
}
