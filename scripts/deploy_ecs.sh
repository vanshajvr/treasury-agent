#!/usr/bin/env bash
# Deploy the demo server to Amazon ECS Express Mode (Fargate + managed HTTPS load balancer).
#
#   brew install awscli && aws login        # once
#   scripts/deploy_ecs.sh                   # first deploy and every update
#
# Env: AWS_REGION (default: the CLI's region), SERVICE (default treasury-demo).
# GROQ_API_KEY is read from .env into SSM Parameter Store (SecureString) and injected by ECS at start-up,
# so it never appears in the task definition. Airwallex keys are never sent: the demo cannot move money.
# Cost: one 0.25 vCPU / 0.5 GB task (min = max = 1) plus the load balancer Express Mode manages.
# Tear down: aws ecs delete-express-gateway-service --service-arn <arn>
set -euo pipefail
cd "$(dirname "$0")/.."

SERVICE="${SERVICE:-treasury-demo}"
CLUSTER=default
REGION="${AWS_REGION:-$(aws configure get region || true)}"
[[ -n "$REGION" ]] || { echo "Set AWS_REGION or run 'aws configure'." >&2; exit 1; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# `aws login` sessions sometimes fail their first token refresh (CreateOAuth2Token); the retry succeeds.
aws() {
  local n rc
  for n in 1 2 3; do
    rc=0; command aws --region "$REGION" "$@" 2>"$TMP/aws.err" || rc=$?
    [[ $rc == 0 ]] && return 0
    grep -q CreateOAuth2Token "$TMP/aws.err" || break
    sleep 2
  done
  cat "$TMP/aws.err" >&2; return $rc
}
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
REPO="$REGISTRY/$SERVICE"
PARAM="/$SERVICE/GROQ_API_KEY"
EXEC_ROLE="$SERVICE-execution"
INFRA_ROLE="$SERVICE-infrastructure"

envval() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//' || true; }

echo "1/6 IAM roles"
# Service-linked roles exist once per account; a new account that never used ECS from the console has neither.
for svc in ecs.amazonaws.com elasticloadbalancing.amazonaws.com; do
  aws iam create-service-linked-role --aws-service-name "$svc" >/dev/null 2>&1 || true
done
ensure_role() {  # name, service principal, managed policy ARN
  if ! aws iam get-role --role-name "$1" >/dev/null 2>&1; then
    aws iam create-role --role-name "$1" --assume-role-policy-document \
      "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"$2\"},\"Action\":\"sts:AssumeRole\"}]}" >/dev/null
    CREATED_ROLE=1
  fi
  aws iam attach-role-policy --role-name "$1" --policy-arn "$3"
}
CREATED_ROLE=0
ensure_role "$EXEC_ROLE" ecs-tasks.amazonaws.com arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
ensure_role "$INFRA_ROLE" ecs.amazonaws.com arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices
aws iam put-role-policy --role-name "$EXEC_ROLE" --policy-name read-groq-key --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"ssm:GetParameters\",\"Resource\":\"arn:aws:ssm:$REGION:$ACCOUNT:parameter$PARAM\"}]}"
[[ $CREATED_ROLE == 1 ]] && { echo "   waiting for new roles to propagate..."; sleep 15; }

echo "2/6 secret"
SECRETS="[]"
GROQ_API_KEY="$(envval GROQ_API_KEY)"
if [[ -n "$GROQ_API_KEY" ]]; then
  ( umask 077; GROQ_API_KEY="$GROQ_API_KEY" PARAM="$PARAM" python3 -c \
    'import json,os;print(json.dumps({"Name":os.environ["PARAM"],"Value":os.environ["GROQ_API_KEY"],"Type":"SecureString","Overwrite":True}))' \
    > "$TMP/param.json" )
  aws ssm put-parameter --cli-input-json "file://$TMP/param.json" >/dev/null
  SECRETS="[{\"name\":\"GROQ_API_KEY\",\"valueFrom\":\"arn:aws:ssm:$REGION:$ACCOUNT:parameter$PARAM\"}]"
else
  echo "   no GROQ_API_KEY in .env: the live demo will use the keyword rule." >&2
fi

echo "3/6 image"
aws ecr describe-repositories --repository-names "$SERVICE" >/dev/null 2>&1 || {
  aws ecr create-repository --repository-name "$SERVICE" --image-scanning-configuration scanOnPush=true >/dev/null
  aws ecr put-lifecycle-policy --repository-name "$SERVICE" --lifecycle-policy-text \
    '{"rules":[{"rulePriority":1,"description":"keep last 5","selection":{"tagStatus":"any","countType":"imageCountMoreThan","countNumber":5},"action":{"type":"expire"}}]}' >/dev/null
}
TAG="$(date -u +%Y%m%d-%H%M%S)"
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
docker build --platform linux/amd64 -q -t "$REPO:$TAG" . >/dev/null
for n in 1 2 3; do                                  # large uploads can drop; pushed layers are skipped on retry
  docker push -q "$REPO:$TAG" >/dev/null && break
  [[ $n == 3 ]] && { echo "Image push failed 3 times." >&2; exit 1; }
  echo "   push dropped, retrying ($n/3)..."
done
echo "   pushed $SERVICE:$TAG"

echo "4/6 service"
aws ecs describe-clusters --clusters "$CLUSTER" --query 'clusters[?status==`ACTIVE`].clusterName' --output text | grep -q . \
  || aws ecs create-cluster --cluster-name "$CLUSTER" >/dev/null
LLM_PER_DAY="$(envval LLM_PER_DAY)"; PER_IP_PER_MIN="$(envval PER_IP_PER_MIN)"
cat > "$TMP/container.json" <<EOF
{"image": "$REPO:$TAG", "containerPort": 8000,
 "environment": [{"name": "HOST", "value": "0.0.0.0"}, {"name": "PORT", "value": "8000"},
                 {"name": "TRUST_PROXY", "value": "1"},
                 {"name": "LLM_PER_DAY", "value": "${LLM_PER_DAY:-300}"},
                 {"name": "PER_IP_PER_MIN", "value": "${PER_IP_PER_MIN:-6}"}],
 "secrets": $SECRETS}
EOF
COMMON=(--primary-container "file://$TMP/container.json" --health-check-path /api/health
        --execution-role-arn "arn:aws:iam::$ACCOUNT:role/$EXEC_ROLE"
        --cpu 256 --memory 512 --cpu-architecture X86_64
        --scaling-target minTaskCount=1,maxTaskCount=1)
ARN="$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
       --query 'services[?status==`ACTIVE`].serviceArn' --output text)"
if [[ -z "$ARN" || "$ARN" == "None" ]]; then
  ARN="$(aws ecs create-express-gateway-service --cluster "$CLUSTER" --service-name "$SERVICE" \
         --infrastructure-role-arn "arn:aws:iam::$ACCOUNT:role/$INFRA_ROLE" "${COMMON[@]}" \
         --query 'service.serviceArn' --output text)"
else
  aws ecs update-express-gateway-service --service-arn "$ARN" "${COMMON[@]}" >/dev/null
fi

echo "5/6 rollout (first deploy takes several minutes)"
for i in $(seq 1 80); do
  read -r rollout running < <(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --query \
    'services[0].[deployments[?status==`PRIMARY`]|[0].rolloutState, runningCount]' --output text)
  [[ "$rollout" == "COMPLETED" ]] && break
  [[ "$rollout" == "FAILED" ]] && { echo "Rollout failed. Recent events:" >&2
    aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --query 'services[0].events[:5].message' --output text >&2; exit 1; }
  echo "   $rollout, $running running..."; sleep 15
done
[[ "$rollout" == "COMPLETED" ]] || { echo "Timed out waiting for the rollout." >&2; exit 1; }

echo "6/6 endpoint"
HOST_="$(aws ecs describe-express-gateway-service --service-arn "$ARN" \
         --query 'service.activeConfigurations[0].ingressPaths[0].endpoint' --output text)"
URL="https://${HOST_#https://}"
for i in $(seq 1 40); do curl -sf "$URL/api/health" >/dev/null && break; sleep 10; done
echo "live: $URL"
curl -sf "$URL/api/health" && echo
