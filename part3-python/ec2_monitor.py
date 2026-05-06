#!/usr/bin/env python3
"""
ec2_monitor.py — TechKraft EC2 CPU Monitoring Tool

Queries all running EC2 instances, fetches CloudWatch CPU metrics,
and generates a JSON report flagging high-utilization instances.

Usage:
    python ec2_monitor.py --region us-east-1 --threshold 80 --output report.json
    python ec2_monitor.py --region us-east-1 --config config.json
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logger = logging.getLogger("ec2_monitor")


def configure_logging(level: str = "INFO") -> None:
    """Set up structured logging with timestamp and level."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
    )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def load_config(config_path: Optional[str]) -> dict:
    """Load optional config.json; return defaults if not provided."""
    defaults = {
        "alert_threshold": 80,
        "notification_email": "ops@techkraft.com",
        "regions": ["us-east-1"],
    }
    if not config_path:
        return defaults

    path = Path(config_path)
    if not path.exists():
        logger.warning("Config file '%s' not found, using defaults.", config_path)
        return defaults

    try:
        with path.open() as f:
            config = json.load(f)
        logger.info("Loaded config from %s", config_path)
        return {**defaults, **config}  # CLI args will override later
    except json.JSONDecodeError as e:
        logger.error("Failed to parse config file: %s", e)
        sys.exit(1)


# ---------------------------------------------------------------------------
# AWS helpers
# ---------------------------------------------------------------------------
def get_ec2_instances(ec2_client) -> list[dict]:
    """
    Return a list of all running EC2 instances with metadata.
    Handles pagination automatically.
    """
    instances = []
    paginator = ec2_client.get_paginator("describe_instances")

    try:
        pages = paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        )
        for page in pages:
            for reservation in page["Reservations"]:
                for inst in reservation["Instances"]:
                    # Extract the 'Name' tag if it exists
                    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                    instances.append(
                        {
                            "instance_id": inst["InstanceId"],
                            "name": tags.get("Name", "unnamed"),
                            "instance_type": inst["InstanceType"],
                            "private_ip": inst.get("PrivateIpAddress", "N/A"),
                            "public_ip": inst.get("PublicIpAddress", "N/A"),
                            "launch_time": inst["LaunchTime"].isoformat(),
                            "tags": tags,
                        }
                    )
    except ClientError as e:
        logger.error(
            "AWS API error fetching instances: %s — %s",
            e.response["Error"]["Code"],
            e.response["Error"]["Message"],
        )
        raise
    except (BotoCoreError, NoCredentialsError) as e:
        logger.error("AWS credential/config error: %s", e)
        raise

    logger.info("Found %d running EC2 instances.", len(instances))
    return instances


def get_cpu_metrics(
    cw_client,
    instance_id: str,
    period_minutes: int = 60,
    granularity_minutes: int = 5,
) -> dict:
    """
    Fetch CPUUtilization from CloudWatch for the past `period_minutes`.
    Returns dict with avg, min, max, datapoints, and raw samples.
    """
    end_time = datetime.now(tz=timezone.utc)
    start_time = end_time - timedelta(minutes=period_minutes)
    period_seconds = granularity_minutes * 60

    try:
        response = cw_client.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=start_time,
            EndTime=end_time,
            Period=period_seconds,
            Statistics=["Average", "Minimum", "Maximum"],
            Unit="Percent",
        )
    except ClientError as e:
        logger.warning(
            "CloudWatch error for %s: %s", instance_id, e.response["Error"]["Message"]
        )
        return {"avg_cpu": None, "min_cpu": None, "max_cpu": None, "datapoints": 0}

    datapoints = response.get("Datapoints", [])
    if not datapoints:
        logger.debug("No CloudWatch datapoints for %s in the last %d minutes.", instance_id, period_minutes)
        return {"avg_cpu": None, "min_cpu": None, "max_cpu": None, "datapoints": 0}

    # Sort by timestamp for correct ordering
    datapoints.sort(key=lambda d: d["Timestamp"])
    averages = [d["Average"] for d in datapoints]
    minimums = [d["Minimum"] for d in datapoints]
    maximums = [d["Maximum"] for d in datapoints]

    return {
        "avg_cpu": round(sum(averages) / len(averages), 2),
        "min_cpu": round(min(minimums), 2),
        "max_cpu": round(max(maximums), 2),
        "datapoints": len(datapoints),
        "period_minutes": period_minutes,
        "granularity_minutes": granularity_minutes,
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def build_report(
    instances: list[dict],
    metrics_map: dict[str, dict],
    threshold: float,
    region: str,
) -> dict:
    """Assemble the final JSON report."""
    report_instances = []
    alerts = []

    for inst in instances:
        iid = inst["instance_id"]
        metrics = metrics_map.get(iid, {})
        avg_cpu = metrics.get("avg_cpu")

        is_high = avg_cpu is not None and avg_cpu > threshold

        entry = {
            "instance_id": iid,
            "name": inst["name"],
            "instance_type": inst["instance_type"],
            "private_ip": inst["private_ip"],
            "public_ip": inst["public_ip"],
            "launch_time": inst["launch_time"],
            "cpu_utilization": {
                "avg_percent": avg_cpu,
                "min_percent": metrics.get("min_cpu"),
                "max_percent": metrics.get("max_cpu"),
                "datapoints": metrics.get("datapoints", 0),
                "period_minutes": metrics.get("period_minutes", 60),
            },
            "alert": is_high,
            "alert_reason": f"Avg CPU {avg_cpu:.1f}% exceeds threshold {threshold}%" if is_high else None,
        }
        report_instances.append(entry)

        if is_high:
            alerts.append(
                {
                    "instance_id": iid,
                    "name": inst["name"],
                    "avg_cpu": avg_cpu,
                    "threshold": threshold,
                }
            )

    return {
        "report_metadata": {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "region": region,
            "alert_threshold_percent": threshold,
            "total_instances": len(instances),
            "alert_count": len(alerts),
        },
        "alerts": alerts,
        "instances": report_instances,
    }


def save_report(report: dict, output_path: str) -> None:
    """Write report to JSON file."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info("Report saved to %s", output_path)


def print_summary(report: dict) -> None:
    """Print a human-readable summary to stdout."""
    meta = report["report_metadata"]
    print("\n" + "=" * 60)
    print("  TechKraft EC2 CPU Monitor Report")
    print("=" * 60)
    print(f"  Generated : {meta['generated_at']}")
    print(f"  Region    : {meta['region']}")
    print(f"  Threshold : {meta['alert_threshold_percent']}%")
    print(f"  Instances : {meta['total_instances']}")
    print(f"  Alerts    : {meta['alert_count']}")
    print("=" * 60)

    if report["alerts"]:
        print("\n⚠️  HIGH CPU ALERTS:")
        for alert in report["alerts"]:
            print(f"  • {alert['name']} ({alert['instance_id']}): {alert['avg_cpu']:.1f}% avg CPU")

    print("\nInstance Summary:")
    print(f"  {'Name':<25} {'ID':<20} {'Type':<14} {'Avg CPU':>8}  {'Alert'}")
    print(f"  {'-'*25} {'-'*20} {'-'*14} {'-'*8}  {'-'*5}")
    for inst in report["instances"]:
        avg = inst["cpu_utilization"]["avg_percent"]
        avg_str = f"{avg:.1f}%" if avg is not None else "N/A"
        alert_str = "🔴 YES" if inst["alert"] else "✅ OK"
        print(
            f"  {inst['name']:<25} {inst['instance_id']:<20} "
            f"{inst['instance_type']:<14} {avg_str:>8}  {alert_str}"
        )
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor EC2 CPU utilization and generate a JSON report.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region to query",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=80.0,
        help="CPU%% alert threshold (flag instances above this value)",
    )
    parser.add_argument(
        "--output",
        default="ec2_cpu_report.json",
        help="Output file path for the JSON report",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.json (overrides defaults, CLI args take priority)",
    )
    parser.add_argument(
        "--period",
        type=int,
        default=60,
        help="Lookback period in minutes for CloudWatch metrics",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(args.log_level)

    # Load config, CLI args override config file values
    config = load_config(args.config)
    threshold = args.threshold if args.threshold != 80.0 else config.get("alert_threshold", 80.0)
    region = args.region if args.region != "us-east-1" else config.get("regions", ["us-east-1"])[0]

    logger.info("Starting EC2 monitor — region=%s, threshold=%.1f%%", region, threshold)

    try:
        session = boto3.Session(region_name=region)
        ec2_client = session.client("ec2")
        cw_client = session.client("cloudwatch")
    except NoCredentialsError:
        logger.error(
            "No AWS credentials found. Configure via:\n"
            "  1. AWS CLI: aws configure\n"
            "  2. Environment: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY\n"
            "  3. IAM Instance Profile (recommended for EC2)"
        )
        sys.exit(1)

    # Fetch instances
    try:
        instances = get_ec2_instances(ec2_client)
    except (ClientError, BotoCoreError):
        logger.error("Failed to retrieve EC2 instances. Exiting.")
        sys.exit(1)

    if not instances:
        logger.warning("No running instances found in region %s.", region)
        sys.exit(0)

    # Fetch CloudWatch metrics for each instance
    metrics_map: dict[str, dict] = {}
    for inst in instances:
        iid = inst["instance_id"]
        logger.debug("Fetching CloudWatch metrics for %s (%s)", inst["name"], iid)
        metrics_map[iid] = get_cpu_metrics(cw_client, iid, period_minutes=args.period)

    # Build and save report
    report = build_report(instances, metrics_map, threshold, region)
    save_report(report, args.output)
    print_summary(report)

    # Exit with non-zero code if there are alerts (useful for CI/CD pipelines)
    if report["report_metadata"]["alert_count"] > 0:
        logger.warning(
            "%d instance(s) exceed the %.1f%% CPU threshold.",
            report["report_metadata"]["alert_count"],
            threshold,
        )
        sys.exit(2)  # Exit code 2 = alerts present (not an error, just a warning signal)


if __name__ == "__main__":
    main()
