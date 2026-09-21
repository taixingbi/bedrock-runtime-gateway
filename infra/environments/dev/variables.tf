variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "desired_count" {
  type    = number
  default = 2 # plan section 35.6 -- real HA floor, matches ecs_service's autoscaling_min_capacity
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
