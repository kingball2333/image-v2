import base64
import binascii
import os
import time
from io import BytesIO

import requests
import streamlit as st
from PIL import Image, UnidentifiedImageError


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
SIZE_OPTIONS = {
    "方形 1024x1024": "1024x1024",
    "竖版 1024x1536": "1024x1536",
    "横版 1536x1024": "1536x1024",
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

    if image_format == "jpeg":
        return "jpg", "image/jpeg"
    if image_format == "webp":
        return "webp", "image/webp"
    return "png", "image/png"


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
        "size": size,
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


def edit_company_image(api_key, prompt, size, uploaded_file):
    uploaded_file.seek(0)
    image_bytes = uploaded_file.getvalue()
    uploaded_file.seek(0)

    response = requests.post(
        COMPANY_IMAGE_EDIT_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        data={
            "model": COMPANY_IMAGE_MODEL,
            "prompt": prompt,
            "size": size,
        },
        files={
            "image": (
                uploaded_file.name or "reference.png",
                image_bytes,
                uploaded_file.type or "image/png",
            )
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return parse_image_response(response)


def display_image_result(image_data, started_at):
    image_bytes = fetch_image_bytes(image_data)
    file_extension, image_mime = get_download_info(image_bytes)
    elapsed_seconds = round(time.monotonic() - started_at, 2)

    st.success(f"生成成功，共耗时 {elapsed_seconds} 秒")
    revised_prompt = image_data.get("revised_prompt")
    if revised_prompt:
        with st.expander("查看优化后的提示词"):
            st.write(revised_prompt)

    st.image(image_bytes, width="stretch")
    st.download_button(
        "下载图片",
        data=image_bytes,
        file_name=f"company_image_{int(time.time())}.{file_extension}",
        mime=image_mime,
    )


def render_company_page():
    st.title("🏢 公司生图")
    old_page_link, company_page_link = st.columns(2)
    with old_page_link:
        if st.button("旧中转生图", icon="🎨", width="stretch"):
            st.query_params["page"] = "old"
            st.rerun()
    with company_page_link:
        st.button(
            "公司生图",
            icon="🏢",
            disabled=True,
            width="stretch",
        )

    api_key = get_company_api_key()
    if not api_key:
        st.error("未找到公司中转站密钥，请在 Streamlit Secrets 中配置 COMPANY_API_KEY。")
        st.stop()

    mode = st.radio("生成模式", ["文字生图", "图片重绘"], horizontal=True)
    size_label = st.selectbox("图片尺寸", list(SIZE_OPTIONS.keys()))
    uploaded_file = None
    if mode == "图片重绘":
        uploaded_file = st.file_uploader(
            "上传一张参考图",
            type=["png", "jpg", "jpeg", "webp"],
            accept_multiple_files=False,
        )
        if uploaded_file:
            st.image(uploaded_file, caption=uploaded_file.name, width="stretch")

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
        elif mode == "图片重绘" and uploaded_file is None:
            st.warning("请先上传一张参考图。")
        else:
            started_at = time.monotonic()
            try:
                with st.spinner("正在生成图片..."):
                    if mode == "文字生图":
                        image_data = generate_company_image(
                            api_key,
                            prompt.strip(),
                            SIZE_OPTIONS[size_label],
                        )
                    else:
                        image_data = edit_company_image(
                            api_key,
                            prompt.strip(),
                            SIZE_OPTIONS[size_label],
                            uploaded_file,
                        )
                display_image_result(image_data, started_at)
            except requests.exceptions.Timeout:
                st.error("请求超时，图片可能仍在处理中，请稍后重试。")
            except requests.exceptions.RequestException as exc:
                st.error(f"网络请求失败：{exc}")
            except (RuntimeError, ValueError, binascii.Error, UnidentifiedImageError) as exc:
                st.error(str(exc))
