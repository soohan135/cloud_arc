"""논문 수집 Lambda.
- EventBridge 스케줄(매일 1회): KEYWORDS 환경변수의 키워드로 arXiv 검색 → 메타데이터 DynamoDB 저장 → PDF를 S3에 업로드
- API Lambda가 비동기 호출: {"papers": [...]} 로 넘긴 논문 PDF 다운로드
S3 업로드가 되면 ingest Lambda가 S3 이벤트로 자동 실행된다.
"""
import os
import time

from common import arxiv_search, download_pdf_to_s3, papers_tbl, save_paper_meta

BUCKET = os.environ["DOCS_BUCKET"]
KEYWORDS = [k.strip() for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()]
MAX_PER_KEYWORD = int(os.environ.get("MAX_PER_KEYWORD", "3"))


def _fetch(p: dict):
    try:
        download_pdf_to_s3(p, BUCKET)
        print("downloaded", p["paper_id"])
    except Exception as e:  # noqa: BLE001
        print("download failed", p["paper_id"], e)
        papers_tbl.update_item(
            Key={"paper_id": p["paper_id"]},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "failed"},
        )
    time.sleep(3)  # arXiv 이용 정책: 요청 간 3초


def handler(event, context):
    if event.get("papers"):  # API에서 넘긴 검색 결과
        for p in event["papers"]:
            _fetch(p)
        return {"ok": True, "count": len(event["papers"])}

    new = 0
    for kw in KEYWORDS:
        for p in arxiv_search(kw, MAX_PER_KEYWORD):
            if save_paper_meta(p):
                new += 1
                _fetch(p)
        time.sleep(3)
    print(f"collected {new} new papers")
    return {"ok": True, "new": new}
