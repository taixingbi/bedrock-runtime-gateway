# VPC with real private-subnet isolation (plan section 35.10, P0
# production hardening -- 2026-09-19). Public subnets now hold ONLY
# the NAT gateways (one per AZ, for real HA -- a single shared NAT
# would be a new single point of failure introduced by "hardening");
# the ALB and every ECS task move to private subnets with no public
# IP and no direct route to the internet, egressing only through NAT.
#
# S3 and DynamoDB get free Gateway VPC endpoints -- this platform's
# heaviest AWS traffic (audit buckets, policy/usage/jobs tables) skips
# NAT data-processing charges entirely for those calls. Interface
# endpoints for ECR/CloudWatch Logs/Bedrock/STS ($7.2/mo + data each)
# are a further-hardening follow-up, not built here -- NAT alone
# already satisfies "no public IP, no direct internet exposure," the
# explicit ask; interface endpoints are a cost/latency optimization on
# top of that, not required for it.
#
# Real recurring cost: ~$32-45/mo per NAT gateway + data processing
# ($0.045/GB) -- with one per AZ (2 AZs by default), that's the
# dominant new line item this change adds.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name        = "${var.name_prefix}-vpc"
    Environment = var.environment
  }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name        = "${var.name_prefix}-igw"
    Environment = var.environment
  }
}

# --- Public subnets: NAT gateway placement only, nothing else lives here ---

resource "aws_subnet" "public" {
  count                   = length(var.public_subnet_cidrs)
  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name        = "${var.name_prefix}-public-${count.index}"
    Environment = var.environment
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = {
    Name        = "${var.name_prefix}-public-rt"
    Environment = var.environment
  }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# --- NAT gateways: one per AZ, each in that AZ's public subnet ---

resource "aws_eip" "nat" {
  count  = length(var.public_subnet_cidrs)
  domain = "vpc"

  tags = {
    Name        = "${var.name_prefix}-nat-eip-${count.index}"
    Environment = var.environment
  }
}

resource "aws_nat_gateway" "this" {
  count         = length(var.public_subnet_cidrs)
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = {
    Name        = "${var.name_prefix}-nat-${count.index}"
    Environment = var.environment
  }

  depends_on = [aws_internet_gateway.this]
}

# --- Private subnets: the ALB and every ECS task actually live here ---

resource "aws_subnet" "private" {
  count             = length(var.private_subnet_cidrs)
  vpc_id            = aws_vpc.this.id
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = {
    Name        = "${var.name_prefix}-private-${count.index}"
    Environment = var.environment
  }
}

# One route table per AZ, each routing 0.0.0.0/0 to that AZ's own NAT
# gateway -- keeps egress traffic inside the AZ (avoids cross-AZ data
# transfer charges on top of NAT's own per-GB charge).
resource "aws_route_table" "private" {
  count  = length(var.private_subnet_cidrs)
  vpc_id = aws_vpc.this.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[count.index].id
  }

  tags = {
    Name        = "${var.name_prefix}-private-rt-${count.index}"
    Environment = var.environment
  }
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# --- Free Gateway VPC endpoints for this platform's heaviest AWS traffic ---

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id

  tags = {
    Name        = "${var.name_prefix}-s3-endpoint"
    Environment = var.environment
  }
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id

  tags = {
    Name        = "${var.name_prefix}-dynamodb-endpoint"
    Environment = var.environment
  }
}
