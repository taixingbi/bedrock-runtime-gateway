# HTTPS in front of the portal's (deliberately) HTTP-only ALB --
# Cognito's Hosted UI hard-rejects any callback/logout URL that isn't
# https:// (the sole exception, http://localhost, isn't usable for a
# real deployed URL). CloudFront gives a *.cloudfront.net domain with
# a valid, free default certificate, no owned domain or ACM DNS
# validation needed -- appropriate for this MVP-sized internal tool.
#
# CloudFront-to-ALB stays plain HTTP (origin_protocol_policy =
# "http-only"); only the browser<->CloudFront hop is encrypted. That's
# what actually matters for the session cookie: the Secure flag
# governs what the browser does, and the browser only ever talks to
# CloudFront.

data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

resource "aws_cloudfront_distribution" "this" {
  enabled     = true
  comment     = "${var.name_prefix} portal"
  price_class = "PriceClass_100" # cheapest tier -- US/Europe edge locations, fine for an internal admin tool

  origin {
    domain_name = var.origin_domain_name
    origin_id   = "alb"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    allowed_methods  = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = "alb"

    viewer_protocol_policy = "redirect-to-https"
    compress               = true

    # Fully dynamic app (Server Components/Actions, cookie-based
    # session) -- nothing here is cacheable, and every header/cookie/
    # query param must reach the origin unmodified (Next.js Server
    # Actions validate the Origin header, for one).
    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = {
    Environment = var.environment
  }
}
