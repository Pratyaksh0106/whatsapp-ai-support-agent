variable "region" {
  type    = string
  default = "us-east-1"
}
variable "name" {
  type    = string
  default = "wa-agent"
}
variable "primary_model" {
  type    = string
  default = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}
variable "fallback_model" {
  type    = string
  default = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
}
variable "alert_email" {
  description = "Email for handoff + alarm notifications (confirm the SNS subscription from your inbox)."
  type        = string
  default     = ""
}
variable "daily_budget_usd" {
  type    = number
  default = 20
}
variable "retrieval_min_score" {
  description = "Cosine similarity floor for RAG hits. Titan v2 scores good matches ~0.36-0.49, so 0.30 gives margin."
  type        = number
  default     = 0.30
}
variable "hourly_cost_alarm_usd" {
  type    = number
  default = 5
}
variable "demo_api_key" {
  description = "API key CloudFront injects as x-api-key when proxying /chat and /reset (so it's never in the browser). Must match the api_key in the app secret."
  type        = string
  default     = ""
  sensitive   = true
}
variable "github_repo" {
  description = "owner/repo allowed to assume the CI eval role via OIDC. Empty = skip."
  type        = string
  default     = ""
}
