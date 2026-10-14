"""共享的生成结果、会话版本记录和继续调整界面，不负责中转站协议。"""

import binascii
from io import BytesIO
import time
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
import requests
import streamlit as st

from clipboard_image import ClipboardImageFile, render_paste_image_control


MAX_RESULT_VERSIONS = 5


def get_download_info(image_bytes):
    with Image.open(BytesIO(image_bytes)) as image:
        image_format = (image.format or "PNG").lower()
        image_size = image.size
        image.verify()
    if image_format == "jpeg":
        return "jpg", "image/jpeg", image_size, "JPEG"
    if image_format == "webp":
        return "webp", "image/webp", image_size, "WebP"
    return "png", "image/png", image_size, "PNG"


def save_image_result(state_key, image_bytes, *, model, prompt, elapsed_seconds,
                      caption=None, revised_prompt=None, parent_id=None):
    """验证图片后再存入会话；失败不会覆盖已保存的结果，两页面使用不同键。"""
    extension, mime, size, image_format = get_download_info(image_bytes)
    version_id = uuid4().hex
    result = {
        "id": version_id, "image_bytes": image_bytes, "mime": mime,
        "size": size, "format": image_format,
        "filename": f"{state_key}_{int(time.time())}_{version_id[:8]}.{extension}",
        "model": model, "prompt": prompt, "caption": caption,
        "elapsed_seconds": elapsed_seconds, "revised_prompt": revised_prompt,
        "parent_id": parent_id,
    }
    history_key = f"{state_key}_results"
    history = st.session_state.get(history_key, [])
    result["version"] = history[0]["version"] + 1 if history else 1
    st.session_state[history_key] = [result, *history][:MAX_RESULT_VERSIONS]
    for discarded in history[MAX_RESULT_VERSIONS - 1:]:
        prefix = f"{state_key}_refine_{discarded['id']}_"
        for key in list(st.session_state):
            if key.startswith(prefix):
                del st.session_state[key]
    return result


def refinement_prompt(instruction):
    """以当前结果为修改基础；不把上一轮文字要求或附加参考图隐式重复发送。"""
    return (
        "第一张参考图是本轮待修改的生成结果，请以它为基础进行调整，"
        "保留没有要求修改的内容。若有其他参考图，它们从第二张开始。\n"
        f"本轮修改要求：{instruction.strip()}"
    )


def render_image_results(state_key, *, model, refine_image, max_reference_images=16):
    """展示持久结果；回调接收新指令和文件，复用各页面已有图生图流程。"""
    history = st.session_state.get(f"{state_key}_results", [])
    if not history:
        return
    st.divider()
    st.subheader("生成结果 · 继续调整")
    results = {result["id"]: result for result in history}
    selected_id = st.selectbox(
        "选择结果版本", list(results),
        format_func=lambda item: (
            f"版本 {results[item]['version']} · {results[item]['model']}"
            + ("（最新）" if item == history[0]["id"] else "")
        ),
        key=f"{state_key}_version_{history[0]['id']}",
    )
    result = results[selected_id]
    st.success(
        f"生成成功！模型 {result['model']}，实际尺寸 "
        f"{result['size'][0]}×{result['size'][1]}，耗时 {result['elapsed_seconds']} 秒"
    )
    if result.get("caption"):
        st.caption(result["caption"])
    st.image(result["image_bytes"], use_container_width=True)
    st.download_button(
        "下载这张图片", data=result["image_bytes"], file_name=result["filename"],
        mime=result["mime"], key=f"{state_key}_download_{selected_id}",
    )
    with st.expander("查看本轮提示词"):
        st.write(result["prompt"])
        if result.get("revised_prompt"):
            st.caption("模型优化后的提示词")
            st.write(result["revised_prompt"])
    st.caption(
        f"在当前浏览器会话中保留最近 {MAX_RESULT_VERSIONS} 个版本，两个页面分别保存。"
        "重新打开或会话结束后可能丢失，请及时下载需要保留的图片。"
    )

    # 每个结果的输入使用独立键；新版本成功后输入重置，不会重复带上上一轮补充图。
    controls_key = f"{state_key}_refine_{selected_id}"
    with st.expander("继续调整这张图片", expanded=True):
        st.caption(
            f"上面选中的结果将自动作为第一张参考图；本轮使用模型 {model}，"
            "尺寸和质量按页面当前选择。新增参考图从第二张开始。"
        )
        instruction = st.text_area(
            "补充修改要求", placeholder="例如：保留人物和构图，把背景换成夜景；参考第二张图的配色。",
            key=f"{controls_key}_instruction",
        )
        uploads = st.file_uploader(
            "补充参考图（可选）", type=["png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True, key=f"{controls_key}_uploads",
            help=f"当前结果占用 1 个参考图名额，还可补充 {max_reference_images - 1} 张。",
        )
        pasted = render_paste_image_control(controls_key)
        extras = list(uploads or []) + pasted
        st.caption(f"本轮参考图：当前结果 1 张 + 补充 {len(extras)} 张 / 上限 {max_reference_images} 张")
        if extras:
            columns = st.columns(4)
            for index, file in enumerate(extras[:4]):
                with columns[index]:
                    st.image(file.getvalue(), caption=f"参考图 {index + 2} · {file.name}", width=160)
            if len(extras) > 4:
                st.caption(f"还有 {len(extras) - 4} 张补充图未预览")
        if st.button("根据这张图继续生成", type="primary", key=f"{controls_key}_submit"):
            if not instruction.strip():
                st.warning("请输入本轮修改要求；有补充图片时，也请说明图片的用途。")
            elif 1 + len(extras) > max_reference_images:
                st.warning(f"包含当前结果，最多支持 {max_reference_images} 张参考图，请减少补充图片。")
            else:
                base_file = ClipboardImageFile(result["image_bytes"], result["filename"], result["mime"])
                try:
                    with st.spinner("正在根据选中的结果继续调整..."):
                        generated = refine_image(refinement_prompt(instruction), [base_file, *extras])
                        save_image_result(
                            state_key, prompt=instruction.strip(), parent_id=selected_id, **generated,
                        )
                except requests.exceptions.Timeout:
                    st.error("请求超时，上一版图片已保留。任务可能仍在处理中，请先检查任务状态再重试。")
                except requests.exceptions.RequestException as exc:
                    st.error(f"网络请求失败，上一版图片已保留：{exc}")
                except (RuntimeError, ValueError, binascii.Error, UnidentifiedImageError) as exc:
                    st.error(f"本轮调整失败，上一版图片已保留：{exc}")
                else:
                    st.rerun()
