output "role_arns" {
  description = "Map of GitHub Environment name to the IAM role ARN it can assume."
  value       = { for env, role in aws_iam_role.this : env => role.arn }
}
