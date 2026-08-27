import base64
import binascii
import math
import os
import time
from io import BytesIO

import requests
import streamlit as st
from PIL import Image, ImageOps, UnidentifiedImageError

from clipboard_image import render_paste_image_control


COMPANY_IMAGE_API_URL = os.environ.get(
    "COMPANY_IMAGE_API_URL",
    "https://new-api.hk.ilabservice.cloud/v1/images/generations",
).strip()
COMPANY_IMAGE_EDIT_URL = os.environ.get(
    "COMPANY_IMAGE_EDIT_URL",
    f"{COMPANY_IMAGE_API_URL.rsplit('/', 1)[0]}/edits",
).strip()
COMPANY_IMAGE_MODEL = os.environ.get("COMPANY_IMAGE_MODEL", "gpt-image-2")
REQUEST_TIMEOUT_SECONDS = 360
REFERENCE_MAX_EDGE = 4096
REFERENCE_JPEG_QUALITY = 95
MAX_REFERENCE_IMAGES = 16
SIZE_OPTIONS = {
    "自动匹配 · 文生图方形 / 重绘跟随首图": None,
    "方形 · 1024x1024": "1024x1024",
    "横版 · 1536x1024": "1536x1024",
    "竖版 · 1024x1536": "1024x1536",
}
EDIT_SIZE_RATIOS = {
    "1024x1024": 1.0,
    "1536x1024": 1.5,
    "1024x1536": 2 / 3,
}


def get_company_api_key():
    api_key = os.environ.get("COMPANY_API_KEY")
    if api_key:
        return api_key

    try:
        return st.secrets["COMPANY_API_KEY"]
    except (KeyError, FileNotFoundError):
        return None


def format_api_error(response):
    if response.status_code == 413:
        return "参考图总文件过大，请减少图片数量或压缩后重试。"
    if response.status_code == 429:
        return "请求过于频繁或账户额度不足，请稍后重试并检查公司中转额度。"
    if response.status_code in (502, 503, 504, 524):
        return "中转站暂时不可用或生成超时，请稍后重试。"

    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        if text.lower().startswith(("<!doctype html", "<html")):
            return "服务器返回了 HTML 错误页，请稍后重试或检查中转站状态。"
        return text or "服务器没有返回错误详情。"

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or error)
        if error:
            return str(error)
        for key in ("message", "detail"):
            if payload.get(key):
                return str(payload[key])
    return str(payload)


def fetch_image_bytes(image_data):
    base64_data = image_data.get("b64_json")
    if base64_data:
        if base64_data.startswith("data:image"):
            base64_data = base64_data.split(",", 1)[1]
        return base64.b64decode(base64_data)

    image_url = image_data.get("url")
    if not image_url:
        raise ValueError("返回结果中没有找到 b64_json 或 url。")

    image_response = requests.get(image_url, timeout=REQUEST_TIMEOUT_SECONDS)
    image_response.raise_for_status()
    return image_response.content


def get_download_info(image_bytes):
    with Image.open(BytesIO(image_bytes)) as image:
        image_format = (image.format or "PNG").lower()
        image_size = image.size

    if image_format == "jpeg":
        return "jpg", "image/jpeg", image_size, "JPEG"
    if image_format == "webp":
        return "webp", "image/webp", image_size, "WebP"
    return "png", "image/png", image_size, "PNG"


def get_reference_size(uploaded_file):
    uploaded_file.seek(0)
    with Image.open(uploaded_file) as image:
        width, height = image.size
    uploaded_file.seek(0)
    return width, height


def get_edit_size(width, height):
    """在接口已验证的尺寸中，选择与首张参考图画幅最接近的一项。"""
    ratio = width / height
    return min(
        EDIT_SIZE_RATIOS,
        key=lambda size: abs(math.log(ratio / EDIT_SIZE_RATIOS[size])),
    )


def format_bytes(byte_count):
    if byte_count >= 1024 * 1024:
        return f"{byte_count / (1024 * 1024):.1f} MB"
    return f"{byte_count / 1024:.1f} KB"


def prepare_reference_file(uploaded_file):
    """普通图片保持原始质量，超大图片才缩放，避免无必要的清晰度损失。"""
    uploaded_file.seek(0)
    original_bytes = uploaded_file.getvalue()
    uploaded_file.seek(0)

    with Image.open(BytesIO(original_bytes)) as image:
        image = ImageOps.exif_transpose(image)
        original_size = image.size
        if max(image.size) <= REFERENCE_MAX_EDGE:
            return {
                "filename": uploaded_file.name or "reference.png",
                "mime_type": uploaded_file.type or "image/png",
                "content": original_bytes,
                "original_size": original_size,
                "prepared_size": original_size,
                "original_bytes": len(original_bytes),
                "prepared_bytes": len(original_bytes),
                "was_resized": False,
            }

        scale = REFERENCE_MAX_EDGE / max(image.size)
        resized = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
        output = BytesIO()
        has_alpha = "A" in resized.getbands() or (
            resized.mode == "P" and "transparency" in resized.info
        )
        if has_alpha:
            resized.save(output, format="PNG", optimize=True)
            filename = "reference.png"
            mime_type = "image/png"
        else:
            resized.convert("RGB").save(
                output,
                format="JPEG",
                quality=REFERENCE_JPEG_QUALITY,
                optimize=True,
            )
            filename = "reference.jpg"
            mime_type = "image/jpeg"

        prepared_bytes = output.getvalue()
        return {
            "filename": filename,
            "mime_type": mime_type,
            "content": prepared_bytes,
            "original_size": original_size,
            "prepared_size": resized.size,
            "original_bytes": len(original_bytes),
            "prepared_bytes": len(prepared_bytes),
            "was_resized": True,
        }


def parse_image_response(response):
    if not response.ok:
        raise RuntimeError(
            f"生成请求失败（HTTP {response.status_code}）：{format_api_error(response)}"
        )

    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("接口返回的不是有效 JSON。") from exc

    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise RuntimeError("接口返回成功，但响应中没有可用的 data[0] 图片结果。")
    return data[0]


def generate_company_image(api_key, prompt, size):
    payload = {
        "model": COMPANY_IMAGE_MODEL,
        "prompt": prompt,
        "size": size or "1024x1024",
    }
    response = requests.post(
        COMPANY_IMAGE_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return parse_image_response(response)


def edit_company_image(api_key, prompt, size, uploaded_files):
    prepared_references = [
        prepare_reference_file(uploaded_file) for uploaded_file in uploaded_files
    ]
    files = [
        (
            "image",
            (
                reference["filename"],
                reference["content"],
                reference["mime_type"],
            ),
        )
        for reference in prepared_references
    ]

    response = requests.post(
        COMPANY_IMAGE_EDIT_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        data={
            "model": COMPANY_IMAGE_MODEL,
            "prompt": prompt,
            "size": size,
        },
        files=files,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return parse_image_response(response), prepared_references


def display_image_result(image_data, started_at, extra_caption=None):
    image_bytes = fetch_image_bytes(image_data)
    file_extension, image_mime, image_size, image_format = get_download_info(image_bytes)
    elapsed_seconds = round(time.monotonic() - started_at, 2)

    st.success(f"生成成功，共耗时 {elapsed_seconds} 秒")
    caption = f"输出尺寸：{image_size[0]} × {image_size[1]} · {image_format}"
    if extra_caption:
        caption = f"{extra_caption} · {caption}"
    st.caption(caption)
    revised_prompt = image_data.get("revised_prompt")
    if revised_prompt:
        with st.expander("查看优化后的提示词"):
            st.write(revised_prompt)

    st.image(image_bytes, use_container_width=True)
    st.download_button(
        "下载图片",
        data=image_bytes,
        file_name=f"company_image_{int(time.time())}.{file_extension}",
        mime=image_mime,
        use_container_width=True,
    )


def render_company_page():
    st.title("🏢 公司生图8-20")
    st.caption("gpt-image-2 · 公司中转 · 支持文字生图和多图参考重绘")
    old_page_link, company_page_link = st.columns(2)
    with old_page_link:
        if st.button("旧中转生图", icon="🎨", use_container_width=True):
            st.query_params["page"] = "old"
            st.rerun()
    with company_page_link:
        st.button(
            "公司生图",
            icon="🏢",
            disabled=True,
            use_container_width=True,
        )

    api_key = get_company_api_key()
    if not api_key:
        st.error("未找到公司中转站密钥，请在 Streamlit Secrets 中配置 COMPANY_API_KEY。")
        st.stop()

    st.divider()
    mode = st.radio("生成模式", ["文字生图", "图片重绘"], horizontal=True)
    size_label = st.selectbox(
        "输出尺寸",
        list(SIZE_OPTIONS.keys()),
        help="自动模式下，文生图使用 1024x1024；图片重绘根据第一张参考图的画幅自动选择。",
    )
    selected_size = SIZE_OPTIONS[size_label]
    uploaded_files = []
    reference_size = None
    if mode == "图片重绘":
        st.info("可上传多张参考图，第一张决定自动输出画幅；请在提示词中说明各图片的用途。")
        uploaded_files = st.file_uploader(
            "上传参考图",
            type=["png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True,
            help=f"最多上传 {MAX_REFERENCE_IMAGES} 张，支持 PNG、JPG 和 WebP。",
        )
        pasted_files = render_paste_image_control("company_edit")
        all_reference_files = list(uploaded_files or []) + pasted_files
        if all_reference_files:
            st.caption(
                f"已选择 {len(all_reference_files)} / {MAX_REFERENCE_IMAGES} 张参考图"
            )
            preview_columns = st.columns(4)
            for index, uploaded_file in enumerate(all_reference_files[:4]):
                with preview_columns[index]:
                    st.image(
                        uploaded_file.getvalue(),
                        caption=uploaded_file.name,
                        width=160,
                    )
            if len(all_reference_files) > 4:
                st.caption(f"还有 {len(all_reference_files) - 4} 张未预览")

            reference_size = get_reference_size(all_reference_files[0])
            output_size = selected_size or get_edit_size(*reference_size)
            st.info(
                f"第一张参考图：{reference_size[0]} × {reference_size[1]} · "
                f"{'自动' if selected_size is None else '指定'}输出：{output_size}"
            )
            st.caption(
                f"普通参考图保持原始文件质量；仅当最长边超过 {REFERENCE_MAX_EDGE}px 时等比缩放，"
                "透明图片仍保留透明通道。"
            )
    elif selected_size is None:
        st.caption("自动模式下，文字生图输出为 1024x1024。")

    prompt = st.text_area(
        "画面描述",
        placeholder=(
            "例如：一只可爱的黄色小鸭，蓝色背景，儿童绘本风格"
            if mode == "文字生图"
            else "例如：保留主体和构图，把背景改成蓝色"
        ),
        height=160,
    )

    button_label = "生成图片" if mode == "文字生图" else "开始重绘"
    if st.button(button_label, type="primary"):
        if not prompt.strip():
            st.warning("请先输入画面描述。")
        elif mode == "图片重绘" and not all_reference_files:
            st.warning("请先上传至少一张参考图。")
        elif mode == "图片重绘" and len(all_reference_files) > MAX_REFERENCE_IMAGES:
            st.warning(f"参考图最多支持 {MAX_REFERENCE_IMAGES} 张，请减少后再试。")
        else:
            started_at = time.monotonic()
            try:
                with st.spinner("正在生成图片..."):
                    if mode == "文字生图":
                        image_data = generate_company_image(
                            api_key,
                            prompt.strip(),
                            selected_size,
                        )
                        result_caption = None
                    else:
                        image_data, prepared_references = edit_company_image(
                            api_key,
                            prompt.strip(),
                            selected_size or get_edit_size(*reference_size),
                            all_reference_files,
                        )
                        original_total = sum(
                            item["original_bytes"] for item in prepared_references
                        )
                        prepared_total = sum(
                            item["prepared_bytes"] for item in prepared_references
                        )
                        resized_count = sum(
                            item["was_resized"] for item in prepared_references
                        )
                        quality_note = (
                            f"{resized_count} 张超大图已高质量缩放"
                            if resized_count
                            else "均保持原始文件质量"
                        )
                        result_caption = (
                            f"参考图 {len(prepared_references)} 张，{quality_note} · "
                            f"发送体积 {format_bytes(original_total)}"
                        )
                        if prepared_total != original_total:
                            result_caption += f" -> {format_bytes(prepared_total)}"
                display_image_result(image_data, started_at, result_caption)
            except requests.exceptions.Timeout:
                st.error("请求超时，图片可能仍在处理中，请稍后重试。")
            except requests.exceptions.RequestException as exc:
                st.error(f"网络请求失败：{exc}")
            except (RuntimeError, ValueError, binascii.Error, UnidentifiedImageError) as exc:
                st.error(str(exc))
