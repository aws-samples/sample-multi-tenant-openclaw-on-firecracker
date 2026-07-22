#!/bin/bash
# E2E AZ failover test helpers — sourced by individual test scripts
# Source `.env.deploy` first.

set -uo pipefail

E2E_PROFILE="${E2E_PROFILE:-jiasunm-neo}"
E2E_REGION="${E2E_REGION:-ap-northeast-1}"

# 1.5.9: under RBAC the shared api key is `viewer`; write calls (create/backup/
# delete) need an operator id_token. OC_E2E_ID_TOKEN authorizes them; without
# RBAC the header is ignored. Kept as an array so it expands to nothing when unset.
E2E_AUTH_HDR=()
[ -n "${OC_E2E_ID_TOKEN:-}" ] && E2E_AUTH_HDR=(-H "Authorization: Bearer ${OC_E2E_ID_TOKEN}")

# Hosts (use describe to discover, fail loudly if not found)
e2e_az_a_host() {
  aws ec2 describe-instances --profile "$E2E_PROFILE" --region "$E2E_REGION" \
    --filters "Name=tag:aws:autoscaling:groupName,Values=openclaw-hosts-asg" \
              "Name=instance-state-name,Values=running" \
              "Name=availability-zone,Values=${E2E_REGION}a" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text
}

e2e_az_c_host() {
  aws ec2 describe-instances --profile "$E2E_PROFILE" --region "$E2E_REGION" \
    --filters "Name=tag:aws:autoscaling:groupName,Values=openclaw-hosts-asg" \
              "Name=instance-state-name,Values=running" \
              "Name=availability-zone,Values=${E2E_REGION}c" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text
}

e2e_clear_cooldown() {
  aws dynamodb delete-item --profile "$E2E_PROFILE" --region "$E2E_REGION" \
    --table-name openclaw-hosts \
    --key '{"instance_id":{"S":"__az_failover_state__"}}' >/dev/null 2>&1
}

e2e_inject_stale() {
  local host_id="$1"
  local stale_ts="${2:-2026-05-24T08:00:00Z}"
  aws dynamodb update-item --profile "$E2E_PROFILE" --region "$E2E_REGION" \
    --table-name openclaw-hosts \
    --key "{\"instance_id\":{\"S\":\"${host_id}\"}}" \
    --update-expression "SET last_seen = :t, last_health_check = :t" \
    --expression-attribute-values "{\":t\":{\"S\":\"$stale_ts\"}}" >/dev/null
}

e2e_stop_host_agent() {
  local host_id="$1"
  aws ssm send-command --profile "$E2E_PROFILE" --region "$E2E_REGION" \
    --document-name AWS-RunShellScript \
    --instance-ids "$host_id" \
    --parameters 'commands=["systemctl stop host-agent"]' \
    --query 'Command.CommandId' --output text >/dev/null
  sleep 6
}

e2e_start_host_agent() {
  local host_id="$1"
  aws ssm send-command --profile "$E2E_PROFILE" --region "$E2E_REGION" \
    --document-name AWS-RunShellScript \
    --instance-ids "$host_id" \
    --parameters 'commands=["systemctl restart host-agent && sleep 3 && systemctl is-active host-agent"]' \
    --query 'Command.CommandId' --output text >/dev/null
  sleep 12
}

e2e_invoke_health_check() {
  aws lambda invoke --profile "$E2E_PROFILE" --region "$E2E_REGION" \
    --function-name openclaw-health-check \
    --payload '{}' --cli-binary-format raw-in-base64-out \
    /tmp/hc-out.json >/dev/null 2>&1
}

e2e_lambda_log_last_failover() {
  aws logs tail /aws/lambda/openclaw-health-check \
    --profile "$E2E_PROFILE" --region "$E2E_REGION" \
    --since 5m --format short 2>&1 \
    | grep "az_failover:" | tail -1
}

e2e_create_tenant() {
  local name="$1" host_id="$2" vcpu="${3:-1}" mem_mb="${4:-2048}"
  curl -s -X POST "${API_URL}tenants" \
    -H "x-api-key: $API_KEY" "${E2E_AUTH_HDR[@]}" \
    -d "{\"name\":\"${name}\",\"vcpu\":${vcpu},\"mem_mb\":${mem_mb},\"preferred_host_id\":\"${host_id}\",\"tags\":{\"purpose\":\"e2e-failover\"}}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))"
}

e2e_wait_running() {
  local tenant_id="$1"
  local max_iter="${2:-24}"  # default 2 minutes (24 × 5s)
  for _ in $(seq 1 $max_iter); do
    local status=$(curl -s -H "x-api-key: $API_KEY" "${API_URL}tenants/${tenant_id}" \
      | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null)
    if [ "$status" = "running" ]; then echo "running"; return 0; fi
    sleep 5
  done
  echo "TIMEOUT"
  return 1
}

e2e_get_tenant_field() {
  local tenant_id="$1" field="$2"
  curl -s -H "x-api-key: $API_KEY" "${API_URL}tenants/${tenant_id}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('$field',''))" 2>/dev/null
}

e2e_trigger_backup_and_wait() {
  local tenant_id="$1"
  curl -s -X POST "${API_URL}tenants/${tenant_id}/backup" \
    -H "x-api-key: $API_KEY" "${E2E_AUTH_HDR[@]}" >/dev/null
  # Wait for the .gz to appear in S3
  for i in $(seq 1 18); do
    sleep 3
    local count=$(aws s3 ls "s3://${ASSETS_BUCKET}/backups/${tenant_id}/" \
                  --profile "$E2E_PROFILE" --region "$E2E_REGION" 2>/dev/null | wc -l)
    if [ "$count" -ge 1 ]; then echo "ok"; return 0; fi
  done
  echo "TIMEOUT"
  return 1
}

e2e_dashboard_http_status() {
  local tenant_id="$1"
  local token=$(e2e_get_tenant_field "$tenant_id" gateway_token)
  if [ -z "$token" ]; then echo "0"; return; fi
  curl -sI -o /dev/null -w "%{http_code}" \
    "${DASHBOARD_URL}/vm/${tenant_id}/?token=${token}"
}

e2e_alb_rule_target_for() {
  local tenant_id="$1"
  local listener_arn=$(aws elbv2 describe-listeners \
    --profile "$E2E_PROFILE" --region "$E2E_REGION" \
    --load-balancer-arn $(aws elbv2 describe-load-balancers \
        --profile "$E2E_PROFILE" --region "$E2E_REGION" \
        --query 'LoadBalancers[0].LoadBalancerArn' --output text) \
    --query 'Listeners[0].ListenerArn' --output text)
  aws elbv2 describe-rules \
    --profile "$E2E_PROFILE" --region "$E2E_REGION" \
    --listener-arn "$listener_arn" \
    --query "Rules[?Conditions[?Field=='path-pattern'&&contains(Values, '/vm/${tenant_id}')]].Actions[0].TargetGroupArn" \
    --output text
}

e2e_delete_tenant() {
  local tenant_id="$1"
  curl -s -X DELETE "${API_URL}tenants/${tenant_id}" -H "x-api-key: $API_KEY" "${E2E_AUTH_HDR[@]}" >/dev/null
}

e2e_audit_search() {
  local pattern="$1"
  local audit_table=$(aws dynamodb list-tables \
    --profile "$E2E_PROFILE" --region "$E2E_REGION" \
    --query "TableNames[?contains(@, 'audit-log-')]" --output text)
  aws dynamodb scan --table-name "$audit_table" \
    --profile "$E2E_PROFILE" --region "$E2E_REGION" \
    --filter-expression "begins_with(operation, :p)" \
    --expression-attribute-values "{\":p\":{\"S\":\"${pattern}\"}}" \
    --query 'Items[*].{op:operation.S,res:resource_id.S,ts:ts.S,d:detail.S}' --output json
}
