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
        environment = {k: v for k, v in os.environ.items() if k != "RIGHTAPI_IMAGE_PRO_BASE_URL"}
        with patch.dict(os.environ, environment, clear=True):
            url, _ = build_image_request("gpt-image-2.5-flare", "test", "1024x1024")
            self.assertEqual(url, "https://www.rightapi.ai/draw-pro/v1/images/generations")
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
            "RIGHTAPI_IMAGE_PRO_BASE_URL": "https://www.rightapi.ai/draw-pro",
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
                            self.assertEqual(url, f"https://www.rightapi.ai/draw-pro/v1beta/models/{current_model}:generateContent")
                            config = payload["generationConfig"]["imageConfig"]
                            self.assertEqual(config, {"aspectRatio": "2:3" if edit else "1:1", "imageSize": "2K"})
                            parts = payload["contents"][0]["parts"]
                            self.assertEqual(len(parts), 3 if edit else 1)
                            if edit:
                                self.assertTrue(parts[1]["inline_data"]["data"])
                                self.assertNotIn("data:image", parts[1]["inline_data"]["data"])
                        else:
                            self.assertEqual(payload["model"], current_model)
                            prefix = "draw" if current_model in LEGACY_MODELS else "draw-pro"
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

    @staticmethod
    def colored_image(color):
        output = BytesIO()
        Image.new("RGB", (1024, 1024), color).save(output, "PNG")
        return output.getvalue()

    def assert_reference_color(self, data_url, color):
        raw = base64.b64decode(data_url.split(",", 1)[-1])
        with Image.open(BytesIO(raw)) as image:
            self.assertEqual(image.size, (1024, 1024))
            actual = image.convert("RGB").getpixel((0, 0))
            for channel, expected in zip(actual, color):
                self.assertLessEqual(abs(channel - expected), 2)

    def test_old_refinement_chains_latest_image_and_isolates_inputs(self):
        for model, label in (("gpt-image-2.5-flare", "GPT Image 2.5 Flare（速度优先）"),
                             ("gemini-nano-banana-2.1", "Nano Banana 2.1（速度优先）")):
            with self.subTest(model=model):
                app = self.run_app(AppTest.from_file(str(PROJECT / "app.py")))
                app.selectbox(key="old_image_model").select(label)
                self.run_app(app)
                colors = ((220, 10, 10), (10, 220, 10), (10, 10, 220))
                images = [self.colored_image(color) for color in colors]
                responses = [self.response({"data": [{"b64_json": base64.b64encode(img).decode()}]})
                             for img in images]
                with patch("requests.post", side_effect=responses) as post, patch("requests.get") as get:
                    app.text_area[0].set_value("initial")
                    next(item for item in app.button if item.label == "生成图片").click()
                    self.run_app(app)
                    initial = app.session_state["old_results"][0]
                    self.run_app(app)
                    self.assertEqual(post.call_count, 1)
                    self.assertEqual(app.session_state["old_results"][0]["id"], initial["id"])
                    prefix = f"old_refine_{initial['id']}"
                    app.session_state[f"{prefix}_images"] = [
                        ClipboardImageFile(self.reference_bytes, "extra.png", "image/png", "extra")
                    ]
                    self.run_app(app)
                    app.text_area(key=f"{prefix}_instruction").set_value("change background")
                    app.button(key=f"{prefix}_submit").click()
                    self.run_app(app)
                    second = app.session_state["old_results"][0]
                    self.assertEqual(second["parent_id"], initial["id"])
                    self.assertEqual(second["image_bytes"], images[1])
                    payload = post.call_args.kwargs["json"]
                    if model in GEMINI_MODELS:
                        parts = payload["contents"][0]["parts"]
                        self.assertEqual(len(parts), 3)
                        self.assert_reference_color(parts[1]["inline_data"]["data"], colors[0])
                    else:
                        self.assertTrue(post.call_args.args[0].endswith("/images/edits"))
                        self.assertEqual(len(payload["image"]), 2)
                        self.assertEqual(payload["size"], "1024x1024")
                        self.assert_reference_color(payload["image"][0], colors[0])
                        self.assertIn("change background", payload["prompt"])
                    prefix = f"old_refine_{second['id']}"
                    self.assertEqual(app.text_area(key=f"{prefix}_instruction").value, "")
                    app.text_area(key=f"{prefix}_instruction").set_value("more detail")
                    app.button(key=f"{prefix}_submit").click()
                    self.run_app(app)
                    third = app.session_state["old_results"][0]
                    self.assertEqual(third["parent_id"], second["id"])
                    self.assertEqual(third["image_bytes"], images[2])
                    payload = post.call_args.kwargs["json"]
                    if model in GEMINI_MODELS:
                        parts = payload["contents"][0]["parts"]
                        self.assertEqual(len(parts), 2)
                        self.assert_reference_color(parts[1]["inline_data"]["data"], colors[1])
                    else:
                        self.assertEqual(len(payload["image"]), 1)
                        self.assert_reference_color(payload["image"][0], colors[1])
                    self.assertEqual(post.call_count, 3)
                    get.assert_not_called()

                prefix = f"old_refine_{third['id']}"
                with patch("requests.post") as post:
                    app.button(key=f"{prefix}_submit").click()
                    self.run_app(app)
                    self.assertTrue(app.warning)
                    post.assert_not_called()
                    app.session_state[f"{prefix}_images"] = [
                        ClipboardImageFile(self.reference_bytes, f"extra{i}.png", "image/png", str(i))
                        for i in range(16)
                    ]
                    self.run_app(app)
                    app.text_area(key=f"{prefix}_instruction").set_value("too many")
                    app.button(key=f"{prefix}_submit").click()
                    self.run_app(app)
                    self.assertIn("最多支持 16", app.warning[0].value)
                    post.assert_not_called()
                app.session_state[f"{prefix}_images"] = []
                self.run_app(app)
                app.selectbox(key=f"old_version_{third['id']}").select(initial["id"])
                self.run_app(app)
                prefix = f"old_refine_{initial['id']}"
                with patch("requests.post", return_value=self.response({"status": "failed", "error": "refine failed"})) as post:
                    app.text_area(key=f"{prefix}_instruction").set_value("try earlier version")
                    app.button(key=f"{prefix}_submit").click()
                    self.run_app(app)
                    self.assertIn("上一版图片已保留", app.error[0].value)
                    self.assertEqual(len(app.session_state["old_results"]), 3)
                    self.assertEqual(app.session_state["old_results"][0]["id"], third["id"])
                    self.assertEqual(app.selectbox(key=f"old_version_{third['id']}").value, initial["id"])
                    self.assertEqual(post.call_count, 1)

    def test_company_refinement_uses_latest_bytes_and_preserves_results_across_pages(self):
        app = AppTest.from_file(str(PROJECT / "app.py"))
        app.query_params["page"] = "company"
        self.run_app(app)
        first = self.colored_image((240, 30, 30))
        second = self.colored_image((30, 240, 30))
        third = self.colored_image((30, 30, 240))
        def response_bytes(content):
            return self.response({"data": [{"b64_json": base64.b64encode(content).decode()}]})
        with patch("requests.post", side_effect=[response_bytes(first), response_bytes(second), response_bytes(third)]) as post:
            app.text_area[0].set_value("initial company")
            next(item for item in app.button if item.label == "生成图片").click()
            self.run_app(app)
            initial = app.session_state["company_results"][0]
            self.run_app(app)
            self.assertEqual(post.call_count, 1)
            prefix = f"company_refine_{initial['id']}"
            app.session_state[f"{prefix}_images"] = [
                ClipboardImageFile(self.reference_bytes, "extra.png", "image/png", "extra")
            ]
            self.run_app(app)
            app.text_area(key=f"{prefix}_instruction").set_value("modify company")
            app.button(key=f"{prefix}_submit").click()
            self.run_app(app)
            self.assertEqual(post.call_args.args[0], "https://new-api.hk.ilabservice.cloud/v1/images/edits")
            self.assertEqual(len(post.call_args.kwargs["files"]), 2)
            self.assertEqual(post.call_args.kwargs["files"][0][1][1], first)
            self.assertEqual(post.call_args.kwargs["files"][1][1][1], self.reference_bytes)
            second_result = app.session_state["company_results"][0]
            self.assertEqual(second_result["image_bytes"], second)
            prefix = f"company_refine_{second_result['id']}"
            app.text_area(key=f"{prefix}_instruction").set_value("modify again")
            app.button(key=f"{prefix}_submit").click()
            self.run_app(app)
            self.assertEqual(len(post.call_args.kwargs["files"]), 1)
            self.assertEqual(post.call_args.kwargs["files"][0][1][1], second)
            self.assertEqual(post.call_count, 3)
            self.assertEqual(len(app.session_state["company_results"]), 3)
        latest = app.session_state["company_results"][0]
        prefix = f"company_refine_{latest['id']}"
        with patch("requests.post", side_effect=RuntimeError("company failed")):
            app.text_area(key=f"{prefix}_instruction").set_value("fail once")
            app.button(key=f"{prefix}_submit").click()
            self.run_app(app)
            self.assertIn("上一版图片已保留", app.error[0].value)
            self.assertEqual(app.session_state["company_results"][0]["image_bytes"], third)
        next(item for item in app.button if item.label == "旧中转生图").click()
        self.run_app(app)
        self.assertFalse(any(item.label == "选择结果版本" for item in app.selectbox))
        next(item for item in app.button if item.label == "公司生图").click()
        self.run_app(app)
        self.assertEqual(app.session_state["company_results"][0]["id"], latest["id"])
        self.assertTrue(any(item.label == "选择结果版本" for item in app.selectbox))

    def test_version_limit_and_invalid_image_preserve_latest(self):
        app = self.run_app(AppTest.from_file(str(PROJECT / "app.py")))
        image_bytes = self.colored_image((120, 120, 120))
        result = self.response({"data": [{"b64_json": base64.b64encode(image_bytes).decode()}]})
        with patch("requests.post", return_value=result):
            app.text_area[0].set_value("history")
            for _ in range(7):
                next(item for item in app.button if item.label == "生成图片").click()
                self.run_app(app)
            versions = app.session_state["old_results"]
            self.assertEqual([item["version"] for item in versions], [7, 6, 5, 4, 3])
        prefix = f"old_refine_{versions[0]['id']}"
        invalid = self.response({"data": [{"b64_json": base64.b64encode(b'not an image').decode()}]})
        with patch("requests.post", return_value=invalid):
            app.text_area(key=f"{prefix}_instruction").set_value("invalid result")
            app.button(key=f"{prefix}_submit").click()
            self.run_app(app)
            self.assertTrue(app.error)
            self.assertEqual(app.session_state["old_results"][0]["id"], versions[0]["id"])
        oldest = versions[-1]
        app.selectbox(key=f"old_version_{versions[0]['id']}").select(oldest["id"])
        self.run_app(app)
        prefix = f"old_refine_{oldest['id']}"
        with patch("requests.post", return_value=result):
            app.text_area(key=f"{prefix}_instruction").set_value("branch from earlier version")
            app.button(key=f"{prefix}_submit").click()
            self.run_app(app)
            self.assertFalse(app.error)
            history = app.session_state["old_results"]
            self.assertEqual(history[0]["parent_id"], oldest["id"])
            self.assertEqual([item["version"] for item in history], [8, 7, 6, 5, 4])


if __name__ == "__main__":
    unittest.main()
