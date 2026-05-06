#!/usr/bin/env bash
# =============================================================================
# analyze_nginx_logs.sh — TechKraft Nginx Access Log Analyzer
#
# Usage:
#   ./analyze_nginx_logs.sh [log_file]
#   ./analyze_nginx_logs.sh /var/log/nginx/access.log
#   ./analyze_nginx_logs.sh  (defaults to /var/log/nginx/access.log)
#
# Handles:
#   - Standard combined log format
#   - Custom log formats (attempts best-effort parsing)
#   - Missing or malformed entries
#   - Gzipped rotated logs (access.log.gz)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
readonly DEFAULT_LOG="/var/log/nginx/access.log"
readonly TOP_N=10
readonly SEPARATOR="$(printf '%.0s─' {1..65})"

# ANSI colors (disabled if not a terminal)
if [[ -t 1 ]]; then
    BOLD='\033[1m'
    CYAN='\033[0;36m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    RED='\033[0;31m'
    RESET='\033[0m'
else
    BOLD='' CYAN='' GREEN='' YELLOW='' RED='' RESET=''
fi

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
print_header() {
    echo -e "\n${BOLD}${CYAN}$1${RESET}"
    echo "$SEPARATOR"
}

print_error() {
    echo -e "${RED}ERROR: $1${RESET}" >&2
}

print_warning() {
    echo -e "${YELLOW}WARNING: $1${RESET}" >&2
}

usage() {
    echo "Usage: $0 [log_file_path]"
    echo "  Defaults to: $DEFAULT_LOG"
    echo "  Supports .gz compressed files."
    exit 0
}

# ---------------------------------------------------------------------------
# Argument handling
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
fi

LOG_FILE="${1:-$DEFAULT_LOG}"

# ---------------------------------------------------------------------------
# Validate log file
# ---------------------------------------------------------------------------
if [[ ! -f "$LOG_FILE" ]]; then
    print_error "Log file not found: $LOG_FILE"
    print_error "Usage: $0 [path_to_log_file]"
    exit 1
fi

if [[ ! -r "$LOG_FILE" ]]; then
    print_error "Cannot read log file: $LOG_FILE (permission denied)"
    print_error "Try: sudo $0 $LOG_FILE"
    exit 1
fi

# Determine how to read the file (support gzipped rotated logs)
if [[ "$LOG_FILE" == *.gz ]]; then
    if ! command -v zcat &>/dev/null; then
        print_error "zcat not found. Cannot read gzipped file."
        exit 1
    fi
    CAT_CMD="zcat"
else
    CAT_CMD="cat"
fi

# ---------------------------------------------------------------------------
# Check for empty file
# ---------------------------------------------------------------------------
LINE_COUNT=$($CAT_CMD "$LOG_FILE" | wc -l)
if [[ "$LINE_COUNT" -eq 0 ]]; then
    print_warning "Log file is empty: $LOG_FILE"
    exit 0
fi

# ---------------------------------------------------------------------------
# Parse the log into a temp file for efficient multiple passes
# Nginx combined log format:
#   $remote_addr - $remote_user [$time_local] "$request" $status $bytes
# ---------------------------------------------------------------------------
TMPFILE=$(mktemp /tmp/nginx_analysis.XXXXXX)
trap 'rm -f "$TMPFILE"' EXIT

# Extract well-formed lines only (skip malformed entries)
# A valid line starts with an IP address
$CAT_CMD "$LOG_FILE" | grep -E '^[0-9a-fA-F.:]+' > "$TMPFILE" || true

VALID_LINES=$(wc -l < "$TMPFILE")
MALFORMED=$((LINE_COUNT - VALID_LINES))

if [[ "$VALID_LINES" -eq 0 ]]; then
    print_error "No valid log entries found in $LOG_FILE"
    print_error "Ensure the file uses standard Nginx combined log format."
    exit 1
fi

# ---------------------------------------------------------------------------
# Overall statistics
# ---------------------------------------------------------------------------
TOTAL_REQUESTS=$VALID_LINES
UNIQUE_IPS=$(awk '{print $1}' "$TMPFILE" | sort -u | wc -l)

# Status code counts
# Field 9 in combined format is the HTTP status code
ERRORS_4XX=$(awk '$9 ~ /^4[0-9]{2}$/' "$TMPFILE" | wc -l)
ERRORS_5XX=$(awk '$9 ~ /^5[0-9]{2}$/' "$TMPFILE" | wc -l)

# Calculate percentages (awk for floating point)
PCT_4XX=$(awk -v total="$TOTAL_REQUESTS" -v errors="$ERRORS_4XX" \
    'BEGIN { if (total > 0) printf "%.2f", (errors / total) * 100; else print "0.00" }')
PCT_5XX=$(awk -v total="$TOTAL_REQUESTS" -v errors="$ERRORS_5XX" \
    'BEGIN { if (total > 0) printf "%.2f", (errors / total) * 100; else print "0.00" }')

# ---------------------------------------------------------------------------
# Top 10 IPs by request count
# ---------------------------------------------------------------------------
TOP_IPS=$(awk '{print $1}' "$TMPFILE" \
    | sort \
    | uniq -c \
    | sort -rn \
    | head -"$TOP_N")

# ---------------------------------------------------------------------------
# Top 10 endpoints (field 7 is the URI path, strip query strings)
# ---------------------------------------------------------------------------
TOP_ENDPOINTS=$(awk '{
    # Field 7 is the URI: GET /path?query HTTP/1.1
    # $7 contains the path portion after splitting by space in $6,$7,$8
    # In combined format: $6=method, $7=path, $8=protocol (wrapped in quotes)
    # Actually $6 = "GET, $7 = /path, $8 = HTTP/1.1"
    # Strip leading quote from $7 if present
    gsub(/"/, "", $7)
    # Strip query string
    sub(/\?.*/, "", $7)
    # Only count if it looks like a path
    if ($7 ~ /^\//) print $7
}' "$TMPFILE" \
    | sort \
    | uniq -c \
    | sort -rn \
    | head -"$TOP_N")

# ---------------------------------------------------------------------------
# HTTP status code breakdown
# ---------------------------------------------------------------------------
STATUS_BREAKDOWN=$(awk '{print $9}' "$TMPFILE" \
    | grep -E '^[0-9]{3}$' \
    | sort \
    | uniq -c \
    | sort -rn \
    | head -15)

# ---------------------------------------------------------------------------
# Bandwidth analysis (field 10 = bytes sent)
# ---------------------------------------------------------------------------
TOTAL_BYTES=$(awk '{
    if ($10 ~ /^[0-9]+$/) sum += $10
} END { print sum+0 }' "$TMPFILE")

# Human-readable bytes
format_bytes() {
    local bytes=$1
    if   [[ $bytes -ge 1073741824 ]]; then awk -v b="$bytes" 'BEGIN{printf "%.2f GB", b/1073741824}'
    elif [[ $bytes -ge 1048576    ]]; then awk -v b="$bytes" 'BEGIN{printf "%.2f MB", b/1048576}'
    elif [[ $bytes -ge 1024       ]]; then awk -v b="$bytes" 'BEGIN{printf "%.2f KB", b/1024}'
    else echo "${bytes} B"
    fi
}

TOTAL_BANDWIDTH=$(format_bytes "$TOTAL_BYTES")

# ---------------------------------------------------------------------------
# Time range of logs
# ---------------------------------------------------------------------------
FIRST_ENTRY=$(awk 'NR==1{print $4}' "$TMPFILE" | tr -d '[')
LAST_ENTRY=$(awk 'END{print $4}' "$TMPFILE" | tr -d '[')

# ---------------------------------------------------------------------------
# Print Report
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║          TechKraft — Nginx Log Analysis Report               ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  Log File     : ${CYAN}$LOG_FILE${RESET}"
echo -e "  Log Period   : $FIRST_ENTRY  →  $LAST_ENTRY"
[[ "$MALFORMED" -gt 0 ]] && echo -e "  ${YELLOW}Skipped      : $MALFORMED malformed/unparseable entries${RESET}"

print_header "📊 SUMMARY"
printf "  %-25s %s\n" "Total Requests:"   "$TOTAL_REQUESTS"
printf "  %-25s %s\n" "Unique IPs:"       "$UNIQUE_IPS"
printf "  %-25s %s\n" "Total Bandwidth:"  "$TOTAL_BANDWIDTH"
printf "  %-25s %s (%s%%)\n" "4xx Client Errors:" "$ERRORS_4XX" "$PCT_4XX"
printf "  %-25s %s (%s%%)\n" "5xx Server Errors:" "$ERRORS_5XX" "$PCT_5XX"

print_header "🔥 TOP ${TOP_N} IPs BY REQUEST COUNT"
printf "  %-6s  %-18s  %s\n" "Rank" "IP Address" "Requests"
printf "  %-6s  %-18s  %s\n" "----" "----------" "--------"
RANK=1
while IFS= read -r line; do
    COUNT=$(echo "$line" | awk '{print $1}')
    IP=$(echo "$line" | awk '{print $2}')
    printf "  %-6s  %-18s  %s\n" "$RANK." "$IP" "$COUNT"
    ((RANK++))
done <<< "$TOP_IPS"

print_header "📍 TOP ${TOP_N} ENDPOINTS BY REQUEST COUNT"
printf "  %-6s  %-10s  %s\n" "Rank" "Requests" "Endpoint"
printf "  %-6s  %-10s  %s\n" "----" "--------" "--------"
RANK=1
while IFS= read -r line; do
    COUNT=$(echo "$line" | awk '{print $1}')
    ENDPOINT=$(echo "$line" | awk '{print $2}')
    printf "  %-6s  %-10s  %s\n" "$RANK." "$COUNT" "$ENDPOINT"
    ((RANK++))
done <<< "$TOP_ENDPOINTS"

print_header "📈 HTTP STATUS CODE BREAKDOWN"
printf "  %-12s  %-10s  %s\n" "Status" "Count" "Percentage"
printf "  %-12s  %-10s  %s\n" "------" "-----" "----------"
while IFS= read -r line; do
    COUNT=$(echo "$line" | awk '{print $1}')
    STATUS=$(echo "$line" | awk '{print $2}')
    PCT=$(awk -v total="$TOTAL_REQUESTS" -v c="$COUNT" \
        'BEGIN{printf "%.2f%%", (c/total)*100}')
    # Color-code by status class
    if   [[ "$STATUS" =~ ^5 ]]; then COLOR="$RED"
    elif [[ "$STATUS" =~ ^4 ]]; then COLOR="$YELLOW"
    elif [[ "$STATUS" =~ ^2 ]]; then COLOR="$GREEN"
    else                              COLOR="$RESET"
    fi
    printf "  ${COLOR}%-12s  %-10s  %s${RESET}\n" "$STATUS" "$COUNT" "$PCT"
done <<< "$STATUS_BREAKDOWN"

echo ""
echo -e "${BOLD}$SEPARATOR${RESET}"
echo -e "  Report generated: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo ""
