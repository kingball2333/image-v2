"""新版模型路由和页面交互回归测试；所有 HTTP 请求均被替换，不产生费用。"""

import base64
from io import BytesIO
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from streamlit.testing.v1 import AppTest


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from clipboard_image import ClipboardImageFile
from rightapi_models import (
    GEMINI_MODELS, LEGACY_MODELS, MODEL_OPTIONS,
    build_image_request, get_quality_options, match_gemini_aspect_ratio,
)


class ModelAdapterTests(unittest.TestCase):
    def test_model_quality_limits(self):
        self.assertEqual(set(get_quality_options("gpt-image-2").values()),
                         {"auto", "low", "medium", "high"})
        for model in ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst"):
            self.assertIn("xhigh", get_quality_options(model).values())
            self.assertIn("max", get_quality_options(model).values())
        with self.assertRaises(ValueError):
            build_image_request("gpt-image-2", "test", "1024x1024", quality="max")

    def test_gemini_matching_and_image_encoding(self):
        for width, height, expected in ((1600, 900, "16:9"), (900, 1600, "9:16"),
                                        (800, 1200, "2:3"), (400, 400, "1:1")):
            self.assertEqual(match_gemini_aspect_ratio(width, height), expected)
        references = ["data:image/jpeg;base64,QUJD", "data:image/png;base64,REVG"]
        _, payload = build_image_request("gemini-nano-banana-2.1", "test", "16:9",
                                         references, image_size="2K")
        self.assertEqual(payload["contents"][0]["parts"][1:], [
            {"inline_data": {"mime_type": "image/jpeg", "data": "QUJD"}},
            {"inline_data": {"mime_type": "image/png", "data": "REVG"}},
        ])
        self.assertNotIn("model", payload)
        self.assertNotIn("quality", payload)
        with self.assertRaises(ValueError):
            build_image_request("gemini-nano-banana-2.1", "test", "1024x1024")

    def test_base_url_overrides_and_legacy_payload(self):
        with patch.dict(os.environ, {
            "RIGHTAPI_DRAW_BASE_URL": "https://legacy.example/draw/",
            "RIGHTAPI_IMAGE_PRO_BASE_URL": "https://new.example/",
        }):
            url, payload = build_image_request("gpt-image-2-vip", "test", "1024x1024")
            self.assertEqual(url, "https://legacy.example/draw/v1/images/generations")
            self.assertNotIn("quality", payload)
            self.assertNotIn("image", payload)
            url, _ = build_image_request("gpt-image-2.5-flare", "test", "1024x1024",
                                         ["data:image/png;base64,QUJD"])
            self.assertEqual(url, "https://new.example/v1/images/edits")


class ImagePageTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "MY_API_KEY": "test-key", "COMPANY_API_KEY": "test-company-key",
            "RIGHTAPI_DRAW_BASE_URL": "https://www.rightapi.ai/draw",
            "RIGHTAPI_IMAGE_PRO_BASE_URL": "https://www.rightapi.ai/image-pro",
            "RIGHTAPI_TASKS_BASE_URL": "https://www.rightapi.ai/v1/tasks",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        reference = BytesIO()
        Image.new("RGB", (800, 1200), "blue").save(reference, "PNG")
        self.reference_bytes = reference.getvalue()
        output = BytesIO()
        Image.new("RGB", (320, 240), "yellow").save(output, "JPEG")
        self.output_bytes = output.getvalue()

    @staticmethod
    def response(data):
        result = Mock(ok=True, status_code=200)
        result.json.return_value = data
        return result

    def run_app(self, app):
        app.run(timeout=15)
        self.assertFalse(app.exception, [item.value for item in app.exception])
        return app

    def test_all_models_text_edit_and_page_switch(self):
        app = self.run_app(AppTest.from_file(str(PROJECT / "app.py")))
        self.assertEqual(app.selectbox(key="old_image_model").value, "GPT Image 2 VIP（原模型）")
        app.session_state["old_edit_images"] = [
            ClipboardImageFile(self.reference_bytes, "first.png", "image/png", "1"),
            ClipboardImageFile(self.output_bytes, "second.jpg", "image/jpeg", "2"),
        ]
        self.run_app(app)
        image_url = "https://images.example/result.jpg"
        current_model = None

        def get(url, **kwargs):
            if "/v1/tasks/" in url:
                if current_model in GEMINI_MODELS:
                    return self.response({"candidates": [{"content": {"parts": [
                        {"text": "附加说明"}, {"text": image_url},
                    ]}}]})
                return self.response({"data": [{"url": image_url}]})
            self.assertEqual(url, image_url)
            result = Mock(content=self.output_bytes)
            return result

        with patch("requests.post", return_value=self.response({"task_id": "test-task", "status": "processing"})) as post, \
                patch("requests.get", side_effect=get), patch("time.sleep"):
            for label, current_model in MODEL_OPTIONS.items():
                with self.subTest(model=current_model):
                    app.selectbox(key="old_image_model").select(label)
                    self.run_app(app)
                    qualities = get_quality_options(current_model)
                    if qualities:
                        quality_key = f"old_quality_{current_model}"
                        app.selectbox(key=quality_key).select(list(qualities)[-1])
                        self.run_app(app)
                    if current_model in GEMINI_MODELS:
                        app.selectbox(key="old_gemini_resolution").select("2K")
                        self.run_app(app)
                    for edit in (False, True):
                        field = app.text_input[0] if edit else app.text_area[0]
                        field.set_value("测试重绘" if edit else "测试文生图")
                        button_label = "开始重绘" if edit else "生成图片"
                        next(item for item in app.button if item.label == button_label).click()
                        self.run_app(app)
                        self.assertFalse(app.error, [item.value for item in app.error])
                        url = post.call_args.args[0]
                        payload = post.call_args.kwargs["json"]
                        self.assertIs(payload["async"], True)
                        if current_model in GEMINI_MODELS:
                            self.assertEqual(url, f"https://www.rightapi.ai/image-pro/v1beta/models/{current_model}:generateContent")
                            config = payload["generationConfig"]["imageConfig"]
                            self.assertEqual(config, {"aspectRatio": "2:3" if edit else "1:1", "imageSize": "2K"})
                            parts = payload["contents"][0]["parts"]
                            self.assertEqual(len(parts), 3 if edit else 1)
                            if edit:
                                self.assertTrue(parts[1]["inline_data"]["data"])
                                self.assertNotIn("data:image", parts[1]["inline_data"]["data"])
                        else:
                            self.assertEqual(payload["model"], current_model)
                            prefix = "draw" if current_model in LEGACY_MODELS else "image-pro"
                            path = "edits" if edit and current_model not in LEGACY_MODELS else "generations"
                            self.assertEqual(url, f"https://www.rightapi.ai/{prefix}/v1/images/{path}")
                            self.assertEqual(payload["size"], "1024x1536" if edit else "1024x1024")
                            if edit:
                                self.assertEqual(len(payload["image"]), 2)
                            if qualities:
                                self.assertEqual(payload["quality"], list(qualities.values())[-1])
                        self.assertIn("320×240", app.success[0].value)
            self.assertEqual(post.call_count, 2 * len(MODEL_OPTIONS))
        next(item for item in app.button if item.label == "公司生图").click()
        self.run_app(app)
        self.assertEqual(app.query_params["page"], ["company"])
        self.assertFalse(any(item.key == "old_image_model" for item in app.selectbox))
        app.radio[0].set_value("图片重绘")
        self.run_app(app)
        next(item for item in app.button if item.label == "旧中转生图").click()
        self.run_app(app)
        self.assertTrue(any(item.key == "old_image_model" for item in app.selectbox))

    def test_terminal_statuses_stop_without_retry(self):
        app = self.run_app(AppTest.from_file(str(PROJECT / "app.py")))
        for status in ("failed", "expired", "canceled"):
            with self.subTest(status=status), \
                    patch("requests.post", return_value=self.response({"task_id": "t", "status": status, "error": "test failure"})), \
                    patch("requests.get") as get:
                app.text_area[0].set_value("test")
                next(item for item in app.button if item.label == "生成图片").click()
                self.run_app(app)
                self.assertIn("test failure", app.error[0].value)
                get.assert_not_called()
        with patch("requests.post", return_value=self.response({"task_id": "t", "status": "processing"})), \
                patch("requests.get", return_value=self.response({"status": "canceled", "error": {"message": "poll failed"}})) as get, \
                patch("time.sleep"):
            next(item for item in app.button if item.label == "生成图片").click()
            self.run_app(app)
            self.assertIn("poll failed", app.error[0].value)
            self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
