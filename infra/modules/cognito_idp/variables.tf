variable "name_prefix" {
  description = "Prefix applied to resource names, e.g. \"gateway-dev\"."
  type        = string
}

variable "aws_region" {
  type = string
}

variable "callback_url" {
  description = "Exact OIDC redirect_uri the portal's callback route listens on, e.g. \"http://<portal-alb-dns>/api/auth/callback\". Cognito requires an exact match, no wildcards -- see the caller for why this is a plain string, not a module-output reference."
  type        = string
}

variable "logout_url" {
  description = "Where Cognito's Hosted UI /logout redirects back to, e.g. the portal's login page."
  type        = string
}
