"""S3 이벤트(papers/*.pdf 업로드) → 텍스트 추출 → 청크 분할 → Bedrock 임베딩 → DynamoDB 벡터 저장."""
import io
import os
import urllib.parse

from pypdf import PdfReader

from common import chunks_tbl, embed, get_paper, papers_tbl, s3, vec_to_bytes

CHUNK = 1500
OVERLAP = 200
MAX_CHUNKS = int(os.environ.get("MAX_CHUNKS", "40"))


def split(text: str) -> list[str]:
    text = " ".join(text.split())
    out, i = [], 0
    while i < len(text) and len(out) < MAX_CHUNKS:
        out.append(text[i:i + CHUNK])
        i += CHUNK - OVERLAP
    return out


def handler(event, context):
    for rec in event["Records"]:
        bucket = rec["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
        paper_id = key.rsplit("/", 1)[-1].removesuffix(".pdf")
        print("ingest", paper_id)

        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        try:
            reader = PdfReader(io.BytesIO(body))
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
        except Exception as e:  # noqa: BLE001
            print("pdf parse failed", e)
            text = ""

        meta = get_paper(paper_id) or {}
        head = f"Title: {meta.get('title', '')}\nAbstract: {meta.get('summary', '')}"
        s3.put_object(Bucket=bucket, Key=f"texts/{paper_id}.txt", Body=text.encode("utf-8"))

        chunks = [head] + split(text)
        with chunks_tbl.batch_writer() as bw:
            for idx, c in enumerate(chunks):
                bw.put_item(Item={
                    "paper_id": paper_id,
                    "idx": idx,
                    "text": c,
                    "emb": vec_to_bytes(embed(c)),
                })

        papers_tbl.update_item(
            Key={"paper_id": paper_id},
            UpdateExpression="SET #s = :s, n_chunks = :n",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "indexed", ":n": len(chunks)},
        )
        print("indexed", paper_id, len(chunks))
    return {"ok": True}
