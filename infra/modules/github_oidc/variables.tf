variable "github_org" {
  description = "GitHub organization/user that owns the repo, e.g. \"taixingbi\"."
  type        = string
}

variable "github_repo" {
  description = "Repository name this module's roles trust, e.g. \"platform-authz-service\". One module call = one repo; call this module once per repo that needs OIDC roles."
  type        = string
}

variable "create_oidc_provider" {
  description = "Whether to create the account-wide GitHub OIDC provider. Set true on exactly one call of this module across the whole account (the resource is a singleton -- a second `aws_iam_openid_connect_provider` for the same URL will fail to create); every other call sets this false and looks the existing one up via data source."
  type        = bool
  default     = true
}

variable "roles" {
  description = <<-EOT
    Map of GitHub Environment name (repo Settings -> Environments, e.g.
    "dev"/"prod"/"plan"/"apply"/"publish") to the role that Environment's
    workflow jobs can assume. `policy_json` is the full IAM policy
    document (as JSON, e.g. from `data.aws_iam_policy_document.*.json`)
    for that role -- this module has no opinion on what a role is
    allowed to do, only on how it's assumed.
  EOT
  type = map(object({
    role_name   = string
    policy_json = string
  }))
}
