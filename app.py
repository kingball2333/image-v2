import streamlit as st
import requests
import base64
import os
import time
from io import BytesIO
from PIL import Image, ImageOps

from company_image_page import get_download_info, render_company_page
from clipboard_image import remove_pasted_image, render_paste_image_control
from rightapi_models import (
    GEMINI_ASPECT_RATIOS,
    GEMINI_MODELS,
    GEMINI_SIZE_OPTIONS,
    MODEL_DESCRIPTIONS,
    MODEL_OPTIONS,
    build_image_request,
    get_quality_options,
    match_gemini_aspect_ratio,
)

# RightAPI 使用异步任务；默认回退地址保留旧 /draw，新模型路由由适配模块选择。
# 任务查询不带渠道前缀，保留环境变量覆盖以支持不同部署环境。
API_BASE = os.environ.get("RIGHTAPI_DRAW_BASE_URL", "https://www.rightapi.ai/draw").rstrip("/")
GENERATIONS_URL = f"{API_BASE}/v1/images/generations"
TASKS_BASE_URL = os.environ.get(
    "RIGHTAPI_TASKS_BASE_URL",
    "https://www.rightapi.ai/v1/tasks",
).rstrip("/")
POLL_INTERVAL_SECONDS = 3
POLL_TIMEOUT_SECONDS = 600
POLL_REQUEST_TIMEOUT_SECONDS = 30
MIN_PIXELS = 655_360
MAX_PIXELS = 8_294_400
MAX_EDGE = 3840
MAX_RATIO = 3
EXPERIMENTAL_PIXELS = 2560 * 1440
MAX_REFERENCE_IMAGES = 16
REFERENCE_MAX_EDGE = 2048
REFERENCE_JPEG_QUALITY = 92
SIZE_OPTIONS = {
    "自动默认": None,
    "方形 1024x1024": "1024x1024",
    "竖版 1024x1536": "1024x1536",
    "横版 1536x1024": "1536x1024",
    "2K 方形 2048x2048（一般不建议）": "2048x2048",
    "2K 横版 2048x1152（一般不建议）": "2048x1152",
    "长海报 720x2160": "720x2160",
    "4K 横版 3840x2160（实验性大图）": "3840x2160",
    "4K 竖版 2160x3840（实验性大图）": "2160x3840",
}

def format_error_message(response):
    if response.status_code == 524:
        return (
            "请求在中转站超时了（Cloudflare 524）。通常是图片任务处理时间太长，"
            "建议换小一点的尺寸、压缩参考图，或稍后重试。"
        )

    content_type = response.headers.get("Content-Type", "")
    if "application/json" in content_type:
        try:
            return format_error_payload(response.json())
        except ValueError:
            pass

    text = response.text.strip()
    if text.lower().startswith("<!doctype html") or text.lower().startswith("<html"):
        return "服务器返回了 HTML 错误页，请稍后重试或检查中转站状态。"

    return text


def format_error_payload(payload):
    """从中转站的错误 JSON 中提取适合直接展示给用户的消息。"""
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
    """兼容 OpenAI 图片接口常见的 b64_json 和 url 两种返回格式。"""
    b64_str = image_data.get("b64_json")
    if b64_str:
        return base64.b64decode(b64_str)

    image_url = image_data.get("url")
    if not image_url:
        raise ValueError("返回数据中没有找到 b64_json 或 url")

    if image_url.startswith("data:image"):
        return base64.b64decode(image_url.split(",", 1)[1])

    img_response = requests.get(image_url, timeout=120)
    img_response.raise_for_status()
    return img_response.content


def generate_image_task(payload, progress_callback=None, request_url=None):
    """提交并轮询绘图任务，兼容 OpenAI data 和 Gemini candidates 两类结果。"""
    response = requests.post(
        request_url or GENERATIONS_URL,
        json=payload,
        headers=HEADERS,
        timeout=POLL_REQUEST_TIMEOUT_SECONDS,
    )
    if not response.ok:
        raise RuntimeError(
            f"提交绘图任务失败（HTTP {response.status_code}）：{format_error_message(response)}"
        )

    try:
        task_data = response.json()
    except ValueError as exc:
        raise RuntimeError("提交绘图任务失败：服务器返回的不是有效 JSON。") from exc
    if not isinstance(task_data, dict):
        raise RuntimeError(f"提交绘图任务失败：服务器返回了异常格式。返回内容：{format_error_payload(task_data)}")

    failed_statuses = {"failed", "expired", "canceled"}
    if task_data.get("status") in failed_statuses:
        raise RuntimeError(f"提交绘图任务失败：{format_error_payload(task_data)}")
    # 兼容中转站直接返回完成结果的情况。
    task_id = task_data.get("task_id")
    if not task_id:
        if "data" in task_data or "candidates" in task_data:
            return task_data
        raise RuntimeError(f"提交绘图任务失败：响应中没有 task_id。返回内容：{format_error_payload(task_data)}")

    pending_statuses = {"processing", "pending", "queued", "in_progress"}
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = task_data.get("status")
        progress = task_data.get("progress")
        if progress_callback:
            progress_callback(status or "processing", progress)

        if status in failed_statuses:
            raise RuntimeError(f"绘图任务失败：{format_error_payload(task_data)}")
        if status == "completed" or "data" in task_data or "candidates" in task_data:
            return task_data
        if status and status not in pending_statuses:
            raise RuntimeError(f"绘图任务返回未知状态 {status!r}：{format_error_payload(task_data)}")

        time.sleep(POLL_INTERVAL_SECONDS)
        task_response = requests.get(
            f"{TASKS_BASE_URL}/{task_id}",
            headers=HEADERS,
            timeout=POLL_REQUEST_TIMEOUT_SECONDS,
        )
        if not task_response.ok:
            raise RuntimeError(
                f"查询绘图任务失败（HTTP {task_response.status_code}）："
                f"{format_error_message(task_response)}"
            )
        try:
            task_data = task_response.json()
        except ValueError as exc:
            raise RuntimeError("查询绘图任务失败：服务器返回的不是有效 JSON。") from exc
        if not isinstance(task_data, dict):
            raise RuntimeError(f"查询绘图任务失败：服务器返回了异常格式。返回内容：{format_error_payload(task_data)}")

    raise TimeoutError(
        f"绘图任务超过 {POLL_TIMEOUT_SECONDS // 60} 分钟仍未完成，任务号：{task_id}。"
        "请到中转站检查任务状态后再重试，以免重复扣费。"
    )


def update_task_progress(progress_bar, status_placeholder, status, progress):
    """更新 Streamlit 中的异步任务进度，不影响旧版 Streamlit。"""
    if isinstance(progress, (int, float)):
        progress_bar.progress(max(0, min(100, int(progress))))
    if status:
        progress_text = f"（{int(progress)}%）" if isinstance(progress, (int, float)) else ""
        status_placeholder.caption(f"任务状态：{status}{progress_text}")


def get_first_image_data(result):
    """统一两种响应：OpenAI data 图片对象，或 Gemini text 中的图片 URL。"""
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    candidates = result.get("candidates") if isinstance(result, dict) else None
    if isinstance(candidates, list):
        for candidate in candidates:
            content = candidate.get("content") if isinstance(candidate, dict) else None
            parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(parts, list):
                continue
            for part in parts:
                text = part.get("text") if isinstance(part, dict) else None
                if isinstance(text, str) and text.strip().startswith(("https://", "http://")):
                    return {"url": text.strip()}
    raise ValueError("任务已完成，但返回数据中没有可用的图片结果。")


def uploaded_file_to_data_url(uploaded_file):
    uploaded_file.seek(0)
    image_bytes = uploaded_file.getvalue()
    uploaded_file.seek(0)
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    return f"data:{uploaded_file.type};base64,{base64_image}"


def format_bytes(byte_count):
    if byte_count >= 1024 * 1024:
        return f"{byte_count / 1024 / 1024:.2f} MB"
    return f"{byte_count / 1024:.0f} KB"


def get_resample_filter():
    if hasattr(Image, "Resampling"):
        return Image.Resampling.LANCZOS
    return Image.LANCZOS


def uploaded_file_to_compressed_data_url(
    uploaded_file,
    max_edge=REFERENCE_MAX_EDGE,
    quality=REFERENCE_JPEG_QUALITY,
):
    uploaded_file.seek(0)
    original_bytes = uploaded_file.getvalue()
    uploaded_file.seek(0)

    with Image.open(BytesIO(original_bytes)) as image:
        original_orientation = image.getexif().get(274, 1)
        image = ImageOps.exif_transpose(image)
        original_width, original_height = image.size
        has_transparency = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
        was_resized = False

        if max(original_width, original_height) > max_edge:
            scale = max_edge / max(original_width, original_height)
            new_size = (
                max(1, round(original_width * scale)),
                max(1, round(original_height * scale)),
            )
            image = image.resize(new_size, get_resample_filter())
            was_resized = True

        if has_transparency:
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.convert("RGBA").getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")

        buffer = BytesIO()
        image.save(
            buffer,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
            subsampling=0,
        )
        compressed_bytes = buffer.getvalue()
        compressed_size = image.size

    original_mime = uploaded_file.type or "image/jpeg"
    use_original = (
        original_mime in ("image/jpeg", "image/jpg")
        and not was_resized
        and not has_transparency
        and original_orientation == 1
        and len(original_bytes) <= len(compressed_bytes)
    )
    if use_original:
        output_bytes = original_bytes
        output_mime = original_mime
    else:
        output_bytes = compressed_bytes
        output_mime = "image/jpeg"

    base64_image = base64.b64encode(output_bytes).decode("utf-8")
    stats = {
        "name": uploaded_file.name,
        "original_bytes": len(original_bytes),
        "compressed_bytes": len(output_bytes),
        "original_size": (original_width, original_height),
        "compressed_size": compressed_size,
    }
    return f"data:{output_mime};base64,{base64_image}", stats


def parse_size(size):
    width, height = size.lower().split("x", 1)
    return int(width), int(height)


def validate_image_size(size):
    width, height = parse_size(size)
    pixels = width * height
    ratio = max(width, height) / min(width, height)

    if width % 16 != 0 or height % 16 != 0:
        return "图片宽高都必须是 16 的倍数。"
    if max(width, height) > MAX_EDGE:
        return f"图片最大边不能超过 {MAX_EDGE}px。"
    if ratio > MAX_RATIO:
        return f"图片长边:短边不能超过 {MAX_RATIO}:1。"
    if pixels < MIN_PIXELS or pixels > MAX_PIXELS:
        return f"图片总像素数必须在 {MIN_PIXELS:,} 到 {MAX_PIXELS:,} 之间。"

    return None


def normalize_reference_size(width, height):
    """把参考图尺寸修正到 GPT Image 2 支持的范围内，并尽量保持原比例和像素量。"""
    target_ratio = width / height

    if target_ratio <= 1 / 2:
        return "720x2160"
    if target_ratio < 1:
        return "1024x1536"
    if target_ratio >= 2:
        return "3072x1024"
    if target_ratio > 1:
        return "1536x1024"

    return "1024x1024"


def normalize_reference_size_precise(width, height):
    """保留精细尺寸算法，后续需要更贴近参考图比例时可以切换使用。"""
    original_area = width * height
    target_area = min(max(original_area, MIN_PIXELS), MAX_PIXELS)
    target_ratio = min(max(width / height, 1 / MAX_RATIO), MAX_RATIO)

    best_size = None
    best_score = None

    for candidate_width in range(16, MAX_EDGE + 1, 16):
        for candidate_height in range(16, MAX_EDGE + 1, 16):
            area = candidate_width * candidate_height
            if area < MIN_PIXELS or area > MAX_PIXELS:
                continue

            ratio = candidate_width / candidate_height
            if ratio > MAX_RATIO or (1 / ratio) > MAX_RATIO:
                continue

            ratio_score = abs(ratio - target_ratio) / target_ratio
            area_score = abs(area - target_area) / target_area
            score = (ratio_score * 10) + area_score

            if best_score is None or score < best_score:
                best_score = score
                best_size = (candidate_width, candidate_height)

    if best_size is None:
        return "1024x1024"

    return f"{best_size[0]}x{best_size[1]}"


def is_experimental_size(size):
    width, height = parse_size(size)
    return width * height > EXPERIMENTAL_PIXELS


def resolve_output_size(size_setting, reference_size=None, gemini=False):
    """两种生成模式共用尺寸解析；Gemini 用比例，GPT 用像素尺寸。"""
    if gemini:
        if size_setting != "自动匹配":
            return size_setting
        return match_gemini_aspect_ratio(*reference_size) if reference_size else "1:1"
    size = SIZE_OPTIONS[size_setting]
    if size is None:
        size = normalize_reference_size(*reference_size) if reference_size else "1024x1024"
    error = validate_image_size(size)
    if error:
        raise ValueError(f"当前尺寸 {size} 不符合模型要求：{error}")
    return size


st.set_page_config(
    page_title="AI 绘图助手",
    page_icon="🎨",
    initial_sidebar_state="expanded",
)

if st.query_params.get("page") == "company":
    render_company_page()
    st.stop()

# 旧中转只在旧页面读取自己的密钥，避免影响公司页面的独立配置。
API_KEY = os.environ.get("MY_API_KEY")
if not API_KEY:
    try:
        API_KEY = st.secrets["MY_API_KEY"]
    except (KeyError, FileNotFoundError):
        st.error("未找到 API Key，请配置环境变量 MY_API_KEY")
        st.stop()

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

st.title("🎨 AI 绘图助手 Made BY ljj（多模型版10-14）")
old_page_link, company_page_link = st.columns(2)
with old_page_link:
    st.button(
        "旧中转生图",
        icon="🎨",
        disabled=True,
        use_container_width=True,
    )
with company_page_link:
    if st.button("公司生图", icon="🏢", use_container_width=True):
        st.query_params["page"] = "company"
        st.rerun()
model_choice = st.sidebar.selectbox(
    "生图模型",
    list(MODEL_OPTIONS.keys()),
    index=0,
    key="old_image_model",
    help="文字生图和图片重绘都使用此处选择的模型。",
)
selected_model = MODEL_OPTIONS[model_choice]
is_gemini = selected_model in GEMINI_MODELS
st.sidebar.caption(f"当前模型：{selected_model}")
st.sidebar.caption(MODEL_DESCRIPTIONS[selected_model])
quality = "auto"
image_size_tier = "1K"
if is_gemini:
    size_choice = st.sidebar.selectbox(
        "画幅比例", ["自动匹配", *GEMINI_ASPECT_RATIOS], key="old_gemini_ratio",
    )
    image_size_tier = st.sidebar.selectbox(
        "分辨率档位", GEMINI_SIZE_OPTIONS, key="old_gemini_resolution",
        help="分辨率越高，耗时与费用通常越高；实际像素尺寸由模型决定。",
    )
    st.sidebar.caption("自动匹配：文生图用 1:1；图生图选择最接近第一张参考图的画幅。")
else:
    size_choice = st.sidebar.selectbox(
        "图片尺寸", list(SIZE_OPTIONS.keys()), index=0, key="old_gpt_size",
        help="分辨率越高，生成时间越长；实验性大图可能更不稳定。",
    )
    st.sidebar.caption("自动默认：文生图用 1024x1024；图生图按第一张参考图比例匹配支持的尺寸。")
    quality_options = get_quality_options(selected_model)
    if quality_options:
        quality_choice = st.sidebar.selectbox(
            "渲染质量", list(quality_options), key=f"old_quality_{selected_model}",
            help="质量越高，耗时与费用通常越高。建议先用自动或中等质量。",
        )
        quality = quality_options[quality_choice]
st.sidebar.caption("每次生成 1 张；各模型费用不同，请以中转站账号计费为准。")

tab1, tab2 = st.tabs(["📝 文字生图", "🖼️ 图片重绘"])

with tab1:
    # 优化了默认文字，使用 placeholder 提示用户
    prompt = st.text_area("请输入画面描述：", placeholder="例如：画一个小鸡", value="")

    if st.button("生成图片", type="primary"):
        if not prompt.strip():
            st.warning("⚠️ 提示词不能为空哦，请先输入你想画什么！")
        else:
            with st.spinner("AI 正在疯狂作画中，请稍候..."):
                start_time = time.time()  # 记录开始时间
                try:
                    image_size = resolve_output_size(size_choice, gemini=is_gemini)
                    request_url, payload = build_image_request(
                        selected_model, prompt, image_size, quality=quality,
                        image_size=image_size_tier,
                    )
                except ValueError as exc:
                    st.error(str(exc))
                    st.stop()
                if not is_gemini and is_experimental_size(image_size):
                    st.warning("当前尺寸超过 2560x1440 像素量，属于 experimental 范围，生成可能更慢或不稳定。")
                progress_bar = st.progress(0)
                status_placeholder = st.empty()
                try:
                    res_data = generate_image_task(
                        payload,
                        lambda status, progress: update_task_progress(
                            progress_bar, status_placeholder, status, progress
                        ),
                        request_url=request_url,
                    )

                    # 计算耗时
                    end_time = time.time()
                    elapsed_time = round(end_time - start_time, 2)

                    # 提取数据
                    progress_bar.progress(100)
                    status_placeholder.caption("任务状态：completed")
                    image_data = get_first_image_data(res_data)
                    revised_prompt = image_data.get("revised_prompt", "原样回显（模型未优化）")
                    image_bytes = fetch_image_bytes(image_data)
                    extension, mime, actual_size, image_format = get_download_info(image_bytes)

                    # 【优化】显示成功提示、耗时和优化的提示词
                    st.success(f"🎉 生成成功！模型 {selected_model}，实际尺寸 {actual_size[0]}×{actual_size[1]}，共耗时 {elapsed_time} 秒")
                    st.caption(f"请求画幅 / 尺寸：{image_size}" + (f" · 分辨率档位：{image_size_tier}" if is_gemini else f" · 质量：{quality}") + f" · {image_format}")
                    with st.expander("💡 查看 AI 优化后的提示词 (点击展开)", expanded=True):
                        st.info(revised_prompt)

                    # 渲染图片
                    st.image(image_bytes, use_container_width=True)
                    st.download_button(label="📥 下载图片", data=image_bytes,
                                       file_name=f"ai_image_{int(time.time())}.{extension}", mime=mime)

                except requests.exceptions.Timeout:
                    st.error("❌ 网络请求超时了，请稍后重试。")
                except TimeoutError as e:
                    st.error(f"❌ {e}")
                except Exception as e:
                    st.error(f"❌ 请求发生异常：{e}")

with tab2:
    st.info("图生图功能：上传参考图并输入修改指令")
    st.caption(f"参考图发送前会自动等比压缩到最长边 {REFERENCE_MAX_EDGE}px、JPEG 质量 {REFERENCE_JPEG_QUALITY}，以降低超时概率。")
    uploaded_files = st.file_uploader(
        "上传参考图",
        type=["png", "jpg", "jpeg"],
        accept_multiple_files=True,
        help=f"最多上传 {MAX_REFERENCE_IMAGES} 张参考图"
    )
    pasted_files = render_paste_image_control("old_edit")
    uploaded_files = list(uploaded_files or [])
    all_reference_files = uploaded_files + pasted_files
    if all_reference_files:
        st.caption(f"已选择 {len(all_reference_files)} / {MAX_REFERENCE_IMAGES} 张参考图")
        preview_columns = st.columns(4)
        for index, file in enumerate(all_reference_files[:4]):
            with preview_columns[index]:
                st.image(file.getvalue(), caption=file.name, width=160)
                if index >= len(uploaded_files):
                    pasted_index = index - len(uploaded_files)
                    if st.button(
                        "删除",
                        key=f"old_edit_remove_{pasted_index}",
                        help="移除这张剪切板图片",
                    ):
                        remove_pasted_image("old_edit", pasted_index)
                        st.rerun()
        if len(all_reference_files) > 4:
            st.caption(f"还有 {len(all_reference_files) - 4} 张未预览")
        if is_gemini and size_choice == "自动匹配":
            with Image.open(BytesIO(all_reference_files[0].getvalue())) as reference_image:
                reference_size = ImageOps.exif_transpose(reference_image).size
            st.caption(
                f"首张参考图 {reference_size[0]}×{reference_size[1]} → "
                f"自动画幅 {resolve_output_size(size_choice, reference_size, gemini=True)} · {image_size_tier}"
            )

    edit_prompt = st.text_input("修改指令", placeholder="例如：改成水彩画风")

    if st.button("开始重绘"):
        if not all_reference_files:
            st.warning("⚠️ 请先上传至少一张参考图片！")
        elif len(all_reference_files) > MAX_REFERENCE_IMAGES:
            st.warning(f"⚠️ 参考图最多支持 {MAX_REFERENCE_IMAGES} 张，请减少后再试。")
        elif not edit_prompt.strip():
            st.warning("⚠️ 请输入你要修改的指令！")
        else:
            with st.spinner("AI 正在重绘..."):
                start_time = time.time()
                first_uploaded_file = all_reference_files[0]
                first_uploaded_file.seek(0)
                with Image.open(first_uploaded_file) as reference_image:
                    reference_image = ImageOps.exif_transpose(reference_image)
                    reference_width, reference_height = reference_image.size
                first_uploaded_file.seek(0)
                compressed_images = [uploaded_file_to_compressed_data_url(file) for file in all_reference_files]
                image_urls = [item[0] for item in compressed_images]
                compression_stats = [item[1] for item in compressed_images]
                original_total_bytes = sum(item["original_bytes"] for item in compression_stats)
                compressed_total_bytes = sum(item["compressed_bytes"] for item in compression_stats)
                st.caption(
                    f"参考图已压缩：{format_bytes(original_total_bytes)} -> "
                    f"{format_bytes(compressed_total_bytes)}"
                )
                try:
                    image_size = resolve_output_size(
                        size_choice, (reference_width, reference_height), gemini=is_gemini,
                    )
                    request_url, payload = build_image_request(
                        selected_model, edit_prompt, image_size, image_urls,
                        quality=quality, image_size=image_size_tier,
                    )
                except ValueError as exc:
                    st.error(str(exc))
                    st.stop()
                if not is_gemini and is_experimental_size(image_size):
                    st.warning("当前尺寸超过 2560x1440 像素量，属于 experimental 范围，生成可能更慢或不稳定。")
                progress_bar = st.progress(0)
                status_placeholder = st.empty()
                try:
                    res_data = generate_image_task(
                        payload,
                        lambda status, progress: update_task_progress(
                            progress_bar, status_placeholder, status, progress
                        ),
                        request_url=request_url,
                    )

                    end_time = time.time()
                    elapsed_time = round(end_time - start_time, 2)

                    progress_bar.progress(100)
                    status_placeholder.caption("任务状态：completed")
                    image_data = get_first_image_data(res_data)
                    img_bytes = fetch_image_bytes(image_data)
                    extension, mime, actual_size, image_format = get_download_info(img_bytes)

                    st.success(f"🎉 重绘成功！模型 {selected_model}，参考图 {len(image_urls)} 张，实际尺寸 {actual_size[0]}×{actual_size[1]}，共耗时 {elapsed_time} 秒")
                    st.caption(f"请求画幅 / 尺寸：{image_size}" + (f" · 分辨率档位：{image_size_tier}" if is_gemini else f" · 质量：{quality}") + f" · {image_format}")
                    st.image(img_bytes, use_container_width=True)
                    st.download_button(label="📥 下载重绘后的图片", data=img_bytes,
                                       file_name=f"edited_image_{int(time.time())}.{extension}", mime=mime)

                except requests.exceptions.Timeout:
                    st.error("❌ 网络请求超时了，请稍后重试。")
                except TimeoutError as e:
                    st.error(f"❌ {e}")
                except Exception as e:
                    st.error(f"❌ 发生异常：{e}")
