# ---------------- Public demo: S3 (static UI) + CloudFront (serves page, proxies API, injects key at edge) ----------------
# Only created when var.demo_api_key is set.
locals {
  demo_enabled = var.demo_api_key == "" ? 0 : 1
  # Lambda Function URL host (strip scheme + trailing slash) for use as a CloudFront custom origin.
  lambda_host = replace(replace(aws_lambda_function_url.agent.function_url, "https://", ""), "/", "")
}

resource "aws_s3_bucket" "demo" {
  count         = local.demo_enabled
  bucket        = "${var.name}-demo-${data.aws_caller_identity.me.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "demo" {
  count                   = local.demo_enabled
  bucket                  = aws_s3_bucket.demo[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "index" {
  count        = local.demo_enabled
  bucket       = aws_s3_bucket.demo[0].id
  key          = "index.html"
  source       = "${path.module}/../demo/index.html"
  etag         = filemd5("${path.module}/../demo/index.html")
  content_type = "text/html; charset=utf-8"
}

resource "aws_cloudfront_origin_access_control" "demo" {
  count                             = local.demo_enabled
  name                              = "${var.name}-demo-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "demo" {
  count               = local.demo_enabled
  enabled             = true
  default_root_object = "index.html"
  comment             = "${var.name} demo UI"

  # Origin 1: the static site in S3
  origin {
    origin_id                = "s3"
    domain_name              = aws_s3_bucket.demo[0].bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.demo[0].id
  }

  # Origin 2: the Lambda Function URL, with the API key injected as a header at the edge
  origin {
    origin_id   = "lambda"
    domain_name = local.lambda_host
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
    custom_header {
      name  = "x-api-key"
      value = var.demo_api_key
    }
  }

  # Default: serve the static page from S3
  default_cache_behavior {
    target_origin_id       = "s3"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = "658327ea-f89d-4fab-a63d-7e88639e58f6" # Managed-CachingOptimized
  }

  # /chat -> Lambda (POST, no caching, forward everything except Host)
  ordered_cache_behavior {
    path_pattern             = "/chat"
    target_origin_id         = "lambda"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # Managed-CachingDisabled
    origin_request_policy_id = "b689b0a8-53d0-40ab-baf2-68738e2966ac" # Managed-AllViewerExceptHostHeader
  }

  # /reset -> Lambda
  ordered_cache_behavior {
    path_pattern             = "/reset"
    target_origin_id         = "lambda"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad" # Managed-CachingDisabled
    origin_request_policy_id = "b689b0a8-53d0-40ab-baf2-68738e2966ac" # Managed-AllViewerExceptHostHeader
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }
  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

# Bucket policy: allow only this CloudFront distribution (via OAC) to read objects
data "aws_iam_policy_document" "demo_bucket" {
  count = local.demo_enabled
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.demo[0].arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.demo[0].arn]
    }
  }
}

resource "aws_s3_bucket_policy" "demo" {
  count  = local.demo_enabled
  bucket = aws_s3_bucket.demo[0].id
  policy = data.aws_iam_policy_document.demo_bucket[0].json
}

output "demo_url" {
  value       = nonsensitive(length(aws_cloudfront_distribution.demo) > 0 ? "https://${aws_cloudfront_distribution.demo[0].domain_name}" : "set -var demo_api_key=... to enable")
  description = "Public shareable demo link"
}
