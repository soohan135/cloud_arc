#!/usr/bin/env bash
# 과금 방지용 전체 삭제
set -euo pipefail
STACK=${STACK:-researchmate}
REGION=${AWS_REGION:-$(aws configure get region || echo ap-northeast-2)}
REGION=${REGION:-ap-northeast-2}
out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
echo "※ CloudFront 무료(Flat-rate) 플랜에 가입했다면, 먼저 콘솔에서 플랜을 취소(pay-as-you-go로 전환)해야 삭제됩니다."
read -r -p "계속할까요? [y/N] " yn; [ "$yn" = "y" ] || exit 1
aws s3 rm "s3://$(out WebBucketName)" --recursive || true
aws s3 rm "s3://$(out DocsBucketName)" --recursive || true
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK"
echo "삭제 완료"
