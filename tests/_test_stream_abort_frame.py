"""A stream that dies after the first chunk must say so, not send [DONE].

Issue #549: the Chat Completions path caught a mid-body read error and
appended `data: [DONE]`, which every client reads as a normal finish. The
truncated answer then looked complete. This suite drives a real gateway
against a mock upstream that sends one chunk, promises more, and closes.

Run with: python tests/_test_stream_abort_frame.py
No network, no credentials: only loopback sockets on ephemeral ports.
"""
import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import _lifecycle as life  # noqa: E402
from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-stream-abort-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]
USAGE_DIR = os.environ["WB_PROXY_USAGE_DIR"]
os.environ["HOME"] = _TMP.name
os.environ["USERPROFILE"] = _TMP.name
os.environ["WB_PROXY_KEY"] = "TESTKEY"

MODEL = "deepseek-v4.1-flash"


def jwt(uid="acct-stream-abort-1"):
    def seg(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return seg({"alg": "none", "typ": "JWT"}) + "." + seg(
        {"uid": uid, "exp": int(time.time()) + 86400 * 30}) + ".x"


for index in (1, 2):
    uid = "acct-stream-abort-%d" % index
    with open(os.path.join(ACCOUNTS, "acct%d.json" % index), "w", encoding="utf-8") as fh:
        json.dump({
            "uid": uid,
            "accessToken": jwt(uid),
            "refreshToken": "",
            "expiresAt": int(time.time()) + 86400 * 30,
            "realm": "intl",
            "domain": "www.workbuddy.ai",
            "platform": "CLI",
            "product": "workbuddy",
            "enabled": True,
        }, fh)

UPSTREAM_PORT = life.free_port()
MOCK = {"stream_calls": 0, "model_error": None, "audio_calls": 0}


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def respond(self, payload, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path.endswith("/chat/completions"):
            MOCK["stream_calls"] += 1
            if MOCK["model_error"]:
                if MOCK.get("error_http"):
                    return self.respond(json.dumps(MOCK["model_error"]).encode("utf-8"),
                                        "application/json", status=502)
                data = "data: %s\n\ndata: [DONE]\n\n" % json.dumps(MOCK["model_error"])
                return self.respond(data.encode("utf-8"), "text/event-stream")
            chunk = {"id": "up-1", "model": MODEL,
                     "choices": [{"index": 0, "delta": {"content": "partial answer"}}]}
            payload = ("data: " + json.dumps(chunk) + "\n\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            # Promise more than is sent: iter_sse_lines turns the missing
            # tail into IncompleteRead after the first chunk is delivered.
            self.send_header("Content-Length", str(len(payload) + 4096))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            self.close_connection = True
            return
        if self.path.endswith("/audio/speech"):
            MOCK["audio_calls"] += 1
            delta = {"type": "speech.audio.delta", "audio": {"data": "YWI="}}
            payload = "data: %s\n\n" % json.dumps(delta)
            if MOCK["audio_calls"] > 1:
                payload = 'data: {"type":"speech.audio.delta","audio":{"data":"Y2Q="}}\n\n'
                payload += 'data: {"type":"speech.audio.done"}\n\n'
            return self.respond(payload.encode("utf-8"), "text/event-stream")
        if self.path.endswith("/agenttool/v1/asr"):
            MOCK["audio_calls"] += 1
            return self.respond(b'{"code":0,"text":"recognized"}', "application/json")
        payload = b'{"data":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        payload = b'{"data":{}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


upstream = ThreadingHTTPServer(("127.0.0.1", UPSTREAM_PORT), MockUpstream)
threading.Thread(target=upstream.serve_forever, daemon=True).start()

import wb_identity  # noqa: E402
import wb_proxy as P  # noqa: E402

_ENDPOINTS = dict(wb_identity._ENDPOINTS)
for key in list(_ENDPOINTS):
    _ENDPOINTS[key] = ("http://127.0.0.1:%d" % UPSTREAM_PORT, _ENDPOINTS[key][1])
wb_identity._ENDPOINTS.update(_ENDPOINTS)

GATEWAY_PORT = life.free_port()


class Args(object):
    host = "127.0.0.1"
    port = GATEWAY_PORT
    api_key = "TESTKEY"
    system_prompt = "You are a helpful assistant."
    user_agent = None
    usage_dir = USAGE_DIR
    accounts_dir = ACCOUNTS
    import_desktop = False
    lan = False
    panel_password = None
    info = None


P._apply_cli_overrides(Args())
P._bootstrap_runtime(Args())
gateway = ThreadingHTTPServer(("127.0.0.1", GATEWAY_PORT), P.Handler)
threading.Thread(target=gateway.serve_forever, daemon=True).start()

PASS = FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, detail))


def wait_ready():
    for _ in range(60):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/health" % GATEWAY_PORT, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def parse_frames(raw):
    frames = []
    for block in raw.decode("utf-8", "replace").split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                frames.append("[DONE]")
            elif data:
                try:
                    frames.append(json.loads(data))
                except Exception:
                    frames.append({"__raw__": data})
    return frames


def gateway_post(path, payload, content_type="application/json"):
    data = json.dumps(payload).encode("utf-8") if isinstance(payload, dict) else payload
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (GATEWAY_PORT, path),
                                 data=data, method="POST", headers={
                                     "Content-Type": content_type,
                                     "Authorization": "Bearer TESTKEY",
                                 })
    try:
        with urllib.request.urlopen(req, timeout=30) as reply:
            return reply.status, reply.headers, reply.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.headers, exc.read()


try:
    if not wait_ready():
        print("  [FAIL] gateway did not start")
        sys.exit(1)

    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % GATEWAY_PORT,
        data=json.dumps({"model": MODEL, "stream": True,
                         "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
        method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer TESTKEY")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            code, body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        code, body = exc.code, exc.read()

    frames = parse_frames(body)
    text = "".join(
        (ch.get("delta") or {}).get("content") or ""
        for f in frames if isinstance(f, dict)
        for ch in (f.get("choices") or []))
    errors = [f for f in frames if isinstance(f, dict) and f.get("error")]

    check("200 with a parsed SSE stream", code == 200 and frames, (code, body[:200]))
    check("the delivered chunk reached the client", "partial answer" in text, text)
    check("a terminal error frame was sent", len(errors) == 1, frames)
    check("the error names the upstream abort",
          bool(errors) and errors[0]["error"].get("code") == "upstream_stream_error",
          errors)
    check("no [DONE] pretends the turn finished", "[DONE]" not in frames, frames)
    check("the stream was not retried after bytes were sent",
          MOCK["stream_calls"] == 1, MOCK["stream_calls"])

    P.wb_settings.set_enable_anthropic_messages(ACCOUNTS, True)
    for path, params in (
        ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
        ("/v1/responses", {"input": "hi"}),
        ("/v1/messages", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 32}),
    ):
        for stream, failure in (
            (False, {"error": {"code": 11102, "message": "model missing"}}),
            (True, {"code": "11102", "msg": "model missing"}),
        ):
            MOCK["model_error"] = failure
            count = MOCK["stream_calls"]
            code, _, body = gateway_post(path, dict(params, model=MODEL, stream=stream))
            check("%s stream=%s reports 11102 once" % (path, stream),
                  code == (200 if stream else 400) and b"11102" in body
                  and MOCK["stream_calls"] - count == 1, (code, body[:300]))
            if stream:
                check("%s omits a successful stream terminator" % path,
                      all(terminal not in body for terminal in (
                          b"[DONE]", b"response.completed", b"message_stop")), body[:300])

    for error_code in (11103, 11152):
        for http_error in (False, True):
            count = MOCK["stream_calls"]
            MOCK["model_error"] = {"error": {"code": error_code, "message": "invalid request"}}
            MOCK["error_http"] = http_error
            code, _, body = gateway_post("/v1/chat/completions", {
                "model": MODEL, "stream": False,
                "messages": [{"role": "user", "content": "hi"}],
            })
            check("%d HTTP error=%s is a single request failure" % (error_code, http_error),
                  code == 400 and str(error_code).encode() in body
                  and MOCK["stream_calls"] - count == 1, (code, body[:300]))

    P.wb_settings.set_audio_enabled(ACCOUNTS, "tts", True)
    P.wb_settings.set_audio_enabled(ACCOUNTS, "asr", True)
    status, _, body = gateway_post("/v1/audio/speech", {"input": "test", "response_format": "pcm"})
    check("a truncated TTS attempt retries without returning any partial audio",
          status == 200 and body == b"cd" and MOCK["audio_calls"] == 2, (status, body))
    for fmt, expected_status, expected_type in (
        ("text", 200, "text/plain"),
        ("json", 200, "application/json"),
        ("vtt", 400, "application/json"),
    ):
        count = MOCK["audio_calls"]
        raw = (b'--b\r\nContent-Disposition: form-data; name="file"\r\n\r\nRIFF\r\n'
               b'--b\r\nContent-Disposition: form-data; name="response_format"\r\n\r\n'
               + fmt.encode("ascii") + b'\r\n--b--\r\n')
        status, headers, body = gateway_post("/v1/audio/transcriptions", raw,
                                             "multipart/form-data; boundary=b")
        check("ASR response_format=%s" % fmt,
              status == expected_status and headers["Content-Type"].startswith(expected_type)
              and (MOCK["audio_calls"] - count == (1 if expected_status == 200 else 0))
              and (body == b"recognized" if fmt == "text" else
                   json.loads(body).get("text") == "recognized" if fmt == "json" else
                   json.loads(body)["error"]["code"] == "unsupported_audio_format"),
              (status, body[:300]))
finally:
    gateway.shutdown()
    upstream.shutdown()

print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)

