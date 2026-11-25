"""Pin Codex remote-compaction v2 handling in the WorkBuddy gateway.

The gateway translates Responses to Chat Completions, but Codex's native
compaction turn (`input` ends with {"type":"compaction_trigger"}) needs exactly
one `compaction` output item back. Returning ordinary text made Codex report
"got 0 compaction output items" and keep the uncompacted history, so a long
thread walked past the model's window and died on `11115 prompt is too long`.

Run with the bundled Python:

    python tests/_test_remote_compaction.py
"""
import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as proxy


def _chat_stream(text):
    chunk = {
        "id": "chatcmpl-test",
        "model": "deepseek-v4.1-flash",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    final = {
        "id": "chatcmpl-test",
        "model": "deepseek-v4.1-flash",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }
    return [
        ("data: " + json.dumps(chunk) + "\n\n").encode(),
        ("data: " + json.dumps(final) + "\n\n").encode(),
        b"data: [DONE]\n\n",
    ]


class _WFile(object):
    def __init__(self):
        self.buf = b""

    def write(self, data):
        self.buf += data

    def flush(self):
        pass


class _Stub(object):
    path = "/v1/responses"
    _responses_compaction_response = proxy.Handler._responses_compaction_response

    def __init__(self):
        self.wfile = _WFile()
        self.status = None
        self.json_body = None

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def _json(self, code, obj):
        self.status = code
        self.json_body = obj
        return code, obj

    def _error(self, code, message, err_type="server_error"):
        self.status = code
        self.json_body = {"error": {"message": message}}
        return code, message

    def _key_id(self):
        return "test-key"


class _Account(object):
    uid = "acct-test"


class TriggerTests(unittest.TestCase):
    def test_trigger_shapes(self):
        self.assertTrue(proxy._has_compaction_trigger(
            {"input": [{"type": "compaction_trigger"}]}))
        self.assertTrue(proxy._has_compaction_trigger(
            {"input": "compaction_trigger"}))
        self.assertTrue(proxy._has_compaction_trigger(
            {"input": [{"type": "message"}, {"type": "compaction_trigger"}]}))

    def test_ordinary_turns_are_not_compaction(self):
        self.assertFalse(proxy._has_compaction_trigger(
            {"input": [{"type": "message", "role": "user"}]}))
        self.assertFalse(proxy._has_compaction_trigger({"input": "hello"}))
        self.assertFalse(proxy._has_compaction_trigger({}))


class SummaryCodecTests(unittest.TestCase):
    def test_round_trip(self):
        blob = proxy._encode_compaction_summary("the summary")
        self.assertEqual("the summary", proxy._decode_compaction_summary(blob))

    def test_plain_text_is_kept(self):
        # A foreign provider may return the summary unencoded; dropping it would
        # lose the only copy of the compacted context.
        self.assertEqual("plain text", proxy._decode_compaction_summary("plain text"))

    def test_empty(self):
        self.assertEqual("", proxy._decode_compaction_summary(""))
        self.assertEqual("", proxy._decode_compaction_summary(None))

    def test_opaque_ciphertext_is_skipped_and_plain_summary_is_bounded(self):
        opaque = "gAAAAA" + ("a9O-" * 30)
        self.assertEqual("", proxy._decode_compaction_summary(opaque))
        self.assertEqual("", proxy._decode_compaction_summary("a9O-" * 30))
        text = "\u4f60" * proxy.COMPACTION_SUMMARY_MAX_BYTES
        raw = proxy._encode_compaction_summary(text)
        for value in (text, raw):
            summary = proxy._decode_compaction_summary(value)
            self.assertEqual(summary, text[:proxy.COMPACTION_SUMMARY_MAX_BYTES // 3])


class RequestTranslationTests(unittest.TestCase):
    def _payload(self, extra=None):
        payload = {
            "model": "deepseek-v4.1-flash",
            "input": [
                {"type": "message", "role": "user", "content": "do the thing"},
                {"type": "message", "role": "assistant", "content": "done"},
                {"type": "compaction_trigger"},
            ],
            "tools": [{"type": "function", "name": "exec",
                       "parameters": {"type": "object"}}],
        }
        payload.update(extra or {})
        return payload

    def test_compaction_turn_adds_the_summary_prompt(self):
        chat = proxy.responses_to_chat(self._payload())
        last = chat["messages"][-1]
        self.assertEqual("user", last["role"])
        self.assertIn("CONTEXT CHECKPOINT COMPACTION", last["content"])

    def test_compaction_turn_withholds_tools(self):
        chat = proxy.responses_to_chat(self._payload())
        self.assertNotIn("tools", chat)
        self.assertNotIn("tool_choice", chat)
        self.assertNotIn("parallel_tool_calls", chat)

    def test_trigger_item_is_not_turned_into_a_message(self):
        chat = proxy.responses_to_chat(self._payload())
        for message in chat["messages"]:
            self.assertNotIn("compaction_trigger", str(message.get("content")))

    def test_client_supplied_summary_prompt_is_not_duplicated(self):
        payload = self._payload()
        payload["input"] = payload["input"][:-1] + [
            {"type": "message", "role": "user", "content": proxy.COMPACTION_PROMPT},
            {"type": "compaction_trigger"},
        ]
        chat = proxy.responses_to_chat(payload)
        prompts = [m for m in chat["messages"]
                   if "CONTEXT CHECKPOINT COMPACTION" in str(m.get("content"))]
        self.assertEqual(1, len(prompts))

    def test_replayed_compaction_item_becomes_context(self):
        blob = proxy._encode_compaction_summary("earlier work summary")
        chat = proxy.responses_to_chat({
            "model": "deepseek-v4.1-flash",
            "input": [
                {"type": "compaction", "id": "cmp_1", "encrypted_content": blob},
                {"type": "message", "role": "user", "content": "continue"},
            ],
        })
        self.assertEqual("system", chat["messages"][0]["role"])
        self.assertIn("earlier work summary", chat["messages"][0]["content"])
        self.assertEqual("continue", chat["messages"][1]["content"])

    def test_commentary_reasoning_and_parallel_calls_share_one_message(self):
        chat = proxy.responses_to_chat({"model": "m", "input": [
            {"role": "assistant", "phase": "commentary", "content": "Checking",
             "reasoning_content": "Already decided"},
            {"type": "web_search_call", "id": "receipt"},
            {"type": "reasoning", "summary": "Need tools"},
            {"type": "function_call", "call_id": "one", "name": "read"},
            {"type": "custom_tool_call", "call_id": "two", "name": "custom"},
            {"role": "tool", "tool_call_id": "one", "content": ""},
            {"type": "custom_tool_call_output", "call_id": "two", "output": "ok"},
        ]})
        assistant, *replies = chat["messages"]
        self.assertEqual(assistant["content"], "Checking")
        self.assertEqual(assistant["reasoning_content"], "Already decided\nNeed tools")
        self.assertEqual([call["id"] for call in assistant["tool_calls"]], ["one", "two"])
        self.assertEqual([reply["tool_call_id"] for reply in replies], ["one", "two"])

    def test_replay_keeps_final_answers_and_opaque_checkpoint_boundaries(self):
        answer = {"role": "assistant", "phase": "final_answer", "content": "Done"}
        calls = [
            {"type": "function_call", "call_id": "one", "name": "read"},
            {"type": "function_call_output", "call_id": "one", "output": "ok"},
        ]
        cases = ([answer, *calls], [*calls, answer], [
            {"role": "assistant", "phase": "commentary", "content": "Done"},
            {"type": "compaction", "encrypted_content": "gAAAAA" + "abcd" * 30},
            *calls,
        ])
        for items in cases:
            with self.subTest(items=items):
                chat = proxy.responses_to_chat({"model": "m", "input": items})
                for build in (proxy.build_upstream_body, proxy.wb_codearts.build_chat_body):
                    body = build(chat)
                    assistant = [m for m in body["messages"] if m["role"] == "assistant"]
                    self.assertEqual(len(assistant), 2)
                    spoken = next(m for m in assistant if m["content"] == "Done")
                    self.assertNotIn("tool_calls", spoken)
                    self.assertNotIn("_wb_replay_boundary", json.dumps(body))
                    self.assertNotIn("gAAAAA", json.dumps(body))

    def test_ordinary_turn_keeps_its_tools(self):
        payload = self._payload()
        payload["input"] = payload["input"][:-1]
        chat = proxy.responses_to_chat(payload)
        self.assertIn("tools", chat)


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self._record_upstream_usage = proxy.record_upstream_usage
        proxy.record_upstream_usage = lambda *a, **k: None

    def tearDown(self):
        proxy.record_upstream_usage = self._record_upstream_usage

    def _run(self, text, stream):
        stub = _Stub()
        stub._responses_compaction_response(
            _chat_stream(text), "deepseek-v4.1-flash", "fp",
            _Account(), 0.0, stream)
        return stub

    def _items(self, body):
        found = []
        for line in body.splitlines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            item = event.get("item")
            if isinstance(item, dict):
                found.append(item)
        return found

    def test_stream_emits_exactly_one_compaction_item(self):
        stub = self._run("handoff summary", True)
        self.assertEqual(200, stub.status)
        body = stub.wfile.buf.decode()
        self.assertIn("response.output_item.done", body)
        self.assertIn("response.completed", body)

        items = [i for i in self._items(body) if i.get("type") == "compaction"]
        self.assertEqual(1, len(items))
        self.assertIn("handoff summary",
                      proxy._decode_compaction_summary(items[0]["encrypted_content"]))

    def test_stream_item_carries_a_real_summary(self):
        stub = self._run("real summary", True)
        items = [i for i in self._items(stub.wfile.buf.decode())
                 if i.get("type") == "compaction"]
        self.assertEqual("real summary",
                         proxy._decode_compaction_summary(items[0]["encrypted_content"]))

    def test_nonstream_returns_the_item(self):
        stub = self._run("nonstream summary", False)
        self.assertEqual(200, stub.status)
        output = stub.json_body.get("output") or []
        self.assertEqual(["compaction"], [o.get("type") for o in output])
        self.assertEqual("nonstream summary",
                         proxy._decode_compaction_summary(output[0]["encrypted_content"]))

    def test_empty_summary_is_an_error_not_an_empty_item(self):
        proxy.record_error = lambda *a, **k: None
        stub = self._run("   ", False)
        self.assertEqual(502, stub.status)
        self.assertIn("no summary", stub.json_body["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
