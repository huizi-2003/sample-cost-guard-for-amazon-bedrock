#!/bin/bash
set -euo pipefail

# ============================================================================
# Bedrock Lite Guard deploy script.
# Packages the code, uploads it to an S3 bucket (auto-created) and deploys the
# CloudFormation stack (Lambda + DynamoDB + EventBridge + EC2 web console).
#
# Usage:
#   ./deploy.sh
# Optional env overrides:
#   AWS_REGION, S3_BUCKET, STACK_NAME, ALLOWED_CIDR, KEY_NAME
# ============================================================================

cd "$(dirname "$0")"

# ----- config (override via env) -----
STACK_NAME="${STACK_NAME:-bedrock-cost-guard}"
REGION="${AWS_REGION:-us-east-1}"
S3_KEY="bedrock-cost-guard/lambda.zip"
KEY_NAME="${KEY_NAME:-}"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="${S3_BUCKET:-bedrock-cost-guard-deploy-${ACCOUNT_ID}-${REGION}}"

# Auto-detect your public IP for the security group, unless ALLOWED_CIDR is set.
if [ -z "${ALLOWED_CIDR:-}" ]; then
  MYIP=$(curl -s https://checkip.amazonaws.com || true)
  if [ -n "${MYIP}" ]; then
    ALLOWED_CIDR="${MYIP}/32"
  else
    ALLOWED_CIDR="127.0.0.1/32"
  fi
fi

echo "==> Account:     ${ACCOUNT_ID}"
echo "==> Region:      ${REGION}"
echo "==> Bucket:      ${S3_BUCKET}"
echo "==> AllowedCidr: ${ALLOWED_CIDR}"

# ----- 1. ensure bucket exists -----
if ! aws s3api head-bucket --bucket "${S3_BUCKET}" --region "${REGION}" 2>/dev/null; then
  echo "==> Creating bucket ${S3_BUCKET}..."
  if [ "${REGION}" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "${S3_BUCKET}" --region "${REGION}"
  else
    aws s3api create-bucket --bucket "${S3_BUCKET}" --region "${REGION}" \
      --create-bucket-configuration LocationConstraint="${REGION}"
  fi
fi

# ----- 2. package code -----
echo "==> Packaging lambda.zip..."
rm -f lambda.zip
zip -r lambda.zip common/ monitor/ reconciler/ web/ -x '*/__pycache__/*' '*.pyc' >/dev/null

# ----- 3. upload -----
echo "==> Uploading to s3://${S3_BUCKET}/${S3_KEY}..."
aws s3 cp lambda.zip "s3://${S3_BUCKET}/${S3_KEY}" --region "${REGION}"

# ----- 4. deploy stack -----
echo "==> Deploying stack ${STACK_NAME}..."
PARAMS="S3Bucket=${S3_BUCKET} S3Key=${S3_KEY} AllowedCidr=${ALLOWED_CIDR}"
if [ -n "${INSTANCE_TYPE:-}" ]; then
  PARAMS="${PARAMS} InstanceType=${INSTANCE_TYPE}"
fi
if [ -n "${KEY_NAME}" ]; then
  PARAMS="${PARAMS} KeyName=${KEY_NAME}"
fi

aws cloudformation deploy \
  --template-file template.yaml \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --parameter-overrides ${PARAMS} \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset

# ----- 5. refresh Lambda code (S3 ref unchanged across redeploys) -----
echo "==> Updating Lambda code..."
for FN in bedrock-cost-guard-monitor bedrock-cost-guard-reconciler; do
  aws lambda update-function-code \
    --function-name "${FN}" \
    --s3-bucket "${S3_BUCKET}" --s3-key "${S3_KEY}" \
    --region "${REGION}" >/dev/null
done

rm -f lambda.zip

# ----- 6. refresh web code on EC2 via SSM -----
# On first deploy the instance UserData already pulls the code, so this is
# belt-and-suspenders; on subsequent deploys it is the only thing that refreshes
# the EC2 web code. Non-fatal: if the instance isn't SSM-ready yet (still booting
# on first deploy), we warn and move on since UserData will have done the job.
INSTANCE_ID=$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='WebInstanceId'].OutputValue" --output text)

if [ -n "${INSTANCE_ID}" ] && [ "${INSTANCE_ID}" != "None" ]; then
  echo "==> Waiting for instance ${INSTANCE_ID} to register with SSM..."
  SSM_READY=""
  for _ in $(seq 1 30); do
    PING=$(aws ssm describe-instance-information --region "${REGION}" \
      --filters "Key=InstanceIds,Values=${INSTANCE_ID}" \
      --query "InstanceInformationList[0].PingStatus" --output text 2>/dev/null || true)
    if [ "${PING}" = "Online" ]; then
      SSM_READY="yes"
      break
    fi
    sleep 10
  done

  if [ -n "${SSM_READY}" ]; then
    echo "==> Updating web code on EC2 via SSM..."
    CMD_ID=$(aws ssm send-command \
      --instance-ids "${INSTANCE_ID}" \
      --document-name "AWS-RunShellScript" \
      --comment "Refresh bedrock-cost-guard web code" \
      --parameters commands="[\
        \"aws s3 cp s3://${S3_BUCKET}/${S3_KEY} /tmp/lambda.zip --region ${REGION}\",\
        \"mkdir -p /opt/bedrock-cost-guard\",\
        \"unzip -o /tmp/lambda.zip -d /opt/bedrock-cost-guard\",\
        \"rm -f /tmp/lambda.zip\",\
        \"systemctl restart bedrock-cost-guard-web || true\"\
      ]" \
      --region "${REGION}" \
      --query "Command.CommandId" --output text 2>/dev/null || true)
    if [ -n "${CMD_ID}" ] && [ "${CMD_ID}" != "None" ]; then
      aws ssm wait command-executed --command-id "${CMD_ID}" --instance-id "${INSTANCE_ID}" --region "${REGION}" 2>/dev/null \
        && echo "    Web code refreshed." \
        || echo "    ⚠ SSM command did not confirm success; first-boot UserData may still be running. Check manually if the console is stale."
    else
      echo "    ⚠ Failed to send SSM command; skipping web refresh."
    fi
  else
    echo "    ⚠ Instance not SSM-ready (likely first boot still in progress). UserData will deploy the web code; skipping SSM refresh."
  fi
else
  echo "    ⚠ WebInstanceId not found in stack outputs; skipping web refresh."
fi

IP=$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='WebPublicIp'].OutputValue" --output text)
echo "==> Done. Web console: http://${IP}"
