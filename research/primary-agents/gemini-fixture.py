"""Single-turn Gemini fixture. Log markers and tool names only."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class Gemini(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        route = urlsplit(self.path).path
        names = [
            fn["name"] for group in data.get("tools", [])
            for fn in group.get("functionDeclarations", group.get("function_declarations", []))
        ]
        text = json.dumps(data)
        clicked = "visible fixture button now reads 1" in text
        print(route, "context", "FACTORY_CONTEXT_701" in text, "project", "PROJECT_CONTEXT_702" in text,
              "placeholder", "fixture-placeholder" in list(self.headers.values()), "clicked", clicked,
              "search_denied", "FACTORY_SEARCH_DENIED" in text, names, flush=True)
        marker = Path("/tmp/asked-agy")
        part = {"text": "Fixture complete."}
        if "call_mcp_tool" in names and not marker.exists():
            marker.touch()
            part = {"functionCall": {"name": "call_mcp_tool", "args": {"ServerName": "factory", "ToolName": "view_fixture", "Arguments": {}}}}
            if Path("/tmp/request-search-agy").exists():
                part = {"functionCall": {"name": "search_web", "args": {"query": "fixture"}}}
        reply = {"candidates": [{"index": 0, "content": {"role": "model", "parts": [part]}, "finishReason": "STOP"}],
                 "usageMetadata": {"promptTokenCount": 42, "candidatesTokenCount": 7, "totalTokenCount": 49}}
        stream = "streamGenerateContent" in route
        body = json.dumps(reply)
        if stream:
            body = "data: " + body + "\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
        self.end_headers()
        self.wfile.write(body.encode())


HTTPServer(("127.0.0.1", 18783), Gemini).serve_forever()
