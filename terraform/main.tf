terraform {
  required_version = ">= 1.6"
  required_providers {
    aws     = { source = "hashicorp/aws", version = ">= 5.90, < 7.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "me" {}
data "aws_vpc" "default" { default = true }
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

locals {
  namespace = "WhatsAppAgent"
}

# ---------------- State: DynamoDB single table ----------------
resource "aws_dynamodb_table" "agent" {
  name         = var.name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"
  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
  point_in_time_recovery { enabled = true }
}

# ---------------- RAG: Aurora Serverless v2 + pgvector, accessed via Data API ----------------
resource "aws_db_subnet_group" "kb" {
  name       = "${var.name}-kb"
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_rds_cluster" "kb" {
  cluster_identifier          = "${var.name}-kb"
  engine                      = "aurora-postgresql"
  engine_version              = "16.8"
  database_name               = "agent"
  master_username             = "agent_admin"
  manage_master_user_password = true # credentials live in Secrets Manager, never in state/code
  enable_http_endpoint        = true # Data API: Lambda needs no VPC attachment / NAT / pooling
  storage_encrypted           = true
  db_subnet_group_name        = aws_db_subnet_group.kb.name
  skip_final_snapshot         = true
  serverlessv2_scaling_configuration {
    min_capacity             = 0 # auto-pause when idle => near-zero cost for a demo
    max_capacity             = 2
    seconds_until_auto_pause = 300
  }
}

resource "aws_rds_cluster_instance" "kb" {
  identifier         = "${var.name}-kb-1"
  cluster_identifier = aws_rds_cluster.kb.id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.kb.engine
  engine_version     = aws_rds_cluster.kb.engine_version
}

# ---------------- Secrets + notifications ----------------
resource "aws_secretsmanager_secret" "app" {
  name                    = "${var.name}/app"
  description             = "JSON: verify_token, app_secret, access_token, phone_number_id, api_key"
  recovery_window_in_days = 0
}

resource "aws_sns_topic" "handoff" { name = "${var.name}-handoff" }
resource "aws_sns_topic" "alerts" { name = "${var.name}-alerts" }
resource "aws_sns_topic_subscription" "handoff_email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.handoff.arn
  protocol  = "email"
  endpoint  = var.alert_email
}
resource "aws_sns_topic_subscription" "alerts_email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------------- Lambda ----------------
resource "aws_iam_role" "lambda" {
  name = "${var.name}-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "app" {
  name = "app"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "Bedrock", Effect = "Allow", Action = ["bedrock:InvokeModel"],
        Resource = ["arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:*:${data.aws_caller_identity.me.account_id}:inference-profile/*"] },
      { Sid = "State", Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
        Resource = aws_dynamodb_table.agent.arn },
      { Sid = "DataApi", Effect = "Allow", Action = ["rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement"],
        Resource = aws_rds_cluster.kb.arn },
      { Sid = "Secrets", Effect = "Allow", Action = ["secretsmanager:GetSecretValue"],
        Resource = [aws_secretsmanager_secret.app.arn, aws_rds_cluster.kb.master_user_secret[0].secret_arn] },
      { Sid = "Handoff", Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.handoff.arn },
    ]
  })
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = 30
}

resource "aws_lambda_function" "agent" {
  function_name    = var.name
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "app.main.handler"
  filename         = "${path.module}/../dist/lambda.zip" # run scripts/build_lambda.sh first
  source_code_hash = filebase64sha256("${path.module}/../dist/lambda.zip")
  timeout          = 60 # headroom for Aurora resuming from auto-pause
  memory_size      = 512
  depends_on       = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.app]

  environment {
    variables = {
      TABLE_NAME        = aws_dynamodb_table.agent.name
      DB_CLUSTER_ARN    = aws_rds_cluster.kb.arn
      DB_SECRET_ARN     = aws_rds_cluster.kb.master_user_secret[0].secret_arn
      DB_NAME           = "agent"
      APP_SECRETS_ARN   = aws_secretsmanager_secret.app.arn
      HANDOFF_TOPIC_ARN = aws_sns_topic.handoff.arn
      PRIMARY_MODEL     = var.primary_model
      FALLBACK_MODEL    = var.fallback_model
      DAILY_BUDGET_USD    = tostring(var.daily_budget_usd)
      RETRIEVAL_MIN_SCORE = tostring(var.retrieval_min_score)
      METRIC_NAMESPACE  = local.namespace
      SERVICE_NAME      = var.name
    }
  }
}

# Public HTTPS endpoint for the Meta webhook (auth = HMAC signature verified in code)
resource "aws_lambda_function_url" "agent" {
  function_name      = aws_lambda_function.agent.function_name
  authorization_type = "NONE"
  cors {
    allow_origins = ["*"]
    allow_methods = ["*"]
    allow_headers = ["content-type", "x-api-key"]
    max_age       = 3600
  }
}

resource "aws_lambda_permission" "url_invoke" {
  statement_id           = "AllowPublicUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.agent.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "url_invoke_fn" {
  statement_id             = "AllowPublicUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.agent.function_name
  principal                = "*"
  invoked_via_function_url = true
}

# ---------------- Observability: dashboard + alarms (metrics come from EMF log lines) ----------------
resource "aws_cloudwatch_dashboard" "agent" {
  dashboard_name = var.name
  dashboard_body = jsonencode({
    widgets = [
      { type = "metric", x = 0, y = 0, width = 12, height = 6, properties = {
        title = "Latency p50 / p95 (ms)", region = var.region, view = "timeSeries", period = 300,
        metrics = [[local.namespace, "LatencyMs", "Service", var.name, { stat = "p50" }],
        ["...", { stat = "p95" }]] } },
      { type = "metric", x = 12, y = 0, width = 12, height = 6, properties = {
        title = "Cost (USD) per 5 min", region = var.region, view = "timeSeries", period = 300, stat = "Sum",
      metrics = [[local.namespace, "CostUSD", "Service", var.name]] } },
      { type = "metric", x = 0, y = 6, width = 12, height = 6, properties = {
        title = "Turns vs handoffs vs ungrounded answers", region = var.region, view = "timeSeries", period = 300, stat = "Sum",
        metrics = [[local.namespace, "Turns", "Service", var.name], [".", "Handoff", ".", "."], [".", "UngroundedAnswer", ".", "."]] } },
      { type = "metric", x = 12, y = 6, width = 12, height = 6, properties = {
        title = "Fallback model use & LLM outages", region = var.region, view = "timeSeries", period = 300, stat = "Sum",
        metrics = [[local.namespace, "FallbackModel", "Service", var.name], [".", "Errors", ".", "."]] } },
      { type = "metric", x = 0, y = 12, width = 12, height = 6, properties = {
        title = "Tokens", region = var.region, view = "timeSeries", period = 300, stat = "Sum",
        metrics = [[local.namespace, "InputTokens", "Service", var.name], [".", "OutputTokens", ".", "."]] } },
    ]
  })
}

resource "aws_cloudwatch_metric_alarm" "latency" {
  alarm_name          = "${var.name}-p95-latency"
  namespace           = local.namespace
  metric_name         = "LatencyMs"
  dimensions          = { Service = var.name }
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 2
  threshold           = 15000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "llm_outage" {
  alarm_name          = "${var.name}-llm-unavailable"
  namespace           = local.namespace
  metric_name         = "Errors"
  dimensions          = { Service = var.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 3
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "cost" {
  alarm_name          = "${var.name}-hourly-cost"
  namespace           = local.namespace
  metric_name         = "CostUSD"
  dimensions          = { Service = var.name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = var.hourly_cost_alarm_usd
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# ---------------- CI: GitHub Actions assumes a role via OIDC to run evals against Bedrock ----------------
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.github_repo == "" ? 0 : 1
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # If your account already has this provider, delete this resource and reference the existing ARN.
}

resource "aws_iam_role" "ci_evals" {
  count = var.github_repo == "" ? 0 : 1
  name  = "${var.name}-ci-evals"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
      Principal = { Federated = aws_iam_openid_connect_provider.github[0].arn },
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike   = { "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "ci_evals" {
  count = var.github_repo == "" ? 0 : 1
  name  = "bedrock-invoke"
  role  = aws_iam_role.ci_evals[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["bedrock:InvokeModel"],
      Resource = ["arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:*:${data.aws_caller_identity.me.account_id}:inference-profile/*"] }]
  })
}
