"""Поддельный cloudflared для e2e-тестов (STREAM_TTS_CLOUDFLARED в harness.py): то, что stream-tts берёт у
настоящего быстрого туннеля (tunnel.py), без сети.

usage: fake_cloudflared.py tunnel --no-autoupdate --metrics 127.0.0.1:<порт> --url http://127.0.0.1:<порт>

Метрики на --metrics: GET /ready — 200, GET /quicktunnel — {"hostname": "fake-<pid>.trycloudflare.com"}. Каждый запуск
дописывает строку JSON {pid, args, url, hostname} в $FAKE_CLOUDFLARED_DIR/runs.jsonl: по ней тест находит порт
страницы голосов и процесс. Файл $FAKE_CLOUDFLARED_DIR/fail — запуск падает с ошибкой, как у Cloudflare без
квоты; работает, пока его не убьют."""
import argparse, json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd")
    p.add_argument("--no-autoupdate", action="store_true")
    p.add_argument("--metrics", required=True)
    p.add_argument("--url", required=True)
    a = p.parse_args()
    d = os.environ["FAKE_CLOUDFLARED_DIR"]
    os.makedirs(d, exist_ok=True)
    print("INF Requesting new quick Tunnel on trycloudflare.com...", flush=True)
    if os.path.exists(os.path.join(d, "fail")):
        print('ERR Error unmarshaling QuickTunnel response: error code: 1015', flush=True)
        sys.exit(1)
    host = f"fake-{os.getpid()}.trycloudflare.com"
    with open(os.path.join(d, "runs.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(pid=os.getpid(), args=sys.argv[1:], url=a.url, hostname=host)) + "\n")

    class Metrics(BaseHTTPRequestHandler):
        def do_GET(self):
            body = {"/ready": {"status": 200, "readyConnections": 1},
                    "/quicktunnel": {"hostname": host}}.get(self.path)
            data = json.dumps(body).encode()
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    mhost, mport = a.metrics.rsplit(":", 1)
    server = ThreadingHTTPServer((mhost, int(mport)), Metrics)
    print(f"INF |  https://{host}  |", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
