output "app_deploy_role_arns" {
  description = "Set as AWS_APP_DEPLOY_ROLE_ARN_DEV / _PROD in this repo's own GitHub Environment variables -- unchanged from before this migration."
  value       = module.github_oidc_app.role_arns
}

output "infra_role_arns" {
  description = "Set as AWS_INFRA_PLAN_ROLE_ARN / AWS_INFRA_APPLY_DEV_ROLE_ARN / AWS_INFRA_APPLY_PROD_ROLE_ARN in this repo's own GitHub Environment variables -- unchanged from before this migration."
  value       = module.github_oidc_infra.role_arns
}
