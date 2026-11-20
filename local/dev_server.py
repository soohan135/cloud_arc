"""로컬 개발 서버: web/ 정적 파일 + /api/* 를 src/api.py 핸들러로 직접 연결.
- 로그인(Cognito) 생략, 사용자 = "local-dev"
- DynamoDB / Bedrock / 수집 Lambda는 배포된 AWS 스택 것을 사용 (먼저 ./deploy.sh 필요)
- 127.0.0.1 에서만 열림

실행: python3 local/dev_server.py   →  http://127.0.0.1:8000
"""
import json
import os
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STACK = os.environ.get("STACK", "researchmate")
PORT = int(os.environ.get("PORT", "8000"))


def stack_resources() -> dict:
    out = subprocess.check_output([
        "aws", "cloudformation", "describe-stack-resources", "--stack-name", STACK,
        "--query", "StackResources[].[LogicalResourceId,PhysicalResourceId]", "--output", "json",
    ])
    return dict(json.loads(out))


res = stack_resources()
os.environ.update({
    "PAPERS_TABLE": res["PapersTable"],
    "CHUNKS_TABLE": res["ChunksTable"],
    "CHATS_TABLE": res["ChatsTable"],
    "USAGE_TABLE": res["UsageTable"],
    "COLLECT_FUNCTION": res["CollectFunction"],
    "DOCS_BUCKET": res["DocsBucket"],
})
os.environ.setdefault("BEDROCK_REGION", "us-east-1")
sys.path.insert(0, str(ROOT / "src"))
import api  # noqa: E402

LOCAL_CONFIG = b'window.RM_CONFIG = { local: true };\n'


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT / "web"), **kw)

    def _api(self, method):
        path = self.path.split("?", 1)[0]
        n = int(self.headers.get("Content-Length") or 0)
        event = {
            "routeKey": f"{method} {path}",
            "body": self.rfile.read(n).decode() if n else None,
            "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "local-dev"}}}},
        }
        r = api.handler(event, None)
        body = r["body"].encode()
        self.send_response(r["statusCode"])
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._api("GET")
        if self.path.startswith("/config.js"):
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.end_headers()
            return self.wfile.write(LOCAL_CONFIG)
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            return self._api("POST")
        self.send_error(404)


if __name__ == "__main__":
    print(f"http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
