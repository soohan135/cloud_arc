#!/usr/bin/env bash
# 과금 방지용 전체 삭제
set -euo pipefail
STACK=${STACK:-researchmate}
REGION=${AWS_REGION:-$(aws configure get region || echo ap-northeast-2)}
REGION=${REGION:-ap-northeast-2}
out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
aws s3 rm "s3://$(out WebBucketName)" --recursive || true
aws s3 rm "s3://$(out DocsBucketName)" --recursive || true
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK"
echo "삭제 완료"
