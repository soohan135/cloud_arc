"""질의 API Lambda (API Gateway HTTP API, Cognito JWT 인증)
POST /api/search     {query}                       arXiv 실시간 검색 + 목록 저장 + 수집 트리거
POST /api/chat       {question, paper_id?}         RAG 질의응답 (근거/출처 포함)
POST /api/summarize  {paper_id}                    목적/방법/결과/한계 구조화 요약
POST /api/cite       {paper_id, my_research}       인용문 초안
GET  /api/papers                                   저장된 논문 목록
GET  /api/history                                  내 대화 기록
"""
import json
import os
import time

import boto3
from boto3.dynamodb.conditions import Key

from common import (arxiv_search, chats_tbl, chunks_tbl, get_paper, llm, papers_tbl,
                    save_paper_meta, to_json, vector_search)

COLLECT_FN = os.environ["COLLECT_FUNCTION"]
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "30"))
MAX_INPUT = 2000  # 입력 글자 수 제한 (토큰 폭탄 방지)
lam = boto3.client("lambda")
usage_tbl = boto3.resource("dynamodb").Table(os.environ["USAGE_TABLE"])


def take_quota(user) -> bool:
    """사용자별 일일 호출 횟수를 원자적으로 1 증가. 한도 초과면 False."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        usage_tbl.update_item(
            Key={"pk": f"{user}#{day}"},
            UpdateExpression="ADD cnt :one SET expires_at = :exp",
            ConditionExpression="attribute_not_exists(cnt) OR cnt < :lim",
            ExpressionAttributeValues={":one": 1, ":lim": DAILY_LIMIT, ":exp": int(time.time()) + 172800},
        )
        return True
    except usage_tbl.meta.client.exceptions.ConditionalCheckFailedException:
        return False

GROUNDING = (
    "너는 연구 보조 AI '리서치메이트'다. 반드시 제공된 [근거] 안의 내용만 사용해 한국어로 답한다. "
    "근거에 없는 내용은 추측하지 말고 '제공된 논문에서 확인할 수 없습니다'라고 말한다. "
    "문장마다 해당 근거 번호를 [1], [2] 형식으로 표시한다."
)


def resp(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False, default=to_json),
    }


def paper_brief(pid):
    p = get_paper(pid) or {}
    return {k: p.get(k) for k in ("paper_id", "title", "authors", "published", "url")}


def build_context(hits):
    blocks, sources = [], []
    for i, h in enumerate(hits, 1):
        meta = paper_brief(h["paper_id"])
        blocks.append(f"[{i}] (논문: {meta.get('title')})\n{h['text']}")
        sources.append({"n": i, **meta, "snippet": h["text"][:300], "score": h["score"]})
    return "\n\n".join(blocks), sources


# ---------------------------------------------------------------- handlers
def search(user, body):
    q = (body.get("query") or "").strip()
    if not q:
        return resp(400, {"error": "query 필요"})
    papers = arxiv_search(q, min(int(body.get("max", 5)), 5))
    new = [p for p in papers if save_paper_meta(p)]
    if new:  # PDF 다운로드는 수집 Lambda에 비동기로 넘김 → S3 이벤트 → 임베딩
        lam.invoke(FunctionName=COLLECT_FN, InvocationType="Event",
                   Payload=json.dumps({"papers": new}).encode())
    for p in papers:
        p["status"] = (get_paper(p["paper_id"]) or {}).get("status", "pending")
    return resp(200, {"papers": papers})


def chat(user, body):
    q = (body.get("question") or "").strip()
    if not q:
        return resp(400, {"error": "question 필요"})
    hits = vector_search(q, k=6, paper_id=body.get("paper_id"))
    if not hits:
        return resp(200, {"answer": "아직 인덱싱된 논문이 없습니다. 먼저 논문을 검색해 수집해 주세요.", "sources": []})
    ctx, sources = build_context(hits)
    answer = llm(GROUNDING, f"[근거]\n{ctx}\n\n[질문]\n{q}")
    chats_tbl.put_item(Item={"user_id": user, "ts": int(time.time() * 1000), "mode": "chat",
                             "question": q, "answer": answer})
    return resp(200, {"answer": answer, "sources": sources})


def paper_text(pid, limit=14000):
    items = chunks_tbl.query(KeyConditionExpression=Key("paper_id").eq(pid))["Items"]
    items.sort(key=lambda x: int(x["idx"]))
    return "\n".join(i["text"] for i in items)[:limit]


def summarize(user, body):
    pid = body.get("paper_id")
    p = get_paper(pid) if pid else None
    if not p:
        return resp(404, {"error": "논문 없음"})
    text = paper_text(pid)
    if not text:
        return resp(409, {"error": "아직 인덱싱 중입니다. 잠시 후 다시 시도하세요."})
    prompt = (
        f"논문 제목: {p['title']}\n\n[논문 본문 일부]\n{text}\n\n"
        "위 논문을 아래 형식의 마크다운으로 요약하라. 본문에 근거가 없는 항목은 '본문에서 확인 불가'로 적는다.\n"
        "## 한 줄 요약\n## 연구 목적\n## 연구 방법\n## 주요 결과\n## 한계 및 향후 연구\n## 핵심 키워드"
    )
    summary = llm("너는 논문을 정확하게 요약하는 연구 보조 AI다. 한국어로 답한다.", prompt)
    chats_tbl.put_item(Item={"user_id": user, "ts": int(time.time() * 1000), "mode": "summarize",
                             "question": p["title"], "answer": summary})
    return resp(200, {"summary": summary, "paper": paper_brief(pid)})


def cite(user, body):
    pid, mine = body.get("paper_id"), (body.get("my_research") or "").strip()
    p = get_paper(pid) if pid else None
    if not p or not mine:
        return resp(400, {"error": "paper_id, my_research 필요"})
    hits = vector_search(mine, k=4, paper_id=pid)
    ctx, sources = build_context(hits)
    authors = p.get("authors") or []
    ref = f"{', '.join(authors[:3])}{' et al.' if len(authors) > 3 else ''} ({p.get('published', '')[:4]}). {p['title']}. arXiv:{p.get('arxiv_id', pid)}."
    prompt = (
        f"[내 연구 설명]\n{mine}\n\n[인용할 논문]\n제목: {p['title']}\n참고문헌: {ref}\n\n[논문 근거]\n{ctx}\n\n"
        "다음을 마크다운으로 작성하라.\n"
        "## 연관성 분석 (내 연구와 이 논문이 어떤 지점에서 연결되는지, 근거 번호 표시)\n"
        "## 인용문 초안 (논문 본문에 넣을 수 있는 한국어 문장 2~3개, 영어 버전 1개, 저자-연도 표기)\n"
        "## 참고문헌 (APA)"
    )
    draft = llm(GROUNDING, prompt)
    chats_tbl.put_item(Item={"user_id": user, "ts": int(time.time() * 1000), "mode": "cite",
                             "question": mine, "answer": draft})
    return resp(200, {"draft": draft, "reference": ref, "sources": sources})


def list_papers(user, body):
    items = papers_tbl.scan(Limit=200)["Items"]
    items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return resp(200, {"papers": items})


def history(user, body):
    items = chats_tbl.query(KeyConditionExpression=Key("user_id").eq(user),
                            ScanIndexForward=False, Limit=30)["Items"]
    return resp(200, {"history": items})


LIMITED = {"POST /api/search", "POST /api/chat", "POST /api/summarize", "POST /api/cite"}

ROUTES = {
    "POST /api/search": search,
    "POST /api/chat": chat,
    "POST /api/summarize": summarize,
    "POST /api/cite": cite,
    "GET /api/papers": list_papers,
    "GET /api/history": history,
}


def health():
    """배포 확인용 (인증 불필요, LLM 호출 없음): 각 AWS 리소스 연결 상태."""
    checks = {}
    try:
        checks["dynamodb_papers"] = papers_tbl.scan(Select="COUNT", Limit=1000)["Count"]
    except Exception as e:  # noqa: BLE001
        checks["dynamodb_papers"] = f"error: {type(e).__name__}"
    try:
        r = boto3.client("s3").list_objects_v2(Bucket=os.environ["DOCS_BUCKET"], Prefix="papers/", MaxKeys=1000)
        checks["s3_pdfs"] = r.get("KeyCount", 0)
    except Exception as e:  # noqa: BLE001
        checks["s3_pdfs"] = f"error: {type(e).__name__}"
    ok = all(isinstance(v, int) for v in checks.values())
    return resp(200 if ok else 503, {
        "status": "ok" if ok else "degraded",
        "service": "ResearchMate",
        "region": os.environ.get("AWS_REGION"),
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checks": checks,
    })


def handler(event, context):
    route = event.get("routeKey")
    if route == "GET /api/health":
        return health()
    fn = ROUTES.get(route)
    if not fn:
        return resp(404, {"error": f"unknown route {route}"})
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    user = claims.get("sub")
    if not user:
        return resp(401, {"error": "unauthorized"})
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    for k in ("query", "question", "my_research", "paper_id"):
        if isinstance(body.get(k), str) and len(body[k]) > MAX_INPUT:
            return resp(400, {"error": f"{k}는 {MAX_INPUT}자 이하로 입력하세요"})
    if route in LIMITED and not take_quota(user):
        return resp(429, {"error": f"오늘 사용 한도({DAILY_LIMIT}회)를 초과했습니다. 내일 다시 시도하세요."})
    try:
        return fn(user, body)
    except Exception as e:  # noqa: BLE001
        print("ERROR", route, repr(e))  # 상세 내용은 CloudWatch 로그에만
        return resp(500, {"error": "서버 오류가 발생했습니다."})
