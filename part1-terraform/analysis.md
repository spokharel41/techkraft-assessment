# Part 1: Terraform Infrastructure Analysis

## Security Issues (8 found)

### 1. SSH Open to the World (CRITICAL)
```hcl
ingress {
  from_port   = 22
  to_port     = 22
  protocol    = "tcp"
  cidr_blocks = ["0.0.0.0/0"]  # ❌ NEVER do this
}
```
**Why this is dangerous:** Anyone on the internet can attempt SSH brute-force or exploit SSH vulnerabilities against your servers. In 2024, exposed SSH ports are one of the #1 vectors for initial compromise.

**Fix:** Restrict to your office IP or VPN CIDR, or better yet, use AWS Systems Manager Session Manager and remove SSH entirely.
```hcl
cidr_blocks = ["10.0.0.0/8"]  # or your VPN CIDR
```

---

### 2. Hardcoded Database Password (CRITICAL)
```hcl
password = "changeme123"  # ❌ Plain text secret in code
```
**Why this is dangerous:** Anyone with access to the repo, Terraform state, or CI/CD logs can see this password. State files are often stored in S3 without proper access control.

**Fix:** Use AWS Secrets Manager or SSM Parameter Store:
```hcl
password = data.aws_secretsmanager_secret_version.db_password.secret_string
```

---

### 3. No Database Encryption at Rest
The RDS instance has no `storage_encrypted = true`. If an AWS employee or attacker accessed the underlying storage, data would be readable.

**Fix:**
```hcl
resource "aws_db_instance" "mysql" {
  storage_encrypted = true
  kms_key_id        = aws_kms_key.rds.arn
}
```

---

### 4. S3 Bucket Has No Versioning, Encryption, or Public Access Block
```hcl
resource "aws_s3_bucket" "uploads" {
  bucket = "techkraft-uploads"  # Bare minimum config
}
```
No encryption, no versioning, no public access block. In AWS, new buckets can be made public accidentally.

**Fix:**
```hcl
resource "aws_s3_bucket_versioning" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket                  = aws_s3_bucket.uploads.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
```

---

### 5. RDS Instance is Not in a Private Subnet
The RDS uses the same security group as web servers and is implicitly placed in a public subnet. Databases should never be publicly routable.

**Fix:** Create private subnets and a DB subnet group:
```hcl
resource "aws_db_subnet_group" "main" {
  name       = "techkraft-db-subnet-group"
  subnet_ids = aws_subnet.private[*].id
}
```

---

### 6. No Deletion Protection on Database
```hcl
deletion_protection = false  # ❌ One terraform destroy = database gone
```
**Fix:** Set `deletion_protection = true` for production. Require a manual step to disable it before destruction.

---

### 7. No Backup Retention
```hcl
backup_retention_period = 0  # ❌ Zero backups
```
If the database crashes or data is corrupted, you have no recovery point. For production, minimum 7 days.

**Fix:**
```hcl
backup_retention_period = 7
backup_window           = "03:00-04:00"  # 3-4 AM Nepal time (UTC+5:45)
```

---

### 8. EC2 Instances Have No IAM Role / Instance Profile
No IAM roles are attached, so applications either can't access AWS APIs at all, or worse — developers bake access keys into the application code or AMIs.

**Fix:**
```hcl
resource "aws_iam_instance_profile" "web" {
  name = "web-instance-profile"
  role = aws_iam_role.web.name
}

resource "aws_instance" "web" {
  iam_instance_profile = aws_iam_instance_profile.web.name
  # ...
}
```

---

## Architectural Problems (7 found)

### 1. No Private Subnets — Everything is Public
All resources (EC2, RDS) are on public subnets. A proper VPC should have:
- **Public subnets**: Load balancers only
- **Private subnets**: Application servers
- **Isolated/DB subnets**: Databases with no internet route

```
Internet → ALB (public subnet) → EC2 (private subnet) → RDS (isolated subnet)
```

---

### 2. No Application Load Balancer
Traffic goes directly to EC2 instances. This means:
- No health checks / automatic failover
- No TLS termination in one place
- Users hit raw IP addresses — no session stickiness, no path-based routing

**Fix:** Add an ALB in front of the EC2 instances.

---

### 3. Hardcoded AMI ID
```hcl
ami = "ami-0c55b159cbfafe1f0"
```
This AMI is region-specific and may be outdated or deprecated. In `ap-south-1` (closest to Nepal), this AMI doesn't even exist.

**Fix:** Use a data source:
```hcl
data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}
```

---

### 4. No State Backend Configured
No `terraform { backend "s3" {} }` block means state is stored locally. Team members can't collaborate, and state will be lost if the laptop is lost.

**Fix:**
```hcl
terraform {
  backend "s3" {
    bucket         = "techkraft-terraform-state"
    key            = "production/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "terraform-state-lock"
  }
}
```

---

### 5. No NAT Gateway for Private Subnet Outbound Traffic
If we add private subnets (which we must), instances there have no way to reach the internet for OS updates, package installs, or calling external APIs. We need NAT Gateways in each AZ.

---

### 6. Single Security Group Shared Between Web and Database
The RDS uses `aws_security_group.web` — the same group that allows HTTP/80 from the internet. The database should have its own security group that only allows MySQL/3306 from the web tier security group.

---

### 7. Missing Outputs — Critical Values Not Exported
```hcl
output "web_ips" {
  value = aws_instance.web[*].private_ip  # Only private IPs, no public
}
```
Missing: ALB DNS name, RDS endpoint, S3 bucket ARN, VPC ID, subnet IDs. These are needed by other teams and by application configuration.

---

## Production-Ready Changes Summary

| Priority | Change | Reason |
|----------|--------|--------|
| P0 | Remove SSH/0.0.0.0/0, use SSM | Active attack surface |
| P0 | Move DB password to Secrets Manager | Credential exposure |
| P0 | Enable deletion_protection + backups | Data loss prevention |
| P1 | Add private + DB subnets | Network segmentation |
| P1 | Add Application Load Balancer | HA + TLS termination |
| P1 | Configure S3 remote state backend | Team collaboration |
| P1 | Enable RDS encryption | Data at rest |
| P2 | Add CloudWatch alarms | Observability |
| P2 | Add IAM roles to EC2 | Least privilege |
| P2 | Use data source for AMI | Multi-region portability |

### Refactored Production Terraform (Key Additions)

```hcl
# variables.tf - Parameterize everything
variable "environment" { default = "production" }
variable "vpc_cidr"    { default = "10.0.0.0/16" }

locals {
  azs             = ["ap-south-1a", "ap-south-1b"]  # Mumbai - closest to Nepal
  public_cidrs    = ["10.0.1.0/24", "10.0.2.0/24"]
  private_cidrs   = ["10.0.11.0/24", "10.0.12.0/24"]
  db_cidrs        = ["10.0.21.0/24", "10.0.22.0/24"]
}

# Private subnets
resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.private_cidrs[count.index]
  availability_zone = local.azs[count.index]
  tags = { Name = "private-${local.azs[count.index]}" }
}

# NAT Gateways (one per AZ for HA)
resource "aws_nat_gateway" "main" {
  count         = 2
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
}

# ALB
resource "aws_lb" "main" {
  name               = "techkraft-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
}
```
