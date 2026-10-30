"""PROTOTYPE (issue #96), throwaway: serve the explorable-page prototype on http://127.0.0.1:8765/.

Run:  python3 src/resilient_triage/api/static/prototype_explorable/serve.py
Then open /?variant=A (or B, C). Re-record traces with record_traces.py.
"""

import http.server
from functools import partial
from pathlib import Path

HERE = Path(__file__).parent
HEAD = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
)


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?")[0] in ("/", "/index.html"):
            body = (HEAD + (HERE / "page.html").read_text() + "</html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class Server(http.server.ThreadingHTTPServer):
    # The default backlog of 5 drops some of the browser's parallel module requests.
    request_queue_size = 64


if __name__ == "__main__":
    server = Server(("127.0.0.1", 8765), partial(Handler, directory=str(HERE)))
    print("Prototype on http://127.0.0.1:8765/?variant=A")
    server.serve_forever()
