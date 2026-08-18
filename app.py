import streamlit as st
import requests
import base64
import os
import time
from io import BytesIO
from PIL import Image, ImageOps

# Right Code 画图接口现在使用异步任务模式：提交地址带 /draw，查询地址不带 /draw。
# 保留环境变量覆盖，方便在不同中转站或测试环境中切换。
API_BASE = os.environ.get("RIGHTAPI_DRAW_BASE_URL", "https://www.rightapi.ai/draw").rstrip("/")
GENERATIONS_URL = f"{API_BASE}/v1/images/generations"
TASKS_BASE_URL = os.environ.get(
    "RIGHTAPI_TASKS_BASE_URL",
    "https://www.rightapi.ai/v1/tasks",
).rstrip("/")
POLL_INTERVAL_SECONDS = 2
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
}

# 双保险读取 Key：先读取部署环境变量，再读取 Streamlit secrets。
API_KEY = os.environ.get("MY_API_KEY")
if not API_KEY:
    try:
        API_KEY = st.secrets["MY_API_KEY"]
    except KeyError:
        st.error("未找到 API Key，请配置环境变量 MY_API_KEY")
        st.stop()

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
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


def generate_image_task(payload, progress_callback=None):
    """提交异步绘图任务并轮询至完成，返回 Images 兼容的结果 JSON。"""
    response = requests.post(
        GENERATIONS_URL,
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

    # 兼容中转站直接返回完成结果的情况。
    task_id = task_data.get("task_id")
    if not task_id:
        if task_data.get("data"):
            return task_data
        raise RuntimeError(f"提交绘图任务失败：响应中没有 task_id。返回内容：{format_error_payload(task_data)}")

    pending_statuses = {"processing", "pending", "queued", "in_progress"}
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = task_data.get("status")
        progress = task_data.get("progress")
        if progress_callback:
            progress_callback(status or "processing", progress)

        if status == "completed" or task_data.get("data"):
            return task_data
        if status == "failed":
            raise RuntimeError(f"绘图任务失败：{format_error_payload(task_data)}")
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
        f"绘图任务超过 {POLL_TIMEOUT_SECONDS // 60} 分钟仍未完成，请稍后到中转站检查任务状态。"
    )


def update_task_progress(progress_bar, status_placeholder, status, progress):
    """更新 Streamlit 中的异步任务进度，不影响旧版 Streamlit。"""
    if isinstance(progress, (int, float)):
        progress_bar.progress(max(0, min(100, int(progress))))
    if status:
        progress_text = f"（{int(progress)}%）" if isinstance(progress, (int, float)) else ""
        status_placeholder.caption(f"任务状态：{status}{progress_text}")


def get_first_image_data(result):
    """校验并取出 Images 兼容响应中的第一张图片。"""
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError("任务已完成，但返回数据中没有可用的图片结果。")
    return data[0]


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


st.set_page_config(
    page_title="AI 绘图助手",
    page_icon="🎨",
    initial_sidebar_state="expanded",
)
st.title("🎨 AI 绘图助手 Made BY ljj（异步接口版08-17-1）")
old_page_link, company_page_link = st.columns(2)
with old_page_link:
    st.button(
        "旧中转生图",
        icon="🎨",
        disabled=True,
        width="stretch",
    )
with company_page_link:
    if st.button("公司生图", icon="🏢", width="stretch"):
        st.switch_page("pages/1_公司生图.py")
size_choice = st.sidebar.selectbox("图片尺寸（分辨率越高生成时间越长失败可能性越大哈）", list(SIZE_OPTIONS.keys()), index=0)
st.sidebar.caption("自动默认：文生图生成 1024x1024 方图；图生图按参考图比例自动修正到模型支持尺寸。")

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
                image_size = SIZE_OPTIONS[size_choice] or "1024x1024"
                size_error = validate_image_size(image_size)
                if size_error:
                    st.error(f"当前尺寸 {image_size} 不符合模型要求：{size_error}")
                    st.stop()
                if is_experimental_size(image_size):
                    st.warning("当前尺寸超过 2560x1440 像素量，属于 experimental 范围，生成可能更慢或不稳定。")
                payload = {
                    "model": "gpt-image-2-vip",
                    "prompt": prompt,
                    "n": 1,
                    "size": image_size,
                    "async": True,
                }
                progress_bar = st.progress(0)
                status_placeholder = st.empty()
                try:
                    res_data = generate_image_task(
                        payload,
                        lambda status, progress: update_task_progress(
                            progress_bar, status_placeholder, status, progress
                        ),
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

                    # 【优化】显示成功提示、耗时和优化的提示词
                    st.success(f"🎉 生成成功！尺寸 {image_size}，共耗时 {elapsed_time} 秒")
                    with st.expander("💡 查看 AI 优化后的提示词 (点击展开)", expanded=True):
                        st.info(revised_prompt)

                    # 渲染图片
                    st.image(image_bytes, width="stretch")
                    st.download_button(label="📥 下载图片", data=image_bytes,
                                       file_name=f"ai_image_{int(time.time())}.png", mime="image/png")

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
    if uploaded_files:
        st.caption(f"已选择 {len(uploaded_files)} / {MAX_REFERENCE_IMAGES} 张参考图")
        preview_columns = st.columns(min(len(uploaded_files), 4))
        for index, file in enumerate(uploaded_files[:4]):
            with preview_columns[index % len(preview_columns)]:
                st.image(file, caption=file.name, width="stretch")
        if len(uploaded_files) > 4:
            st.caption(f"还有 {len(uploaded_files) - 4} 张未预览")

    edit_prompt = st.text_input("修改指令", placeholder="例如：改成水彩画风")

    if st.button("开始重绘"):
        if not uploaded_files:
            st.warning("⚠️ 请先上传至少一张参考图片！")
        elif len(uploaded_files) > MAX_REFERENCE_IMAGES:
            st.warning(f"⚠️ 参考图最多支持 {MAX_REFERENCE_IMAGES} 张，请减少后再试。")
        elif not edit_prompt.strip():
            st.warning("⚠️ 请输入你要修改的指令！")
        else:
            with st.spinner("AI 正在重绘..."):
                start_time = time.time()
                first_uploaded_file = uploaded_files[0]
                first_uploaded_file.seek(0)
                with Image.open(first_uploaded_file) as reference_image:
                    reference_image = ImageOps.exif_transpose(reference_image)
                    reference_width, reference_height = reference_image.size
                first_uploaded_file.seek(0)
                compressed_images = [uploaded_file_to_compressed_data_url(file) for file in uploaded_files]
                image_urls = [item[0] for item in compressed_images]
                compression_stats = [item[1] for item in compressed_images]
                original_total_bytes = sum(item["original_bytes"] for item in compression_stats)
                compressed_total_bytes = sum(item["compressed_bytes"] for item in compression_stats)
                st.caption(
                    f"参考图已压缩：{format_bytes(original_total_bytes)} -> "
                    f"{format_bytes(compressed_total_bytes)}"
                )
                image_size = SIZE_OPTIONS[size_choice] or normalize_reference_size(reference_width, reference_height)
                size_error = validate_image_size(image_size)
                if size_error:
                    st.error(f"当前尺寸 {image_size} 不符合模型要求：{size_error}")
                    st.stop()
                if is_experimental_size(image_size):
                    st.warning("当前尺寸超过 2560x1440 像素量，属于 experimental 范围，生成可能更慢或不稳定。")

                payload = {
                    "model": "gpt-image-2-vip",
                    "prompt": edit_prompt,
                    "image": image_urls,
                    "n": 1,
                    "size": image_size,
                    "async": True,
                }
                progress_bar = st.progress(0)
                status_placeholder = st.empty()
                try:
                    res_data = generate_image_task(
                        payload,
                        lambda status, progress: update_task_progress(
                            progress_bar, status_placeholder, status, progress
                        ),
                    )

                    end_time = time.time()
                    elapsed_time = round(end_time - start_time, 2)

                    progress_bar.progress(100)
                    status_placeholder.caption("任务状态：completed")
                    image_data = get_first_image_data(res_data)
                    img_bytes = fetch_image_bytes(image_data)

                    st.success(f"🎉 重绘成功！参考图 {len(image_urls)} 张，尺寸 {image_size}，共耗时 {elapsed_time} 秒")
                    st.image(img_bytes, width="stretch")
                    st.download_button(label="📥 下载重绘后的图片", data=img_bytes,
                                       file_name=f"edited_image_{int(time.time())}.png", mime="image/png")

                except requests.exceptions.Timeout:
                    st.error("❌ 网络请求超时了，请稍后重试。")
                except TimeoutError as e:
                    st.error(f"❌ {e}")
                except Exception as e:
                    st.error(f"❌ 发生异常：{e}")
