output "user_pool_id" {
  value = aws_cognito_user_pool.this.id
}

output "user_pool_endpoint" {
  description = "Issuer for OIDC_ISSUER / JWKS base: https://<this>/.well-known/jwks.json"
  value       = "https://${aws_cognito_user_pool.this.endpoint}"
}

output "client_id" {
  value = aws_cognito_user_pool_client.portal.id
}

output "client_secret" {
  value     = aws_cognito_user_pool_client.portal.client_secret
  sensitive = true
}

output "hosted_ui_domain" {
  description = "e.g. gateway-dev-portal-646821141010.auth.us-east-1.amazoncognito.com"
  value       = "${aws_cognito_user_pool_domain.this.domain}.auth.${var.aws_region}.amazoncognito.com"
}

output "platform_admin_group_name" {
  value = aws_cognito_user_group.platform_admin.name
}
