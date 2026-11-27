"""공통 유틸: arXiv 검색, Bedrock 임베딩, 벡터 검색, LLM 호출."""
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from array import array
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", REGION)
EMBED_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
LLM_MODEL_ID = os.environ.get("LLM_MODEL_ID", "us.amazon.nova-2-lite-v1:0")
EMBED_DIM = 512

ddb = boto3.resource("dynamodb")
papers_tbl = ddb.Table(os.environ.get("PAPERS_TABLE", "papers"))
chunks_tbl = ddb.Table(os.environ.get("CHUNKS_TABLE", "chunks"))
chats_tbl = ddb.Table(os.environ.get("CHATS_TABLE", "chats"))
s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

ATOM = {"a": "http://www.w3.org/2005/Atom"}
UA = "ResearchMate-student-project/0.1"


# ---------------------------------------------------------------- arXiv
def safe_id(arxiv_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", arxiv_id)


def arxiv_search(query: str, max_results: int = 5) -> list[dict]:
    # 버그 수정: "all:a b c"는 첫 단어에만 all:이 적용돼 엉뚱한 논문이 나옴 → 단어마다 all: + AND
    terms = re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*", query)[:4]  # 단어가 많으면 AND 조건이 너무 좁아져 0건
    if not terms:
        return []
    out = _arxiv_query(" AND ".join(f"all:{t}" for t in terms), max_results)
    if not out and len(terms) > 2:  # 결과 없으면 앞 2단어로 다시 검색
        time.sleep(3)
        out = _arxiv_query(" AND ".join(f"all:{t}" for t in terms[:2]), max_results)
    return out


def _arxiv_query(search_query: str, max_results: int) -> list[dict]:
    q = urllib.parse.urlencode({
        "search_query": search_query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
    })
    req = urllib.request.Request(f"https://export.arxiv.org/api/query?{q}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        root = ET.fromstring(r.read())
    out = []
    for e in root.findall("a:entry", ATOM):
        raw_id = e.findtext("a:id", "", ATOM).rsplit("/abs/", 1)[-1]
        pdf = next((l.get("href") for l in e.findall("a:link", ATOM) if l.get("title") == "pdf"), None)
        out.append({
            "paper_id": safe_id(raw_id),
            "arxiv_id": raw_id,
            "title": " ".join(e.findtext("a:title", "", ATOM).split()),
            "summary": " ".join(e.findtext("a:summary", "", ATOM).split()),
            "authors": [a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)],
            "published": e.findtext("a:published", "", ATOM)[:10],
            "url": f"https://arxiv.org/abs/{raw_id}",
            "pdf_url": pdf or f"https://arxiv.org/pdf/{raw_id}",
        })
    return out


def save_paper_meta(p: dict) -> bool:
    """메타데이터 저장. 새로 저장됐으면 True."""
    try:
        papers_tbl.put_item(
            Item={**p, "status": "pending", "created_at": int(time.time())},
            ConditionExpression="attribute_not_exists(paper_id)",
        )
        return True
    except ddb.meta.client.exceptions.ConditionalCheckFailedException:
        return False


def download_pdf_to_s3(p: dict, bucket: str):
    req = urllib.request.Request(p["pdf_url"], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
    s3.put_object(
        Bucket=bucket,
        Key=f"papers/{p['paper_id']}.pdf",
        Body=body,
        ContentType="application/pdf",
        Metadata={"paper_id": p["paper_id"]},
    )


# ---------------------------------------------------------------- 임베딩 / 검색
def embed(text: str) -> list[float]:
    resp = bedrock.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=json.dumps({"inputText": text[:20000], "dimensions": EMBED_DIM, "normalize": True}),
    )
    return json.loads(resp["body"].read())["embedding"]


def vec_to_bytes(v: list[float]) -> bytes:
    return array("f", v).tobytes()


def bytes_to_vec(b) -> array:
    a = array("f")
    a.frombytes(bytes(b))
    return a


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def _all_chunks(paper_id: str | None):
    """paper_id가 있으면 그 논문만, 없으면 전체 스캔 (데모 규모용 브루트포스)."""
    if paper_id:
        kw = {"KeyConditionExpression": Key("paper_id").eq(paper_id)}
        while True:
            r = chunks_tbl.query(**kw)
            yield from r["Items"]
            if "LastEvaluatedKey" not in r:
                break
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    else:
        kw = {}
        while True:
            r = chunks_tbl.scan(**kw)
            yield from r["Items"]
            if "LastEvaluatedKey" not in r:
                break
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def vector_search(query: str, k: int = 6, paper_id: str | None = None) -> list[dict]:
    """벡터 유사도(코사인) + 키워드 매칭 가산점 하이브리드 검색."""
    qv = embed(query)
    terms = [t.lower() for t in re.findall(r"\w{3,}", query)]
    scored = []
    for it in _all_chunks(paper_id):
        s = _dot(qv, bytes_to_vec(it["emb"]))
        if terms:
            low = it["text"].lower()
            s += 0.05 * sum(t in low for t in terms) / len(terms)
        scored.append((s, it))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"paper_id": it["paper_id"], "idx": int(it["idx"]), "text": it["text"], "score": round(s, 4)}
        for s, it in scored[:k]
    ]


def get_paper(paper_id: str) -> dict | None:
    return papers_tbl.get_item(Key={"paper_id": paper_id}).get("Item")


# ---------------------------------------------------------------- LLM (Bedrock)
# LLM_MODEL_ID가 "anthropic."으로 시작하면 Claude(Anthropic SDK), 그 외(Amazon Nova 등)는 Bedrock Converse API
_llm = None


def llm(system: str, user: str, max_tokens: int = 4000) -> str:  # 호출당 출력 토큰 상한
    if not LLM_MODEL_ID.startswith("anthropic."):
        return _converse(system, user, max_tokens)
    return _claude(system, user, max_tokens)


def _converse(system: str, user: str, max_tokens: int) -> str:
    resp = bedrock.converse(
        modelId=LLM_MODEL_ID,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user}]}],
        inferenceConfig={"maxTokens": min(max_tokens, 5000), "temperature": 0.2},
    )
    if resp.get("stopReason") == "content_filtered":
        return "요청이 모델 안전 정책에 의해 거절되었습니다."
    return "".join(c.get("text", "") for c in resp["output"]["message"]["content"]).strip()


def _claude(system: str, user: str, max_tokens: int) -> str:
    global _llm
    if _llm is None:
        from anthropic import AnthropicBedrockMantle
        _llm = AnthropicBedrockMantle(aws_region=BEDROCK_REGION)
    resp = _llm.messages.create(
        model=LLM_MODEL_ID,
        max_tokens=max_tokens,
        output_config={"effort": "low"},  # API Gateway 30초 제한 때문에 빠르게
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason == "refusal":
        return "요청이 모델 안전 정책에 의해 거절되었습니다."
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def to_json(o):
    """DynamoDB Decimal/bytes → JSON 직렬화."""
    if isinstance(o, Decimal):
        return int(o) if o == int(o) else float(o)
    if isinstance(o, (bytes, bytearray)):
        return None
    if isinstance(o, set):
        return list(o)
    raise TypeError(type(o))
