#!/usr/bin/env bash
# 로그인 계정 생성 (셀프 회원가입은 막혀 있음)
# 사용법: ./add-user.sh 이메일 비밀번호(8자 이상)
set -euo pipefail
[ $# -eq 2 ] || { echo "사용법: $0 <email> <password>"; exit 1; }
STACK=${STACK:-researchmate}
REGION=${AWS_REGION:-$(aws configure get region || echo ap-northeast-2)}
POOL=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)
aws cognito-idp admin-create-user --region "$REGION" --user-pool-id "$POOL" \
  --username "$1" --message-action SUPPRESS \
  --user-attributes Name=email,Value="$1" Name=email_verified,Value=true >/dev/null
aws cognito-idp admin-set-user-password --region "$REGION" --user-pool-id "$POOL" \
  --username "$1" --password "$2" --permanent
echo "계정 생성 완료: $1"
