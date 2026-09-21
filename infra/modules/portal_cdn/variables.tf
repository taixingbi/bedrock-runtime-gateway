variable "name_prefix" {
  description = "Prefix applied to resource names/tags, e.g. \"gateway-dev-portal\"."
  type        = string
}

variable "environment" {
  type = string
}

variable "origin_domain_name" {
  description = "The portal ALB's DNS name (module.portal_service's alb_dns_name output)."
  type        = string
}
