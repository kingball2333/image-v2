import streamlit as st
import requests
import base64
import time  # 【新增】引入时间模块用于计算耗时
from PIL import Image

API_BASE = "https://www.right.codes/draw/v1"
MIN_PIXELS = 655_360
MAX_PIXELS = 8_294_400
MAX_EDGE = 3840
MAX_RATIO = 3
EXPERIMENTAL_PIXELS = 2560 * 1440
SIZE_OPTIONS = {
    "自动默认": None,
    "方形 1024x1024": "1024x1024",
    "竖版 1024x1536": "1024x1536",
    "横版 1536x1024": "1536x1024",
    "2K 方形 2048x2048（一般不建议）": "2048x2048",
    "2K 横版 2048x1152（一般不建议）": "2048x1152",
    "长海报 720x2160": "720x2160",
}

import os

# 【修改点】：双保险读取 Key。先尝试 Zeabur 的方式，如果不行再尝试 Streamlit 的方式
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
            return response.json()
        except ValueError:
            pass

    text = response.text.strip()
    if text.lower().startswith("<!doctype html") or text.lower().startswith("<html"):
        return "服务器返回了 HTML 错误页，请稍后重试或检查中转站状态。"

    return text


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


st.set_page_config(page_title="AI 绘图助手", page_icon="🎨")
st.title("🎨 AI 绘图助手 Made BY ljj（5-12-1）")
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
                    "image": [],
                    "size": image_size,
                    "response_format": "url"
                }
                try:
                    # 强烈建议加上 timeout，防止网络卡死
                    response = requests.post(f"{API_BASE}/images/generations", json=payload, headers=HEADERS,
                                             timeout=360)

                    # 【优化】更优雅的错误处理，直接显示官方返回的错误内容
                    if not response.ok:
                        err_msg = format_error_message(response)
                        st.error(f"❌ 生成失败！状态码: {response.status_code}\n\n服务器返回信息:\n{err_msg}")
                        st.stop()  # 终止后续代码执行

                    res_data = response.json()

                    # 计算耗时
                    end_time = time.time()
                    elapsed_time = round(end_time - start_time, 2)

                    # 提取数据
                    image_data = res_data["data"][0]
                    revised_prompt = image_data.get("revised_prompt", "原样回显（模型未优化）")
                    image_bytes = fetch_image_bytes(image_data)

                    # 【优化】显示成功提示、耗时和优化的提示词
                    st.success(f"🎉 生成成功！尺寸 {image_size}，共耗时 {elapsed_time} 秒")
                    with st.expander("💡 查看 AI 优化后的提示词 (点击展开)", expanded=True):
                        st.info(revised_prompt)

                    # 渲染图片
                    st.image(image_bytes, use_column_width=True)
                    st.download_button(label="📥 下载图片", data=image_bytes,
                                       file_name=f"ai_image_{int(time.time())}.png", mime="image/png")

                except requests.exceptions.Timeout:
                    st.error("❌ 请求超时了！可能是因为图片太复杂或者网络拥堵，请稍后再试。")
                except Exception as e:
                    st.error(f"❌ 请求发生异常：{e}")

with tab2:
    st.info("图生图功能：上传参考图并输入修改指令")
    uploaded_file = st.file_uploader("上传参考图", type=["png", "jpg", "jpeg"])
    edit_prompt = st.text_input("修改指令", placeholder="例如：改成水彩画风")

    if st.button("开始重绘"):
        if uploaded_file is None:
            st.warning("⚠️ 请先上传一张参考图片！")
        elif not edit_prompt.strip():
            st.warning("⚠️ 请输入你要修改的指令！")
        else:
            with st.spinner("AI 正在重绘..."):
                start_time = time.time()
                uploaded_file.seek(0)
                reference_image = Image.open(uploaded_file)
                reference_width, reference_height = reference_image.size
                uploaded_file.seek(0)
                base64_image = base64.b64encode(uploaded_file.getvalue()).decode('utf-8')
                image_url = f"data:{uploaded_file.type};base64,{base64_image}"
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
                    "image": [image_url],
                    "size": image_size,
                    "response_format": "url"
                }
                try:
                    res = requests.post(f"{API_BASE}/images/generations", json=payload, headers=HEADERS, timeout=360)

                    if not res.ok:
                        err_msg = format_error_message(res)
                        st.error(f"❌ 重绘失败！状态码: {res.status_code}\n\n服务器返回: {err_msg}")
                        st.stop()

                    res_data = res.json()

                    end_time = time.time()
                    elapsed_time = round(end_time - start_time, 2)

                    image_data = res_data["data"][0]
                    img_bytes = fetch_image_bytes(image_data)

                    st.success(f"🎉 重绘成功！尺寸 {image_size}，共耗时 {elapsed_time} 秒")
                    st.image(img_bytes, use_column_width=True)
                    st.download_button(label="📥 下载重绘后的图片", data=img_bytes,
                                       file_name=f"edited_image_{int(time.time())}.png", mime="image/png")

                except requests.exceptions.Timeout:
                    st.error("❌ 请求超时！图片处理较慢，请重试。")
                except Exception as e:
                    st.error(f"❌ 发生异常：{e}")
