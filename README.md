# ResearchMate — arXiv 논문 RAG 챗봇 (AWS)

기획서 아키텍처를 그대로 서버리스로 구현한 버전.

| 기획서 구성요소 | 구현 |
|---|---|
| 웹 프런트엔드 | CloudFront + S3 (`web/index.html`), `/api/*`는 CloudFront → API Gateway로 라우팅 |
| 인증 | Cognito User Pool (JWT), API Gateway JWT Authorizer |
| 백엔드 API | API Gateway HTTP API `/api/search`, `/api/chat`, `/api/summarize`, `/api/cite`, `/api/papers`, `/api/history` |
| 백엔드 로직 | Lambda `src/api.py` |
| 논문 수집 | EventBridge(매일 03:00 KST) → Lambda `src/collect.py` → arXiv API → S3 / DynamoDB |
| 추출·분할·임베딩 | S3 이벤트 → Lambda `src/ingest.py` (pypdf → 1500자 청크 → Bedrock Titan Embeddings v2) |
| LLM | Amazon Bedrock — Claude Opus 5.5 (`anthropic.claude-opus-5-5`) |
| 메타데이터·대화기록 | DynamoDB `Papers`, `Chats` |
| 벡터 검색 | DynamoDB `Chunks` + Lambda 내 코사인 유사도 + 키워드 가산점 (하이브리드) |
| 로그·모니터링 | CloudWatch Logs + CloudWatch Alarm(API Lambda 에러) |

> **OpenSearch Serverless 대신 DynamoDB 벡터 저장을 쓴 이유:** OpenSearch Serverless는 최소 OCU 과금이 있어서 켜두기만 해도 한 달에 수십만 원이 나옴. 학생 데모 규모(논문 수십~수백 편)에선 DynamoDB에 임베딩을 저장하고 Lambda에서 유사도를 계산해도 충분히 빠름. 발표 때 "비용 최적화를 위한 설계 변경"이라고 설명하면 됨.

## 배포

준비물: AWS CLI v2(`aws configure` 완료), Python 3, Bedrock 콘솔에서 **Claude Opus 5.5 / Titan Text Embeddings V2 모델 액세스 활성화** (기본은 us-east-1)

```bash
./deploy.sh            # 처음엔 CloudFront 생성 때문에 5~10분 걸림
```

끝나면 `https://xxxx.cloudfront.net` 주소가 출력됨.

환경변수로 바꿀 수 있는 것: `AWS_REGION`(스택 리전, 기본 ap-northeast-2), `BEDROCK_REGION`(기본 us-east-1), `STACK`.
수집 키워드는 `template.yaml`의 `Keywords` 파라미터.

계정 만들기 (셀프 회원가입은 막혀 있음):

```bash
./add-user.sh me@example.com 'mypassword123'
```

비용 경보 메일을 받고 싶으면 `ALERT_EMAIL=me@example.com ./deploy.sh` (월 $10의 80% 넘으면 메일).

## 보안 / 비용 폭탄 방지

| 위협 | 대응 |
|---|---|
| 깃에 키 유출 | 코드에 키 없음. 실행 시 `aws configure` 자격증명 사용. `.gitignore`로 `*.csv`(콘솔 키 파일), `.env`, `config.js`, 로그, 빌드물 제외 |
| 아무나 가입해서 LLM 남용 | Cognito 셀프 가입 차단 (`AllowAdminCreateUserOnly`), 관리자가 `add-user.sh`로만 계정 생성 |
| 로그인 없이 API 호출 | API Gateway JWT Authorizer — 토큰 없으면 401 |
| 한 계정으로 무한 호출 | 사용자별 일일 호출 한도 (`DailyLimit`, 기본 30회) |
| 대량 요청 / 봇 | API Gateway 스로틀링 (초당 2건, 버스트 5) |
| 긴 입력으로 토큰 폭탄 | 입력 2000자 제한, LLM 출력 4000토큰 상한, 검색 최대 5건 |
| 오류 메시지로 내부 정보 노출 | 500 오류는 일반 메시지만 반환, 상세는 CloudWatch 로그에만 |
| 그래도 비용이 샐 때 | AWS Budgets 월 예산 경보 + `./destroy.sh`로 즉시 전체 삭제 |

> Cognito Client ID, CloudFront 주소는 원래 브라우저에 공개되는 값이라 비밀이 아님. 진짜 비밀은 **AWS 액세스 키** 하나뿐.

## 사용 흐름

1. 왼쪽에서 키워드로 arXiv 검색 → 결과가 목록으로 저장되고 PDF 수집이 자동으로 시작됨 (`pending` → 1~2분 뒤 `indexed`, "저장된 논문 새로고침"으로 확인)
2. **질문하기**: 논문을 선택하지 않으면 전체 논문 대상, 선택하면 그 논문만 대상으로 근거 [n] + 출처와 함께 답변
3. **요약**: 논문 선택 → 목적/방법/결과/한계 구조화 요약
4. **인용문 초안**: 논문 선택 + 내 연구 설명 입력 → 연관성 분석 + 인용 문장 + APA 참고문헌

## 삭제 (과금 방지)

```bash
./destroy.sh
```

## 디버깅

- Lambda 로그: CloudWatch Logs `/aws/lambda/researchmate-ApiFunction-...` 등
- `AccessDenied`/모델 오류 → Bedrock 모델 액세스 활성화 여부, `BEDROCK_REGION` 확인
- 질문 응답이 30초 넘으면 API Gateway가 끊음 → `common.py`의 `LLM_MODEL_ID`를 더 빠른 모델로 바꾸거나 `MAX_CHUNKS`/k값 축소
