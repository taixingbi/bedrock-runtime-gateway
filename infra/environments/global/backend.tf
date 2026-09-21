terraform {
  backend "s3" {
    bucket         = "bedrock-gateway-tfstate-646821141010"
    key            = "bedrock-gateway-infra/global/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "bedrock-gateway-tfstate-lock"
    encrypt        = true
  }
}
