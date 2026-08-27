"""浏览器剪切板图片输入组件及 Streamlit 会话状态封装。"""

import base64
import binascii
from io import BytesIO
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components


_COMPONENT_DIR = Path(__file__).with_name("clipboard_image_component")
_paste_image_component = components.declare_component(
    "clipboard_image_paste",
    path=str(_COMPONENT_DIR),
)


class ClipboardImageFile(BytesIO):
    """让剪切板图片具备与 Streamlit UploadedFile 相同的最小接口。"""

    def __init__(self, content, name, mime_type):
        super().__init__(content)
        self.name = name
        self.type = mime_type
        self.size = len(content)


def _read_component_value(component_key):
    value = _paste_image_component(
        key=component_key,
        default=None,
    )
    if not isinstance(value, dict):
        return None
    if not value.get("data") or not value.get("timestamp"):
        return None
    return value


def get_pasted_images(state_key):
    """读取新粘贴的图片，并在当前会话中去重保存。"""
    images_key = f"{state_key}_images"
    timestamp_key = f"{state_key}_timestamp"
    if images_key not in st.session_state:
        st.session_state[images_key] = []
    if timestamp_key not in st.session_state:
        st.session_state[timestamp_key] = 0

    value = _read_component_value(f"{state_key}_component")
    if value and value["timestamp"] > st.session_state[timestamp_key]:
        try:
            content = base64.b64decode(value["data"], validate=True)
        except (binascii.Error, ValueError):
            st.warning("剪切板中的图片数据无效，请重新复制后再试。")
        else:
            mime_type = value.get("type") or "image/png"
            extension = mime_type.rsplit("/", 1)[-1].replace("jpeg", "jpg")
            name = value.get("name") or f"clipboard_{value['timestamp']}.{extension}"
            st.session_state[images_key].append(
                ClipboardImageFile(content, name, mime_type)
            )
            st.session_state[timestamp_key] = value["timestamp"]

    return list(st.session_state[images_key])


def clear_pasted_images(state_key):
    """清空当前页面通过剪切板粘贴的图片。"""
    st.session_state[f"{state_key}_images"] = []


def render_paste_image_control(state_key):
    """渲染剪切板输入控件并返回当前会话中的粘贴图片。"""
    st.caption("也可以点击下方区域后按 Ctrl+V 粘贴图片（macOS 使用 ⌘V）")
    pasted_images = get_pasted_images(state_key)
    if pasted_images and st.button(
        "清空已粘贴图片",
        key=f"{state_key}_clear",
        help="只清空通过剪切板粘贴的图片，不影响文件上传图片。",
    ):
        clear_pasted_images(state_key)
        st.rerun()
    return pasted_images
