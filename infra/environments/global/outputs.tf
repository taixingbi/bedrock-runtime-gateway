output "app_deploy_role_arns" {
  description = "Set as AWS_APP_DEPLOY_ROLE_ARN_DEV / _PROD in this repo's own GitHub Environment variables (app-ci.yml/app-promote-prod.yml)."
  value       = module.github_oidc_app.role_arns
}

output "infra_role_arns" {
  description = "Set as AWS_INFRA_PLAN_ROLE_ARN / AWS_INFRA_APPLY_ROLE_ARN in this repo's own GitHub Environment variables (infra-ci.yml/infra-promote-prod.yml)."
  value       = module.github_oidc_infra.role_arns
}

output "policy_publish_role_arn" {
  description = "Set as AWS_POLICY_PUBLISH_ROLE_ARN in platform-policy-definitions's GitHub Environment variables."
  value       = module.github_oidc_policies.role_arns["publish"]
}

output "portal_deploy_role_arns" {
  description = "Set as AWS_PORTAL_DEPLOY_ROLE_ARN_DEV / _PROD in bedrock-gateway-portal's GitHub Environment variables."
  value       = module.github_oidc_portal.role_arns
}

output "authz_deploy_role_arns" {
  description = "Set as AWS_AUTHZ_DEPLOY_ROLE_ARN_DEV / _PROD in platform-authz-service's GitHub Environment variables."
  value       = module.github_oidc_authz.role_arns
}

output "api_gateway_role_arns" {
  description = "Set as AWS_API_GATEWAY_PLAN_ROLE_ARN / AWS_API_GATEWAY_APPLY_DEV_ROLE_ARN / AWS_API_GATEWAY_APPLY_PROD_ROLE_ARN in platform-edge-gateway's GitHub Environment variables."
  value       = module.github_oidc_api_gateway.role_arns
}
