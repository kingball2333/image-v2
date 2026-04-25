import streamlit as st
import requests
import base64
import time  # 【新增】引入时间模块用于计算耗时

API_BASE = "https://right.codes/gpt/v1"

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

st.set_page_config(page_title="AI 绘图助手", page_icon="🎨")
st.title("🎨 AI 绘图助手 BY ljj")

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
                payload = {
                    "model": "gpt-image-2",
                    "prompt": prompt
                }
                try:
                    # 强烈建议加上 timeout，防止网络卡死
                    response = requests.post(f"{API_BASE}/images/generations", json=payload, headers=HEADERS,
                                             timeout=60)

                    # 【优化】更优雅的错误处理，直接显示官方返回的错误内容
                    if not response.ok:
                        err_msg = response.text
                        try:
                            # 尝试解析成更容易阅读的 JSON
                            err_msg = response.json()
                        except:
                            pass
                        st.error(f"❌ 生成失败！状态码: {response.status_code}\n\n服务器返回信息:\n{err_msg}")
                        st.stop()  # 终止后续代码执行

                    res_data = response.json()

                    # 计算耗时
                    end_time = time.time()
                    elapsed_time = round(end_time - start_time, 2)

                    # 提取数据
                    b64_str = res_data["data"][0]["b64_json"]
                    revised_prompt = res_data["data"][0].get("revised_prompt", "原样回显（模型未优化）")
                    image_bytes = base64.b64decode(b64_str)

                    # 【优化】显示成功提示、耗时和优化的提示词
                    st.success(f"🎉 生成成功！共耗时 {elapsed_time} 秒")
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
                base64_image = base64.b64encode(uploaded_file.getvalue()).decode('utf-8')
                image_url = f"data:image/png;base64,{base64_image}"

                payload = {
                    "model": "gpt-image-2",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": edit_prompt},
                                {"type": "image_url", "image_url": {"url": image_url}}
                            ]
                        }
                    ]
                }
                try:
                    res = requests.post(f"{API_BASE}/chat/completions", json=payload, headers=HEADERS, timeout=60)

                    if not res.ok:
                        st.error(f"❌ 重绘失败！状态码: {res.status_code}\n\n服务器返回: {res.text}")
                        st.stop()

                    res_data = res.json()

                    end_time = time.time()
                    elapsed_time = round(end_time - start_time, 2)

                    content = res_data["choices"][0]["message"]["content"]

                    if "data:image" in content:
                        img_base64 = content.split("base64,")[1].split(")")[0]
                        img_bytes = base64.b64decode(img_base64)

                        st.success(f"🎉 重绘成功！共耗时 {elapsed_time} 秒")
                        st.image(img_bytes, use_column_width=True)
                        st.download_button(label="📥 下载重绘后的图片", data=img_bytes,
                                           file_name=f"edited_image_{int(time.time())}.png", mime="image/png")
                    else:
                        st.warning("⚠️ 接口调用成功，但未能在返回值中找到图片数据。原始返回：")
                        st.write(content)

                except requests.exceptions.Timeout:
                    st.error("❌ 请求超时！图片处理较慢，请重试。")
                except Exception as e:
                    st.error(f"❌ 发生异常：{e}")