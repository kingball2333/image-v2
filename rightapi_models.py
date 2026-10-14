"""RightAPI 绘图模型、渠道路由和两种协议的请求适配。"""

import os


MODEL_OPTIONS = {
    "GPT Image 2 VIP（原模型）": "gpt-image-2-vip",
    "GPT Image 2.5（原模型）": "gpt-image-2.5",
    "GPT Image 2（基础）": "gpt-image-2",
    "GPT Image 2.5 Flare（速度优先）": "gpt-image-2.5-flare",
    "GPT Image 2.5 Sunburst（精细编辑）": "gpt-image-2.5-sunburst",
    "Nano Banana Pro（画质优先）": "gemini-3-pro-image-preview",
    "Nano Banana 2.1（速度优先）": "gemini-nano-banana-2.1",
}
LEGACY_MODELS = {"gpt-image-2-vip", "gpt-image-2.5"}
GEMINI_MODELS = {"gemini-3-pro-image-preview", "gemini-nano-banana-2.1"}
QUALITY_OPTIONS = {
    "自动": "auto",
    "低 · 快速草稿": "low",
    "中 · 平衡": "medium",
    "高 · 精细": "high",
    "超高 · xhigh": "xhigh",
    "最高 · max": "max",
}
GEMINI_ASPECT_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9", "21:9")
GEMINI_SIZE_OPTIONS = ("1K", "2K", "4K")
MODEL_DESCRIPTIONS = {
    "gpt-image-2-vip": "原有 VIP 模型，保留原来的生成和多图参考方式。",
    "gpt-image-2.5": "原有 GPT Image 2.5，保留原来的调用方式。",
    "gpt-image-2": "GPT Image 基础模型，最高支持 high 质量档位。",
    "gpt-image-2.5-flare": "新版 GPT Image 2.5，速度优先，支持 xhigh 和 max 质量。",
    "gpt-image-2.5-sunburst": "新版 GPT Image 2.5，适合精细编辑、多图融合和文字细节。",
    "gemini-3-pro-image-preview": "Nano Banana Pro，适合高质量出图和多图参考；按画幅比例与分辨率档位生成。",
    "gemini-nano-banana-2.1": "Nano Banana 2.1，适合快速创意出图；按画幅比例与分辨率档位生成。",
}


def get_quality_options(model):
    """只提供该模型在新版文档中支持的质量档位。"""
    if model in LEGACY_MODELS or model in GEMINI_MODELS:
        return {}
    values = ("auto", "low", "medium", "high")
    if model in {"gpt-image-2.5-flare", "gpt-image-2.5-sunburst"}:
        values += ("xhigh", "max")
    return {label: value for label, value in QUALITY_OPTIONS.items() if value in values}


def match_gemini_aspect_ratio(width, height):
    """从文档列出的画幅中，选择最接近首张参考图比例的一项。"""
    if width <= 0 or height <= 0:
        raise ValueError("参考图片宽高必须大于零。")
    target = width / height

    def distance(ratio):
        numerator, denominator = map(int, ratio.split(":"))
        value = numerator / denominator
        return max(value / target, target / value)

    return min(GEMINI_ASPECT_RATIOS, key=distance)


def build_image_request(model, prompt, size, reference_images=None, quality="auto", image_size="1K"):
    """返回提交地址和 JSON；参考图复用页面已压缩的 data URL，保持原顺序。"""
    if model not in MODEL_OPTIONS.values():
        raise ValueError("请选择页面提供的生图模型。")
    references = list(reference_images or [])
    if model in LEGACY_MODELS:
        base = os.environ.get("RIGHTAPI_DRAW_BASE_URL", "https://www.rightapi.ai/draw").rstrip("/")
        payload = {"model": model, "prompt": prompt, "n": 1, "size": size, "async": True}
        if references:
            payload["image"] = references
        return f"{base}/v1/images/generations", payload

    # 老版 Key 必须携带渠道前缀；绑定新版渠道的 Key 可用环境变量改成统一域名。
    base = os.environ.get("RIGHTAPI_IMAGE_PRO_BASE_URL", "https://www.rightapi.ai/image-pro").rstrip("/")
    if model in GEMINI_MODELS:
        if size not in GEMINI_ASPECT_RATIOS or image_size not in GEMINI_SIZE_OPTIONS:
            raise ValueError("Gemini 需要支持的画幅比例，以及 1K、2K 或 4K 分辨率档位。")
        parts = [{"text": prompt}]
        for data_url in references:
            metadata, separator, encoded = data_url.partition(",")
            if not separator or not metadata.startswith("data:image/") or not metadata.endswith(";base64") or not encoded:
                raise ValueError("Gemini 参考图必须是带 MIME 类型的 base64 图片。")
            parts.append({"inline_data": {"mime_type": metadata[5:-7], "data": encoded}})
        payload = {
            "async": True,
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"imageConfig": {"aspectRatio": size, "imageSize": image_size}},
        }
        return f"{base}/v1beta/models/{model}:generateContent", payload

    if quality not in get_quality_options(model).values():
        raise ValueError("当前模型不支持选中的质量档位。")
    payload = {
        "model": model, "prompt": prompt, "n": 1, "size": size,
        "quality": quality, "output_format": "png", "async": True,
    }
    path = "generations"
    if references:
        payload["image"] = references
        path = "edits"
    return f"{base}/v1/images/{path}", payload
