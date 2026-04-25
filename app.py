import streamlit as st
import requests
import base64

# 【修改点 1】：配置你的接口地址（这里直接写死没关系，只要不暴露 Key 就行）
API_BASE = "https://你的接口域名/gpt/v1"

# 【修改点 2】：通过 Streamlit 的秘密环境变量读取 Key，而不是直接写在代码里
try:
    API_KEY = st.secrets["MY_API_KEY"]
except KeyError:
    st.error("未找到 API Key，请在 Streamlit Secrets 或本地 .streamlit/secrets.toml 中配置 MY_API_KEY")
    st.stop()

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

st.set_page_config(page_title="AI 绘图助手", page_icon="🎨")
st.title("🎨 AI 绘图助手")

# 使用标签页区分“文生图”和“图生图”
tab1, tab2 = st.tabs(["📝 文字生图", "🖼️ 图片重绘"])

with tab1:
    prompt = st.text_area("请输入画面描述：", "画一个Sam在抖音直播间带货 Right Code 的图片")
    if st.button("生成图片", type="primary"):
        with st.spinner("AI 正在疯狂作画中，请稍候..."):
            payload = {
                "model": "gpt-image-2",
                "prompt": prompt
            }
            try:
                response = requests.post(f"{API_BASE}/images/generations", json=payload, headers=HEADERS)
                response.raise_for_status()
                res_data = response.json()

                # 提取 Base64 并在前端显示
                b64_str = res_data["data"][0]["b64_json"]
                image_bytes = base64.b64decode(b64_str)

                # st.image 渲染的图片，在电脑端可右键保存，手机端可长按保存
                st.image(image_bytes, caption="生成成功！", use_column_width=True)

                # 提供一个显式的下载按钮
                st.download_button(label="📥 下载图片", data=image_bytes, file_name="ai_image.png", mime="image/png")
            except Exception as e:
                st.error(f"生成失败: {e}")

with tab2:
    st.info("图生图功能示例（OpenAI Compatible 接口）")
    uploaded_file = st.file_uploader("上传参考图", type=["png", "jpg", "jpeg"])
    edit_prompt = st.text_input("修改指令", "改成水彩画风")

    if st.button("开始重绘") and uploaded_file is not None:
        with st.spinner("AI 正在重绘..."):
            # 将上传的图片转为 base64
            base64_image = base64.b64encode(uploaded_file.getvalue()).decode('utf-8')
            image_url = f"data:image/png;base64,{base64_image}"

            payload = {
                "model": "gpt-image-2",
                "messages":[
                    {
                        "role": "user",
                        "content":[
                            {"type": "text", "text": edit_prompt},
                            {"type": "image_url", "image_url": {"url": image_url}}
                        ]
                    }
                ]
            }
            try:
                res = requests.post(f"{API_BASE}/chat/completions", json=payload, headers=HEADERS)
                res.raise_for_status() # 【修改点 3】增加报错拦截，方便排查网络问题
                res_data = res.json()
                content = res_data["choices"][0]["message"]["content"]

                if "data:image" in content:
                    img_base64 = content.split("base64,")[1].split(")")[0]
                    img_bytes = base64.b64decode(img_base64)
                    st.image(img_bytes, caption="重绘成功！", use_column_width=True)
            except Exception as e:
                st.error(f"请求报错：{e}")