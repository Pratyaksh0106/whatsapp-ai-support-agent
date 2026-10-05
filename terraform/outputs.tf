output "webhook_url" {
  description = "Paste into Meta > WhatsApp > Configuration > Callback URL (append /webhook)."
  value       = "${aws_lambda_function_url.agent.function_url}webhook"
}
output "chat_url" { value = "${aws_lambda_function_url.agent.function_url}chat" }
output "db_cluster_arn" { value = aws_rds_cluster.kb.arn }
output "db_secret_arn" { value = aws_rds_cluster.kb.master_user_secret[0].secret_arn }
output "app_secret_arn" { value = aws_secretsmanager_secret.app.arn }
output "table_name" { value = aws_dynamodb_table.agent.name }
output "dashboard" { value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards:name=${var.name}" }
output "ci_role_arn" { value = try(aws_iam_role.ci_evals[0].arn, null) }
