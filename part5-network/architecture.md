# Part 5: Redundant DNS Architecture for TechKraft

## Problem Statement

TechKraft's current DNS is a single EC2 instance running Unbound. This means:
- **One server crash = DNS outage = everything is down** (websites, APIs, internal services)
- No failover, no health checks, no monitoring
- Manual intervention required for recovery

This is the classic SPOF (Single Point of Failure) scenario that every production system must eliminate.

---

## Proposed Architecture

### Core Decision: AWS Route 53 as the Primary DNS Layer

Route 53 is AWS's globally distributed DNS service with **100% SLA uptime guarantee**. It runs on Anycast across 13 edge locations globally, including in **Mumbai (ap-south-1)** which is the closest to Nepal (~40ms latency vs ~200ms for US-East).

---

## Architecture Diagram (ASCII)

```
                        ┌─────────────────────────────────────────┐
                        │          NEPAL USERS / CLIENTS           │
                        └─────────────────┬───────────────────────┘
                                          │ DNS Query
                                          ▼
                        ┌─────────────────────────────────────────┐
                        │        AWS ROUTE 53 (Public DNS)         │
                        │  ┌─────────────────────────────────┐    │
                        │  │  Health Check Monitor            │    │
                        │  │  - HTTP check every 30s          │    │
                        │  │  - 3 AZs for checker redundancy  │    │
                        │  └──────────────┬──────────────────┘    │
                        │                 │                        │
                        │   ┌─────────────┴──────────────┐        │
                        │   │    Failover Routing Policy  │        │
                        │   └──────┬────────────────┬─────┘        │
                        │          │                │               │
                        │     PRIMARY          SECONDARY            │
                        │     (Active)         (Standby)           │
                        └──────────┼────────────────┼──────────────┘
                                   │                │
                   ┌───────────────▼──┐    ┌────────▼──────────────┐
                   │  ap-south-1a     │    │  ap-south-1b           │
                   │  ┌─────────────┐ │    │  ┌──────────────────┐  │
                   │  │ Unbound DNS │ │    │  │   Unbound DNS    │  │
                   │  │ (Primary)   │ │    │  │   (Secondary)    │  │
                   │  │ t3.small    │ │    │  │   t3.small       │  │
                   │  │ Private AZ-A│ │    │  │   Private AZ-B   │  │
                   │  └──────┬──────┘ │    │  └────────┬─────────┘  │
                   └─────────│────────┘    └───────────│────────────┘
                             │                         │
                             └──────────┬──────────────┘
                                        │ Upstream forwarders
                             ┌──────────▼──────────────┐
                             │  Route 53 Resolver       │
                             │  (for private/AWS DNS)   │
                             │                          │
                             │  + Optional: CloudFlare  │
                             │    1.1.1.1 / 1.0.0.1    │
                             │  (external DNS fallback) │
                             └──────────────────────────┘

INTERNAL DNS (Private Hosted Zone):
┌────────────────────────────────────────────────────┐
│  Route 53 Private Hosted Zone: techkraft.internal  │
│                                                    │
│  api.techkraft.internal    → ALB DNS name          │
│  db.techkraft.internal     → RDS endpoint          │
│  radius.techkraft.internal → FreeRADIUS server     │
│  proxmox1.techkraft.internal → 10.x.x.x (on-prem) │
└────────────────────────────────────────────────────┘
```

---

## Key Components

### 1. AWS Route 53 (Public Zone — techkraft.com)
**Purpose:** The authoritative public DNS for all customer-facing domains.

- **Failover routing policy** between primary and secondary Unbound servers
- **Health checks** on both servers (see config below)
- **TTL = 60 seconds** for failover records (low TTL = fast failover)
- **Latency-based routing** can optionally steer Nepal users to Mumbai

### 2. Route 53 Private Hosted Zone (techkraft.internal)
**Purpose:** Internal service discovery within the VPC and on-premises (via VPN).

- `db.techkraft.internal` → RDS endpoint alias
- `api.techkraft.internal` → ALB DNS alias
- `*.techkraft.internal` → internal services
- Associated with your VPC — not resolvable from the public internet

### 3. Primary Unbound DNS (ap-south-1a)
**Purpose:** Recursive resolver for cases where custom DNS logic is needed (split-horizon, private zones for on-prem, FreeRADIUS queries).

```
EC2: t3.small, private subnet, ap-south-1a
OS: Amazon Linux 2023
Software: Unbound 1.17+
Role: PRIMARY resolver
Upstream: Route 53 Resolver (169.254.169.253)
```

### 4. Secondary Unbound DNS (ap-south-1b)
**Purpose:** Hot standby in a different Availability Zone. Mirrors primary config.

```
EC2: t3.small, private subnet, ap-south-1b
OS: Amazon Linux 2023
Software: Unbound 1.17+
Role: SECONDARY resolver (same config as primary)
Upstream: Route 53 Resolver (169.254.169.253)
```

### 5. Route 53 Resolver (169.254.169.253)
**Purpose:** AWS's built-in resolver. Used as upstream for the Unbound servers so they can resolve `*.amazonaws.com` and Route 53 private hosted zone records.

---

## Health Check Configuration

```hcl
# Terraform — Route 53 Health Checks

resource "aws_route53_health_check" "dns_primary" {
  ip_address         = aws_instance.dns_primary.private_ip
  port               = 53
  type               = "TCP"
  request_interval   = 30   # Check every 30 seconds
  failure_threshold  = 2    # 2 consecutive failures = unhealthy
  
  tags = {
    Name = "dns-primary-health-check"
  }
}

resource "aws_route53_health_check" "dns_secondary" {
  ip_address         = aws_instance.dns_secondary.private_ip
  port               = 53
  type               = "TCP"
  request_interval   = 30
  failure_threshold  = 2
  
  tags = {
    Name = "dns-secondary-health-check"
  }
}

# Failover routing records
resource "aws_route53_record" "dns_primary_failover" {
  zone_id = aws_route53_zone.internal.zone_id
  name    = "resolver.techkraft.internal"
  type    = "A"
  
  failover_routing_policy {
    type = "PRIMARY"
  }
  
  set_identifier  = "primary"
  health_check_id = aws_route53_health_check.dns_primary.id
  ttl             = 60
  records         = [aws_instance.dns_primary.private_ip]
}

resource "aws_route53_record" "dns_secondary_failover" {
  zone_id = aws_route53_zone.internal.zone_id
  name    = "resolver.techkraft.internal"
  type    = "A"
  
  failover_routing_policy {
    type = "SECONDARY"
  }
  
  set_identifier = "secondary"
  health_check_id = aws_route53_health_check.dns_secondary.id
  ttl             = 60
  records         = [aws_instance.dns_secondary.private_ip]
}
```

---

## Failover Logic

```
NORMAL OPERATION:
  Client → Route 53 → Returns primary DNS IP (AZ-A server)
  Health check on primary: ✅ PASSING every 30s

FAILURE SCENARIO:
  Primary DNS crashes at 14:23:15 NPT
  Route 53 health checker: ❌ FAILS at 14:23:30 (first check)
  Route 53 health checker: ❌ FAILS at 14:24:00 (second check)
  threshold=2 → Primary marked UNHEALTHY at 14:24:00
  Route 53 automatically returns secondary IP for all queries
  Total DNS failover time: ~60-90 seconds

WHY THIS IS FAST:
  - Route 53 checks from 3 global locations simultaneously
  - Low TTL (60s) means clients get new IP quickly
  - Secondary is already running (no cold start)
```

---

## Latency Considerations for Nepal

Nepal (Kathmandu, UTC+5:45) connects to AWS regions:

| Region | City | Approx Latency |
|--------|------|---------------|
| ap-south-1 | Mumbai, India | ~35-45ms |
| ap-southeast-1 | Singapore | ~80-100ms |
| us-east-1 | Virginia | ~200-250ms |

**Decision: Use ap-south-1 (Mumbai) as primary region.**

Route 53 name servers respond from AWS edge locations. The Mumbai edge location serves South Asian queries with minimal latency.

For public DNS (`techkraft.com`), optionally add **latency-based routing**:
```hcl
resource "aws_route53_record" "api" {
  zone_id = aws_route53_zone.public.zone_id
  name    = "api.techkraft.com"
  type    = "A"
  
  latency_routing_policy {
    region = "ap-south-1"  # Route Nepal users to Mumbai
  }
  
  set_identifier = "mumbai"
  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
```

---

## Cost Estimate (Monthly, USD)

| Component | Details | Est. Cost/Month |
|-----------|---------|-----------------|
| Route 53 Public Hosted Zone | 1 zone | $0.50 |
| Route 53 Private Hosted Zone | 1 zone | $0.50 |
| Route 53 Health Checks | 2x health checks | $1.00 |
| Route 53 DNS Queries | ~1M queries/month | $0.40 |
| EC2 DNS Primary (t3.small) | 1x in ap-south-1 | $15.00 |
| EC2 DNS Secondary (t3.small) | 1x in ap-south-1 | $15.00 |
| **Total** | | **~$32.40/month** |

**Comparison to current setup:**
- Current: 1x t3.medium EC2 ≈ $30/month — single point of failure
- Proposed: ~$32/month — fully redundant, automated failover, better latency

This is essentially the same cost for a dramatically better architecture.

---

## Implementation Timeline

| Week | Tasks |
|------|-------|
| Week 1 | Create Route 53 public + private hosted zones. Migrate DNS records from current Unbound server. Verify resolution. |
| Week 1 | Provision secondary Unbound EC2 in AZ-B (mirror primary config with Ansible/user-data). |
| Week 2 | Configure Route 53 health checks. Set up failover routing policy. Set TTL to 60s. |
| Week 2 | Test failover by stopping primary DNS server — verify traffic routes to secondary in <2 minutes. |
| Week 3 | Configure CloudWatch alarms on health check failures → SNS → email/Slack alert to ops team. |
| Week 3 | Update DHCP option sets in VPC to point to new DNS IPs. Update on-premises pfSense to use new resolvers. |
| Week 4 | Decommission old single Unbound server. Final documentation. |

**Total: 4 weeks, estimated 2-3 days actual engineering effort.**

---

## Ansible Playbook Snippet (DNS Server Setup)

```yaml
# roles/unbound_dns/tasks/main.yml
- name: Install Unbound
  package:
    name: unbound
    state: present

- name: Deploy Unbound config
  template:
    src: unbound.conf.j2
    dest: /etc/unbound/unbound.conf
    owner: root
    group: root
    mode: '0644'
  notify: restart unbound

- name: Enable and start Unbound
  systemd:
    name: unbound
    enabled: yes
    state: started
```

```ini
# unbound.conf.j2
server:
    interface: 0.0.0.0
    port: 53
    access-control: 10.0.0.0/8 allow
    access-control: 127.0.0.1/32 allow
    
    # Forward to Route 53 resolver for AWS DNS
    forward-zone:
        name: "."
        forward-addr: 169.254.169.253  # Route 53 Resolver
        forward-addr: 1.1.1.1          # Cloudflare fallback
        forward-addr: 8.8.8.8          # Google fallback
```
