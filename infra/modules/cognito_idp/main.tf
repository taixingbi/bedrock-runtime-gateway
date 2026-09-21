# Human identity provider (Cognito) for the self-service portal --
# separate from the service-to-service AWS_IAM/SigV4 path
# (modules/github_oidc, auth/aws_iam.py's IamTenantResolver), which
# already exists and needs nothing added here. Human user -> Cognito ->
# OIDC JWT -> portal; service/application -> IAM role/STS/SigV4 ->
# gateway. Two different trust mechanisms for two different kinds of
# caller, not one flow doing both.
#
# admin_create_user_config.allow_admin_create_user_only = true: no
# public self-signup. Admins are provisioned (see the caller's
# aws_cognito_user resources), not registered.

data "aws_caller_identity" "current" {}

resource "aws_cognito_user_pool" "this" {
  name = "${var.name_prefix}-admins"

  # Both custom attributes flow into the ID token's custom:* claims
  # (auth/identity.py's identity_from_claims reads them from there) --
  # mutable so an admin's tenant/application assignment can change
  # without recreating the user.
  schema {
    name                = "tenant_id"
    attribute_data_type = "String"
    mutable             = true
    required            = false
  }
  schema {
    name                = "application_id"
    attribute_data_type = "String"
    mutable             = true
    required            = false
  }

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = false
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  auto_verified_attributes = ["email"]

  tags = {
    Environment = var.name_prefix
  }

  # Cognito schema attributes are immutable after pool creation -- AWS
  # silently fills in StringAttributeConstraints defaults we didn't
  # specify, which the provider then sees as permanent drift on every
  # refresh and tries to "update", something Cognito always rejects
  # ("cannot modify or remove schema items"). There is no legitimate
  # in-place schema change to make here; ignore it.
  lifecycle {
    ignore_changes = [schema]
  }
}

# Cognito Hosted UI domains share one global namespace across every AWS
# account, not just this one -- account id suffix keeps this from
# colliding with someone else's "gateway-dev-portal".
resource "aws_cognito_user_pool_domain" "this" {
  domain       = "${var.name_prefix}-portal-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.this.id
}

resource "aws_cognito_user_pool_client" "portal" {
  name         = "${var.name_prefix}-portal"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret = true # confidential client -- the portal exchanges the auth code server-side, never in the browser

  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]

  callback_urls = [var.callback_url]
  logout_urls   = [var.logout_url]

  read_attributes = ["email", "custom:tenant_id", "custom:application_id"]

  # No write_attributes: admins are provisioned via aws_cognito_user
  # (see the caller), never self-service, so the client needs no
  # attribute-update permission.
  write_attributes = []

  # Debugging-only escape hatch, not part of the real login flow (that's
  # the Hosted UI/Authorization Code flow above): lets an operator with
  # AWS credentials mint a real token via AdminInitiateAuth to inspect
  # actual ID token claims directly, without a browser.
  explicit_auth_flows = ["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]

  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 12
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "hours"
  }
}

# Group membership is what identity_from_claims reads as `roles` (the
# ID token's real cognito:groups array claim) -- a custom attribute
# couldn't hold this, Cognito custom attributes are scalar-only.
resource "aws_cognito_user_group" "platform_admin" {
  name         = "platform_admin"
  user_pool_id = aws_cognito_user_pool.this.id
  description  = "Grants the platform_admin role claim used by the gateway's admin API (settings.admin_required_role)"
}
