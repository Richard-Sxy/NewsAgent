"""Synthetic protocol checks only; no GPU, model calls or quality measurements."""
from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


SERVICE_PATH = Path(__file__).resolve().parents[2] / "Hunyuan7B/scripts/serve_writing_eval.py"


def load_service():
    spec = importlib.util.spec_from_file_location("newsagent_writing_eval_offline", SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".", 1)[0] in {"torch", "transformers", "assets"}:
            raise AssertionError("GPU/model dependency imported during offline protocol checks")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=guarded_import):
        spec.loader.exec_module(module)
    return module


SERVICE = load_service()
OUTPUT_LIMIT = 3072


def request(**updates):
    body = {"model": SERVICE.MODEL_ID, "messages": [{"role": "user", "content": "synthetic request"}]}
    body.update(updates)
    return body


class HunyuanWritingProtocolTests(unittest.TestCase):
    def test_import_and_protocol_functions_do_not_load_gpu(self):
        module = load_service()
        self.assertEqual(module.checked_request(request(), OUTPUT_LIMIT)[1], OUTPUT_LIMIT)
        self.assertEqual(module.unwrap("<answer>synthetic</answer>"), "synthetic")

    def test_frozen_model_identity(self):
        self.assertEqual(SERVICE.MODEL_ID, "local-hunyuan-7b-6fd6ecb-nf4-writing")
        self.assertEqual(SERVICE.REVISION, "6fd6ecb05e76589bc43b79f49e3619445c6b4593")
        self.assertEqual(SERVICE.MAX_BODY, 2 * 1024 * 1024)

    def test_supported_roles_and_default_output_budget(self):
        messages = [
            {"role": "system", "content": "synthetic protocol"},
            {"role": "user", "content": "synthetic evidence"},
            {"role": "assistant", "content": "synthetic previous answer"},
        ]
        actual, cap = SERVICE.checked_request(request(messages=messages), OUTPUT_LIMIT)
        self.assertEqual(actual, messages)
        self.assertEqual(cap, OUTPUT_LIMIT)

    def test_request_object_and_exact_model_are_required(self):
        for body in (None, [], "", {}, request(model=None), request(model="other-model")):
            with self.subTest(body_type=type(body).__name__), self.assertRaises(ValueError):
                SERVICE.checked_request(body, OUTPUT_LIMIT)

    def test_streaming_must_be_explicit_false_or_absent(self):
        SERVICE.checked_request(request(stream=False), OUTPUT_LIMIT)
        for stream in (True, 0, 1, None, "false"):
            with self.subTest(stream=stream), self.assertRaises(ValueError):
                SERVICE.checked_request(request(stream=stream), OUTPUT_LIMIT)

    def test_constrained_json_and_unknown_response_formats_are_rejected(self):
        for format_ in (None, {"type": "text"}):
            SERVICE.checked_request(request(response_format=format_), OUTPUT_LIMIT)
        for format_ in ({"type": "json_object"}, {"type": "json_schema"}, "json", {},
                        {"type": "text", "schema": {}}, []):
            with self.subTest(format=format_), self.assertRaises(ValueError):
                SERVICE.checked_request(request(response_format=format_), OUTPUT_LIMIT)

    def test_output_token_boundaries_are_inclusive(self):
        for cap in (1, OUTPUT_LIMIT):
            _, actual = SERVICE.checked_request(request(max_tokens=cap), OUTPUT_LIMIT)
            self.assertEqual(actual, cap)
        for cap in (0, -1, OUTPUT_LIMIT + 1, True, False, 1.0, "1", None):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                SERVICE.checked_request(request(max_tokens=cap), OUTPUT_LIMIT)

    def test_temperature_is_finite_numeric_zero(self):
        for value in (0, 0.0, -0.0):
            SERVICE.checked_request(request(temperature=value), OUTPUT_LIMIT)
        for value in (0.1, -0.1, float("nan"), float("inf"), float("-inf"),
                      True, False, None, "0"):
            with self.subTest(temperature=value), self.assertRaises(ValueError):
                SERVICE.checked_request(request(temperature=value), OUTPUT_LIMIT)

    def test_message_count_and_container_bounds(self):
        valid = [{"role": "user", "content": "x"} for _ in range(64)]
        self.assertEqual(len(SERVICE.checked_request(request(messages=valid), OUTPUT_LIMIT)[0]), 64)
        for messages in (None, {}, "not-a-list", [], valid + [valid[0]]):
            with self.subTest(message_type=type(messages).__name__), self.assertRaises(ValueError):
                SERVICE.checked_request(request(messages=messages), OUTPUT_LIMIT)

    def test_roles_fields_and_nonempty_string_content(self):
        invalid_messages = [
            None, "text", {}, {"role": "user"}, {"content": "x"},
            {"role": "tool", "content": "x"}, {"role": "function", "content": "x"},
            {"role": None, "content": "x"}, {"role": "user", "content": ""},
            {"role": "user", "content": None}, {"role": "user", "content": ["x"]},
            {"role": "user", "content": "x", "name": "unsupported"},
        ]
        for message in invalid_messages:
            with self.subTest(message=message), self.assertRaises(ValueError):
                SERVICE.checked_request(request(messages=[message]), OUTPUT_LIMIT)

    def test_aggregate_input_character_limit_includes_unicode(self):
        allowed = [{"role": "user", "content": "文" * 200_000}]
        SERVICE.checked_request(request(messages=allowed), OUTPUT_LIMIT)
        too_long = [
            {"role": "system", "content": "a" * 100_000},
            {"role": "user", "content": "文" * 100_001},
        ]
        with self.assertRaises(ValueError):
            SERVICE.checked_request(request(messages=too_long), OUTPUT_LIMIT)

    def test_official_think_answer_wrappers_preserve_answer_only(self):
        wrapped = '  <think>synthetic reasoning must not leak</think>\n<answer> {"ok": true} </answer>ignored  '
        self.assertEqual(SERVICE.unwrap(wrapped), '{"ok": true}')
        self.assertEqual(SERVICE.unwrap("<answer>synthetic answer</answer>"), "synthetic answer")

    def test_plain_json_and_surrounding_whitespace_are_preserved(self):
        self.assertEqual(SERVICE.unwrap(' \n{"synthetic": 1}\n '), '{"synthetic": 1}')

    def test_last_complete_thinking_boundary_is_used(self):
        text = '<think>one</think>discard<think>two</think><answer>final</answer>'
        self.assertEqual(SERVICE.unwrap(text), "final")

    def test_unfinished_thinking_never_becomes_an_answer(self):
        for text in ("<think>", "<think>synthetic reasoning", "prefix <think>incomplete"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                SERVICE.unwrap(text)


if __name__ == "__main__":
    unittest.main()
