"""Static loopback-only chord converter. No text ingestion or persistence."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
ROOT = Path(__file__).resolve().parent / 'dist'
FILES = {'/': ('index.html','text/html; charset=utf-8'), '/index.html': ('index.html','text/html; charset=utf-8'), '/style.css': ('style.css','text/css; charset=utf-8'), '/app.mjs': ('app.mjs','text/javascript; charset=utf-8'), '/converter.mjs': ('converter.mjs','text/javascript; charset=utf-8')}
class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self): self.serve(True)
    def do_GET(self): self.serve(False)
    def serve(self, head):
        path = urlsplit(self.path).path
        if path == '/healthz':
            body = json.dumps({'app':'wcfi-chord-converter','status':'ok','version':1}).encode(); kind='application/json'
        elif path in FILES:
            name,kind=FILES[path]; body=(ROOT/name).read_bytes()
        else:
            self.send_error(404); return
        self.send_response(200)
        for name,value in [('Content-Type',kind),('Content-Length',str(len(body))),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('Referrer-Policy','no-referrer')]: self.send_header(name,value)
        self.end_headers()
        if not head: self.wfile.write(body)
    def log_message(self, *args): pass
if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1',8818),Handler).serve_forever()
