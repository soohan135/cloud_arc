#!/usr/bin/env bash
# ResearchMate 배포 스크립트 (AWS CLI만 있으면 됨, SAM CLI/Docker 불필요)
set -euo pipefail
cd "$(dirname "$0")"

STACK=${STACK:-researchmate}
REGION=${AWS_REGION:-$(aws configure get region || echo ap-northeast-2)}
REGION=${REGION:-ap-northeast-2}
BEDROCK_REGION=${BEDROCK_REGION:-us-east-1}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
ARTIFACTS="${STACK}-artifacts-${ACCOUNT}-${REGION}"

echo "==> [1/5] Lambda 코드 빌드 (.build/)"
rm -rf .build && mkdir .build
cp src/*.py .build/
python3 -m pip install -q -r src/requirements.txt -t .build \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.13 \
  --only-binary=:all: --upgrade

echo "==> [2/5] 아티팩트 버킷 준비: $ARTIFACTS"
aws s3api head-bucket --bucket "$ARTIFACTS" 2>/dev/null || \
  aws s3 mb "s3://$ARTIFACTS" --region "$REGION"

echo "==> [3/5] CloudFormation 패키징/배포 (처음엔 CloudFront 때문에 5~10분 걸림)"
aws cloudformation package --region "$REGION" \
  --template-file template.yaml --s3-bucket "$ARTIFACTS" \
  --output-template-file .build/packaged.yaml >/dev/null
aws cloudformation deploy --region "$REGION" \
  --template-file .build/packaged.yaml --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides BedrockRegion="$BEDROCK_REGION" AlertEmail="${ALERT_EMAIL:-}" \
    ${LLM_MODEL_ID:+LlmModelId="$LLM_MODEL_ID"}

out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

echo "==> [4/5] 프런트엔드 업로드"
cat > web/config.js <<EOF
window.RM_CONFIG = { region: "$REGION", clientId: "$(out UserPoolClientId)" };
EOF
aws s3 sync web "s3://$(out WebBucketName)" --delete --region "$REGION"
aws cloudfront create-invalidation --distribution-id "$(out DistributionId)" --paths "/*" >/dev/null

echo "==> [5/5] 초기 논문 수집 실행 (비동기)"
aws lambda invoke --region "$REGION" --function-name "$(out CollectFunctionName)" \
  --invocation-type Event --cli-binary-format raw-in-base64-out --payload '{}' /dev/null >/dev/null

echo
echo "완료! 접속 주소: $(out SiteUrl)"
