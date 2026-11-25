"""WorkBuddy audio endpoints (TTS and ASR) for the OpenAI-compatible gateway.

The upstream speaks its own request shapes, and both endpoints have failure
modes that must not be treated as account failures:

* TTS streams base64 PCM in SSE frames. An unknown voice is a request error.
* ASR rejects naked PCM with 15053. That is a request error, not a bad account.

Keeping those rules here makes them testable without starting the HTTP server.
Python 3.9 and the standard library only.
"""
import base64
import json
import struct


TTS_PATH = "/v2/audio/speech"
ASR_PATH = "/agenttool/v1/asr"

TTS_DEFAULT_MODEL = "hunyuan-tts-2.5"
TTS_DEFAULT_VOICE = "f191_female_cn_tts2.5_flash"
TTS_SAMPLE_RATE = 24000
TTS_CHANNELS = 1
TTS_SAMPLE_WIDTH = 2

ASR_MAX_BYTES = 3 * 1024 * 1024
# Multipart framing, headers and a little slack. The raw request body is
# bounded before parsing so a 3 MB recording cannot turn into a 100 MB upload.
ASR_REQUEST_MAX_BYTES = ASR_MAX_BYTES + 64 * 1024
ASR_FIELD_NAMES = ("file", "audio")

REQUEST_ERROR_CODES = frozenset((10000, 15053))


class AudioError(Exception):
    """A client-visible audio failure with an OpenAI error type."""

    def __init__(self, status, code, message, err_type="invalid_request_error",
                 detail=""):
        super(AudioError, self).__init__(message)
        self.status = int(status)
        self.code = str(code or "audio_error")
        self.message = str(message or code or "audio request failed")
        self.err_type = err_type
        self.detail = str(detail or "")

    def payload(self):
        return {
            "error": {
                "message": self.message,
                "type": self.err_type,
                "param": None,
                "code": self.code,
            }
        }


def _clean_text(value, field, required=True):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise AudioError(400, "invalid_" + field, "%s must be a string" % field)
    value = value.strip()
    if required and not value:
        raise AudioError(400, "invalid_" + field, "%s is required" % field)
    return value


def normalize_tts_request(payload):
    """Validate the OpenAI speech request and return the upstream body.

    OpenAI clients commonly send ``model=tts-1`` and a voice name. The gateway
    keeps that public surface, but the upstream expects its own TTS model and a
    nested ``speech`` object. Only WAV and raw PCM are supported: returning an
    MP3-labelled PCM stream would produce a file every player rejects.
    """
    if not isinstance(payload, dict):
        raise AudioError(400, "invalid_body", "request body must be an object")
    text = _clean_text(payload.get("input"), "input")
    voice = _clean_text(payload.get("voice") or TTS_DEFAULT_VOICE, "voice")
    fmt = _clean_text(payload.get("response_format") or "wav",
                      "response_format", required=False).lower()
    if fmt not in ("wav", "pcm"):
        raise AudioError(
            400, "unsupported_audio_format",
            "response_format must be wav or pcm; the upstream does not return mp3")
    body = {
        "model": TTS_DEFAULT_MODEL,
        "stream": True,
        "speech": {
            "input": text,
            "voice": voice,
            "response_format": "pcm",
            "sample_rate": TTS_SAMPLE_RATE,
        },
    }
    return body, fmt


def tts_headers(account, exact_headers=None):
    """Headers for the TTS endpoint.

    ``Accept: text/event-stream`` must be applied after ``account.headers()``:
    that helper sets the JSON Accept value and would otherwise overwrite it.
    """
    headers = account.headers("chat")
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "text/event-stream"
    if exact_headers is not None:
        exact_headers[:] = list(headers.items())
    return headers


def iter_sse_json(source):
    """Yield decoded JSON payloads from an SSE byte stream.

    Comments and malformed frames are skipped. ``[DONE]`` ends the iterator.
    The function accepts any iterable of byte lines, which keeps it usable with
    both the real HTTP response and test doubles.
    """
    for raw in source:
        if isinstance(raw, bytes):
            line = raw.decode("utf-8", "replace")
        else:
            line = str(raw)
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line:
            continue
        if line == "[DONE]":
            return
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def upstream_error_message(obj):
    """Extract a useful message and code from an upstream error object."""
    error = obj.get("error") if isinstance(obj, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or error.get("msg") or ""
        code = error.get("code")
        return str(message), code
    if isinstance(obj, dict):
        message = obj.get("message") or obj.get("msg") or ""
        return str(message), obj.get("code")
    return "", None


def is_request_error(code, message=""):
    """Whether an upstream failure is request-scoped.

    These codes mean the request itself is wrong (unknown voice, unrecognised
    audio), so rotating accounts only amplifies the failure. Text matching is
    a fallback for upstreams that omit the numeric code.
    """
    try:
        number = int(code)
    except (TypeError, ValueError):
        number = None
    if number in REQUEST_ERROR_CODES:
        return True
    lower = str(message or "").lower()
    return ("invalid voice" in lower or "voice not found" in lower
            or "asr recognition failed" in lower
            or "unsupported audio" in lower)


def pcm_to_wav(pcm, sample_rate=TTS_SAMPLE_RATE, channels=TTS_CHANNELS,
               sample_width=TTS_SAMPLE_WIDTH):
    """Wrap signed little-endian PCM in a RIFF/WAVE container."""
    pcm = bytes(pcm or b"")
    block_align = int(channels) * int(sample_width)
    byte_rate = int(sample_rate) * block_align
    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,                  # PCM
        int(channels),
        int(sample_rate),
        byte_rate,
        block_align,
        int(sample_width) * 8,
        b"data",
        data_size,
    )
    return header + pcm


def collect_tts_pcm(source):
    """Read the upstream SSE stream and return the concatenated PCM bytes."""
    chunks = []
    done = False
    for obj in iter_sse_json(source):
        message, code = upstream_error_message(obj)
        if obj.get("type") == "error" or obj.get("error"):
            raise AudioError(
                502 if not is_request_error(code, message) else 400,
                code or "tts_upstream_error",
                message or "TTS upstream returned an error",
                "server_error" if not is_request_error(code, message)
                else "invalid_request_error",
            )
        event_type = obj.get("type")
        if event_type not in ("speech.audio.delta", "speech.audio.done"):
            continue
        data = obj.get("audio", {}).get("data") if isinstance(obj.get("audio"), dict) else None
        if not data and isinstance(obj.get("data"), dict):
            data = obj["data"].get("audio")
        if not data:
            data = obj.get("audio")
        if isinstance(data, str) and data:
            try:
                chunks.append(base64.b64decode(data, validate=True))
            except Exception:
                raise AudioError(502, "invalid_tts_audio",
                                 "TTS upstream returned invalid base64 audio",
                                 "server_error")
        if event_type == "speech.audio.done":
            done = True
            break
    pcm = b"".join(chunks)
    if not pcm:
        raise AudioError(502, "empty_tts_audio",
                         "TTS upstream returned no audio data",
                         "server_error")
    if not done:
        raise AudioError(502, "tts_stream_truncated",
                         "TTS upstream closed before speech.audio.done",
                         "server_error")
    return pcm


def build_tts_response(pcm, fmt):
    """Return the response bytes and Content-Type for the requested format."""
    if fmt == "wav":
        return pcm_to_wav(pcm), "audio/wav"
    return bytes(pcm or b""), "audio/pcm"


def _multipart_parts(body, content_type):
    """Yield form fields from the request body already capped by the caller."""
    text = str(content_type or "")
    marker = "boundary="
    pos = text.lower().find(marker)
    if pos < 0:
        raise AudioError(400, "invalid_multipart", "multipart boundary is missing")
    boundary = text[pos + len(marker):].strip().strip('"')
    if not boundary:
        raise AudioError(400, "invalid_multipart", "multipart boundary is empty")
    separator = b"--" + boundary.encode("latin-1", "replace")
    raw = bytes(body or b"")
    for part in raw.split(separator):
        # Only the framing CRLF belongs to multipart. Stripping the part
        # itself would delete real leading/trailing CR/LF bytes from an audio
        # file and corrupt formats whose first sample happens to be 0x0d/0x0a.
        if part.startswith(b"\r\n"):
            part = part[2:]
        elif part.startswith(b"\n"):
            part = part[1:]
        if part.startswith(b"--"):
            continue
        if part.endswith(b"\r\n"):
            part = part[:-2]
        elif part.endswith(b"\n"):
            part = part[:-1]
        if not part or b"\r\n\r\n" not in part:
            continue
        head, data = part.split(b"\r\n\r\n", 1)
        headers = {}
        for line in head.split(b"\r\n"):
            if b":" not in line:
                continue
            name, value = line.split(b":", 1)
            headers[name.strip().lower()] = value.strip()
        disposition = headers.get(b"content-disposition", b"").decode(
            "latin-1", "replace")
        field = ""
        for piece in disposition.split(";"):
            piece = piece.strip()
            if piece.lower().startswith("name="):
                field = piece[5:].strip().strip('"')
                break
        if field:
            yield field, data


def parse_multipart(body, content_type):
    """Return the first audio file part."""
    for field, data in _multipart_parts(body, content_type):
        if field in ASR_FIELD_NAMES:
            return field, data
    raise AudioError(400, "missing_audio", "multipart field file or audio is required")


def parse_asr_multipart(body, content_type):
    """Return the audio and the local response format; never forward form options."""
    audio = None
    response_format = "json"
    for field, data in _multipart_parts(body, content_type):
        if field in ASR_FIELD_NAMES and audio is None:
            audio = data
        elif field == "response_format":
            response_format = data.decode("utf-8", "replace").strip().lower() or "json"
            if response_format not in ("json", "text"):
                raise AudioError(400, "unsupported_audio_format",
                                 "response_format must be json or text")
    if audio is None:
        raise AudioError(400, "missing_audio", "multipart field file or audio is required")
    return audio, response_format


def build_asr_multipart(audio, filename="audio.wav", mime="audio/wav"):
    """Build the upstream multipart body, always using the field name ``audio``."""
    boundary = "----wb-audio-%s" % base64.urlsafe_b64encode(
        str(len(audio)).encode("ascii")).decode("ascii").rstrip("=")
    safe_name = str(filename or "audio.wav").replace('"', "_").replace("\r", "_").replace("\n", "_")
    safe_mime = str(mime or "application/octet-stream").replace("\r", "").replace("\n", "")
    head = (
        "--%s\r\n"
        'Content-Disposition: form-data; name="audio"; filename="%s"\r\n'
        "Content-Type: %s\r\n\r\n"
        % (boundary, safe_name, safe_mime)
    ).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    return head + bytes(audio or b"") + tail, "multipart/form-data; boundary=%s" % boundary


def parse_asr_response(payload):
    """Return the transcript from the upstream ASR JSON response."""
    if not isinstance(payload, dict):
        raise AudioError(502, "invalid_asr_response",
                         "ASR upstream returned a non-object response",
                         "server_error")
    code = payload.get("code")
    text = payload.get("text")
    if code not in (None, 0, "0") or not isinstance(text, str):
        message = payload.get("msg") or payload.get("message") or "ASR recognition failed"
        if str(code) == "15052":
            status = 413
        else:
            status = 400 if is_request_error(code, message) else 502
        raise AudioError(status, code or "asr_error", str(message),
                         "invalid_request_error" if status in (400, 413)
                         else "server_error")
    return text
