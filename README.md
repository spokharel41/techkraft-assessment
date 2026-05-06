# TechKraft — Senior DevOps/Infrastructure Engineer Assignment

**Candidate:** [Your Name]  
**Email:** [your.email@gmail.com]  
**Date:** [Date you submit]  
**Total Time Spent:** ~145 minutes

---

## Overview

This repository contains my solutions for the TechKraft Senior DevOps/Infrastructure Engineer take-home assignment. Each part reflects how I would approach these problems in a real production environment — not just "the right answer" but the reasoning behind each decision.

**A note on context:** I've tailored several decisions specifically to TechKraft's Nepal-based operations — choosing Mumbai (ap-south-1) as the primary AWS region (~40ms latency from Kathmandu vs ~200ms from us-east-1), using NPT-friendly maintenance windows, and building cost-conscious solutions that make sense for a growing engineering company rather than a Fortune 500 budget.

---

## Repository Structure

```
.
├── README.md                        ← You are here
├── part1-terraform/
│   └── analysis.md                  ← Terraform code review (8 security + 7 arch issues)
├── part2-linux/
│   ├── troubleshooting.md           ← Systematic SSH debug methodology
│   ├── Dockerfile                   ← Multi-stage, non-root, production Flask
│   └── requirements.txt
├── part3-python/
│   ├── ec2_monitor.py               ← boto3 CloudWatch monitor with full CLI
│   └── config.json
├── part4-bash/
│   └── analyze_nginx_logs.sh        ← Nginx log analyzer, pure standard tools
├── part5-network/
│   └── architecture.md              ← Redundant DNS design, failover, cost analysis
├── part6-cicd/
│   └── improvements.md              ← Full production GitHub Actions pipeline
└── k8s/
    └── deployment.yaml              ← Deployment, Service, HPA, PDB
```

---

## Part Summaries

### Part 1: Terraform Analysis (30 min)
**File:** `part1-terraform/analysis.md`

Found **8 security issues** and **7 architectural problems** in the provided Terraform code. The most critical: SSH open to `0.0.0.0/0`, hardcoded database passwords in plain text, zero database backups, and no private subnets whatsoever. I've explained not just *what* is wrong but *why* it matters in production and provided corrected code for each issue.

Key insight: I'd switch the region from `us-east-1` to `ap-south-1` (Mumbai) — this is an easy win that cuts latency for your Nepal-based users from ~200ms to ~40ms.

---

### Part 2: Linux Administration (25 min)
**Files:** `part2-linux/troubleshooting.md`, `part2-linux/Dockerfile`

For the troubleshooting scenario, I use a systematic outside-in approach: network → service → system resources → logs. I included AWS SSM Session Manager as the "no-SSH fallback" — this is something I'd ensure is configured on every EC2 instance precisely so we're never locked out.

The Dockerfile uses multi-stage builds (builder + alpine production), gunicorn instead of Flask's dev server, non-root user, and a startup probe to avoid health check false-positives during initialization.

---

### Part 3: Python Scripting (30 min)
**File:** `part3-python/ec2_monitor.py`

Full boto3 EC2 monitor with:
- Paginated instance listing (handles >10 instances)
- CloudWatch metrics (avg/min/max CPU, configurable period)
- Structured JSON report with alert flagging
- Human-readable terminal summary
- Graceful error handling for every AWS API call
- `--log-level DEBUG` for troubleshooting
- Exit code 2 when alerts found (useful in CI/CD pipelines)

**Run it:**
```bash
pip install boto3
python ec2_monitor.py --region ap-south-1 --threshold 80 --output report.json
```

---

### Part 4: Bash Scripting (20 min)
**File:** `part4-bash/analyze_nginx_logs.sh`

Pure bash + standard Linux tools (awk, sed, sort, uniq — no external dependencies). Features:
- Handles gzipped rotated logs (`.gz`)
- ANSI-colored output (auto-disabled when not a TTY, e.g., in cron)
- Graceful handling of malformed/empty log entries
- Shows: total requests, unique IPs, bandwidth, 4xx/5xx %, top 10 IPs, top 10 endpoints, status code breakdown

**Run it:**
```bash
chmod +x analyze_nginx_logs.sh
./analyze_nginx_logs.sh /var/log/nginx/access.log
```

---

### Part 5: Network Architecture (20 min)
**File:** `part5-network/architecture.md`

Designed a redundant DNS architecture that eliminates the SPOF with minimal cost increase:
- **Before:** 1 x t3.medium EC2 (~$30/month) — everything dies when it dies
- **After:** Route 53 + 2 x t3.small Unbound in separate AZs (~$32/month) — automatic failover in <90 seconds

Key decision: Route 53 health checks on TCP/53 every 30 seconds with `failure_threshold=2` means failure detection in ~60 seconds, and with TTL=60 on DNS records, clients get the secondary IP within ~90 seconds total.

Nepal-specific: Recommended ap-south-1 (Mumbai) as the closest AWS region — this matters for both latency and data sovereignty considerations.

---

### Part 6: CI/CD Pipeline (15 min)
**File:** `part6-cicd/improvements.md`

Rewrote the pipeline to include:
- **Security scanning** on every run: TruffleHog (secrets), Safety (Python CVEs), Trivy (Docker image)
- **PR-level testing** so engineers get feedback before merging
- **Environment promotion:** develop → staging (auto) → main → production (approval required)
- **Manual approval gate** using GitHub Environments
- **Automatic rollback** — saves current task definition ARN before deploy, rolls back within 60 seconds if health check fails
- **Slack notifications** so the team knows what deployed and when

---

### Bonus: Kubernetes (k8s/deployment.yaml)

Included Deployment, Service, HPA, and PodDisruptionBudget. Highlights:
- Pod anti-affinity to spread replicas across nodes
- Separate liveness, readiness, and startup probes
- `readOnlyRootFilesystem: true` and all capabilities dropped
- HPA scales between 2-10 replicas based on CPU (70%) and memory (80%)
- PDB ensures at least 1 pod stays up during cluster maintenance

---

## Technology & Versions Used

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.11 | Scripting, Flask |
| boto3 | latest | AWS SDK |
| Docker | multi-stage | Container builds |
| Terraform | 1.6+ | IaC |
| GitHub Actions | v4 actions | CI/CD |
| Unbound | 1.17+ | DNS resolver |
| AWS Route 53 | — | Public/private DNS |

---

## Assumptions Made

1. **AWS region:** I defaulted to `ap-south-1` (Mumbai) for new resources due to proximity to Nepal. The original code uses `us-east-1` — I called this out as a change I'd recommend.

2. **Team access patterns:** With 11 engineers, I assumed some level of AWS CLI / IAM access is already set up. The SSM Session Manager troubleshooting steps assume SSM agent is or will be installed on EC2 instances.

3. **GitHub Actions:** Assumed GitHub is the primary SCM (matches the assignment). Production environment approval reviewers would be configured in GitHub repository settings.

4. **Proxmox/on-premises:** Parts 5 and 6 focus on the AWS side. VPN connectivity between on-premises (pfSense) and AWS VPC is assumed to already exist for the hybrid DNS architecture to work.

5. **Database:** The monitor script targets EC2 CPU — RDS CloudWatch metrics would use a different namespace (`AWS/RDS`) and could be added as a follow-up.

---

## If I Had More Time

- **Ansible playbooks** for DNS server configuration (mentioned in architecture.md)
- **Terraform modules** for the improved VPC design with private subnets
- **Unit tests** for `ec2_monitor.py` using `moto` (AWS mock library)
- **Alertmanager integration** for the EC2 monitor (currently just JSON + exit code)
- **Runbook documentation** for the on-call team (how to respond to DNS failover alerts, etc.)

---

*Thank you for the opportunity. I've genuinely enjoyed thinking through these problems — they reflect real challenges I've encountered and solved in production environments.*
