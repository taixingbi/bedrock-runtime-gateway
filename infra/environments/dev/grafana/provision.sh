#!/usr/bin/env bash
# Provisions the CloudWatch datasource + dashboard.json into the
# Amazon Managed Grafana workspace grafana.tf creates.
#
# Why this exists instead of Terraform: the AWS provider has no
# aws_grafana_dashboard/aws_grafana_data_source resource -- dashboards
# and datasources are Grafana-native objects, only reachable through
# Grafana's own HTTP API, not an AWS API grafana.tf's provider could
# call. This script is that missing piece, made idempotent/scripted
# rather than left as tribal-knowledge manual clicking in the UI.
#
# Also fixes a real gap grafana.tf's own comment used to get wrong:
# permission_type=SERVICE_MANAGED does NOT auto-attach the CloudWatch
# IAM policy to the workspace's role the way its name suggests --
# live-verified empty AttachedPolicies after apply. grafana.tf now
# attaches AmazonGrafanaCloudWatchAccess explicitly; this script
# assumes that's already applied (`terraform apply` in this directory)
# before it runs.
#
# Uses a short-lived (1h) Grafana service-account token via `aws
# grafana create-workspace-service-account[-token]` for API auth --
# not an SSO session, since this is a script, not a browser -- and
# deletes the service account again on exit (trap), success or
# failure, so no long-lived credential is left behind.
#
# Usage:
#   ./provision.sh
#
# Requires: aws cli (grafana + identitystore permissions), curl, python3.
set -euo pipefail

WORKSPACE_ID="g-09aaa65e9c"
REGION="us-east-1"
DASHBOARD_JSON="$(dirname "$0")/dashboard.json"

WORKSPACE_URL="https://${WORKSPACE_ID}.grafana-workspace.${REGION}.amazonaws.com"

SERVICE_ACCOUNT_ID=""
TOKEN_ID=""
cleanup() {
  if [ -n "$TOKEN_ID" ]; then
    aws grafana delete-workspace-service-account-token \
      --workspace-id "$WORKSPACE_ID" --service-account-id "$SERVICE_ACCOUNT_ID" --token-id "$TOKEN_ID" \
      --region "$REGION" >/dev/null 2>&1 || true
  fi
  if [ -n "$SERVICE_ACCOUNT_ID" ]; then
    aws grafana delete-workspace-service-account \
      --workspace-id "$WORKSPACE_ID" --service-account-id "$SERVICE_ACCOUNT_ID" \
      --region "$REGION" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "creating temporary service account..."
SERVICE_ACCOUNT_ID=$(aws grafana create-workspace-service-account \
  --workspace-id "$WORKSPACE_ID" --name "provision-$(date +%s)" --grafana-role ADMIN \
  --region "$REGION" --query 'id' --output text)

TOKEN=$(aws grafana create-workspace-service-account-token \
  --workspace-id "$WORKSPACE_ID" --service-account-id "$SERVICE_ACCOUNT_ID" \
  --name "provision-token" --seconds-to-live 3600 \
  --region "$REGION" --query 'serviceAccountToken.key' --output text)
TOKEN_ID=$(aws grafana list-workspace-service-account-tokens \
  --workspace-id "$WORKSPACE_ID" --service-account-id "$SERVICE_ACCOUNT_ID" \
  --region "$REGION" --query 'serviceAccountTokens[0].id' --output text)

echo "ensuring CloudWatch datasource exists..."
DATASOURCE_UID=$(curl -sf -H "Authorization: Bearer $TOKEN" "$WORKSPACE_URL/api/datasources" \
  | python3 -c "import json,sys; ds=[d for d in json.load(sys.stdin) if d['type']=='cloudwatch']; print(ds[0]['uid'] if ds else '')")

if [ -z "$DATASOURCE_UID" ]; then
  echo "  none found, creating..."
  DATASOURCE_UID=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"name": "CloudWatch", "type": "cloudwatch", "access": "proxy", "jsonData": {"authType": "default", "defaultRegion": "'"$REGION"'"}}' \
    "$WORKSPACE_URL/api/datasources" | python3 -c "import json,sys; print(json.load(sys.stdin)['datasource']['uid'])")
fi
echo "  datasource uid: $DATASOURCE_UID"

echo "provisioning dashboard..."
python3 -c "
import json
with open('$DASHBOARD_JSON') as f:
    content = f.read()
content = content.replace('__CLOUDWATCH_DATASOURCE_UID__', '$DATASOURCE_UID')
print(content)
" > /tmp/grafana_dashboard_provisioned.json

RESULT=$(curl -sf -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d @/tmp/grafana_dashboard_provisioned.json \
  "$WORKSPACE_URL/api/dashboards/db")
rm -f /tmp/grafana_dashboard_provisioned.json

echo "$RESULT" | python3 -m json.tool
DASHBOARD_URL=$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin)['url'])")
echo ""
echo "dashboard live at: ${WORKSPACE_URL}${DASHBOARD_URL}"
