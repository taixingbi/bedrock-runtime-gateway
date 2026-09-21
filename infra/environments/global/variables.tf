variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "github_org" {
  description = "GitHub organization/user that owns all three split repos, e.g. \"taixingbi\"."
  type        = string
}
