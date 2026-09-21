output "domain_name" {
  description = "e.g. d111111abcdef8.cloudfront.net -- use as PORTAL_BASE_URL with an https:// prefix."
  value       = aws_cloudfront_distribution.this.domain_name
}
