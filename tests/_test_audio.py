"""OpenAI-compatible TTS/ASR helpers and their failure boundaries.

The audio endpoints never talk to the network in this suite. They are pinned at
the translation layer because every expensive mistake happens there: writing
PCM bytes under an MP3 label, accepting an unbounded upload, treating an
upstream request error as a bad account, or returning a JSON success when the
transcript is missing.

Run with: python tests/_test_audio.py
"""
import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_audio


class TtsRequestTests(unittest.TestCase):
    def test_default_request_is_wav_and_nested_speech(self):
        body, fmt = wb_audio.normalize_tts_request({"input": " hello "})
        self.assertEqual(fmt, "wav")
        self.assertEqual(body["model"], wb_audio.TTS_DEFAULT_MODEL)
        self.assertTrue(body["stream"])
        self.assertEqual(body["speech"]["input"], "hello")
        self.assertEqual(body["speech"]["voice"], wb_audio.TTS_DEFAULT_VOICE)
        self.assertEqual(body["speech"]["response_format"], "pcm")
        self.assertEqual(body["speech"]["sample_rate"], wb_audio.TTS_SAMPLE_RATE)

    def test_pcm_is_passed_through_and_mp3_is_rejected(self):
        _body, fmt = wb_audio.normalize_tts_request({
            "input": "x", "response_format": "PCM"})
        self.assertEqual(fmt, "pcm")
        with self.assertRaises(wb_audio.AudioError) as caught:
            wb_audio.normalize_tts_request({
                "input": "x", "response_format": "mp3"})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "unsupported_audio_format")

    def test_input_must_be_a_non_empty_string(self):
        for payload in ({}, {"input": "   "}, {"input": 3}):
            with self.assertRaises(wb_audio.AudioError):
                wb_audio.normalize_tts_request(payload)


class TtsStreamTests(unittest.TestCase):
    @staticmethod
    def frame(obj):
        return ("data: %s\n\n" % json.dumps(obj)).encode("utf-8")

    def test_sse_chunks_are_concatenated_and_wrapped_in_wav(self):
        first = b"\x01\x02"
        second = b"\x03\x04"
        source = [
            self.frame({"type": "speech.audio.delta",
                        "audio": {"data": base64.b64encode(first).decode()}}),
            self.frame({"type": "speech.audio.delta",
                        "audio": {"data": base64.b64encode(second).decode()}}),
            self.frame({"type": "speech.audio.done"}),
            b"data: [DONE]\n\n",
        ]
        pcm = wb_audio.collect_tts_pcm(source)
        self.assertEqual(pcm, first + second)
        wav, content_type = wb_audio.build_tts_response(pcm, "wav")
        self.assertEqual(content_type, "audio/wav")
        self.assertEqual(len(wav), 44 + len(pcm))
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(wav[8:12], b"WAVE")
        self.assertEqual(wav[36:40], b"data")
        raw, raw_type = wb_audio.build_tts_response(pcm, "pcm")
        self.assertEqual(raw, pcm)
        self.assertEqual(raw_type, "audio/pcm")

    def test_request_error_code_is_400_not_account_failure(self):
        source = [self.frame({
            "type": "error",
            "error": {"code": 10000, "message": "invalid voice"},
        })]
        with self.assertRaises(wb_audio.AudioError) as caught:
            wb_audio.collect_tts_pcm(source)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.err_type, "invalid_request_error")

    def test_partial_audio_is_never_a_success_without_audio_done(self):
        delta = self.frame({"type": "speech.audio.delta", "audio": {"data": "YWI="}})
        for tail in ([], [b"data: [DONE]\n\n"]):
            with self.subTest(tail=tail):
                with self.assertRaises(wb_audio.AudioError) as caught:
                    wb_audio.collect_tts_pcm([delta, *tail])
                self.assertEqual(caught.exception.status, 502)
                self.assertEqual(caught.exception.code, "tts_stream_truncated")

    def test_empty_or_invalid_audio_is_502(self):
        with self.assertRaises(wb_audio.AudioError) as caught:
            wb_audio.collect_tts_pcm([b"data: [DONE]\n\n"])
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(caught.exception.code, "empty_tts_audio")
        with self.assertRaises(wb_audio.AudioError) as caught:
            wb_audio.collect_tts_pcm([self.frame({
                "type": "speech.audio.delta", "audio": {"data": "not base64!"}})])
        self.assertEqual(caught.exception.code, "invalid_tts_audio")


class AsrTests(unittest.TestCase):
    def test_multipart_uses_audio_field_and_sanitizes_filename(self):
        body, content_type = wb_audio.build_asr_multipart(
            b"RIFFdata", filename='a"b\r\n.wav', mime="audio/wav\r\nX: y")
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="audio"', body)
        self.assertIn(b'filename="a_b__.wav"', body)
        self.assertNotIn(b"\r\nX: y", body)
        field, audio = wb_audio.parse_multipart(body, content_type)
        self.assertEqual(field, "audio")
        self.assertEqual(audio, b"RIFFdata")

    def test_client_file_field_is_accepted(self):
        body = (
            b"--b\r\n"
            b'Content-Disposition: form-data; name="file"; filename="x.wav"\r\n'
            b"Content-Type: audio/wav\r\n\r\n"
            b"abc\r\n--b--\r\n"
        )
        field, audio = wb_audio.parse_multipart(
            body, "multipart/form-data; boundary=b")
        self.assertEqual(field, "file")
        self.assertEqual(audio, b"abc")

    def test_missing_boundary_and_missing_audio_are_400(self):
        for body, content_type in (
                (b"x", "multipart/form-data"),
                (b"--b\r\nContent-Disposition: form-data; name=\"other\"\r\n\r\nx\r\n--b--\r\n",
                 "multipart/form-data; boundary=b")):
            with self.assertRaises(wb_audio.AudioError) as caught:
                wb_audio.parse_multipart(body, content_type)
            self.assertEqual(caught.exception.status, 400)

    def test_response_format_after_the_file(self):
        body, content_type = wb_audio.build_asr_multipart(b"RIFFpayload")
        boundary = content_type.split("boundary=", 1)[1].encode("ascii")
        suffix = b"--" + boundary + b"--\r\n"
        option = (b"--" + boundary
                  + b'\r\nContent-Disposition: form-data; name="response_format"\r\n\r\n')
        self.assertEqual(wb_audio.parse_asr_multipart(body, content_type),
                         (b"RIFFpayload", "json"))
        self.assertEqual(wb_audio.parse_asr_multipart(
            body[:-len(suffix)] + option + b" text \r\n" + suffix, content_type),
            (b"RIFFpayload", "text"))
        with self.assertRaises(wb_audio.AudioError) as caught:
            wb_audio.parse_asr_multipart(
                body[:-len(suffix)] + option + b"srt\r\n" + suffix, content_type)
        self.assertEqual(caught.exception.status, 400)

    def test_asr_error_codes_map_to_client_errors(self):
        cases = (
            ({"code": 15052, "msg": "too large"}, 413,
             "invalid_request_error"),
            ({"code": 15053, "msg": "unsupported audio"}, 400,
             "invalid_request_error"),
            ({"code": 99999, "msg": "upstream exploded"}, 502, "server_error"),
        )
        for payload, status, err_type in cases:
            with self.assertRaises(wb_audio.AudioError) as caught:
                wb_audio.parse_asr_response(payload)
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(caught.exception.err_type, err_type)

    def test_transcript_and_empty_transcript(self):
        self.assertEqual(wb_audio.parse_asr_response(
            {"code": 0, "text": "hello"}), "hello")
        self.assertEqual(wb_audio.parse_asr_response({"text": ""}), "")
        with self.assertRaises(wb_audio.AudioError):
            wb_audio.parse_asr_response({"code": 0})

    def test_upload_limit_is_three_megabytes(self):
        self.assertEqual(wb_audio.ASR_MAX_BYTES, 3 * 1024 * 1024)
        self.assertGreater(wb_audio.ASR_REQUEST_MAX_BYTES,
                           wb_audio.ASR_MAX_BYTES)


class AudioErrorTests(unittest.TestCase):
    def test_payload_keeps_openai_shape_and_code(self):
        error = wb_audio.AudioError(413, "audio_too_large", "too large")
        self.assertEqual(error.payload(), {
            "error": {
                "message": "too large",
                "type": "invalid_request_error",
                "param": None,
                "code": "audio_too_large",
            }
        })


if __name__ == "__main__":
    unittest.main(verbosity=2)
