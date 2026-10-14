# 旧中转多模型配置

适配依据：2026-10-14 的 RightAPI 绘图文档。

- 总览：https://docs.rightapi.ai/docs/rc_draw/
- GPT：https://docs.rightapi.ai/docs/rc_draw/openai.html
- Gemini：https://docs.rightapi.ai/docs/rc_draw/gemini.html
- 任务查询：https://docs.rightapi.ai/docs/rc_draw/tasks.html

## Streamlit 部署

旧中转继续从 Streamlit Secrets 的 `MY_API_KEY` 或同名环境变量读取密钥。
公司页面继续使用独立的 `COMPANY_API_KEY`。

提交时必须包含 `app.py` 和新增的 `rightapi_models.py`；测试文件也建议一并提交。
无需安装新依赖或修改 `requirements.txt`。

默认地址：

| 模型 | 地址前缀 | 图生图方式 |
| --- | --- | --- |
| GPT Image 2 VIP、原 GPT Image 2.5 | `https://www.rightapi.ai/draw` | 原 `/v1/images/generations` + `image` 数组 |
| GPT Image 2、2.5 Flare、2.5 Sunburst | `https://www.rightapi.ai/image-pro` | `/v1/images/edits` + `image` 数组 |
| Nano Banana Pro、Nano Banana 2.1 | `https://www.rightapi.ai/image-pro` | `/v1beta/models/{model}:generateContent` + `inline_data` |

默认保留渠道前缀，因为现有旧版密钥访问统一 `/v1/models` 时，中转站要求继续使用渠道前缀。
`/draw/v1/models` 与 `/image-pro/v1/models` 的只读查询均成功。

新模型并不保证所有账号均有权限，最终以密钥权限和渠道状态为准。

## 可选地址覆盖

仅在实际更换密钥类型或中转地址后调整；现有部署无需修改。
以下配置是**环境变量**，不是普通 Streamlit Secrets 键：

- `RIGHTAPI_DRAW_BASE_URL`：原模型地址，默认 `https://www.rightapi.ai/draw`。
- `RIGHTAPI_IMAGE_PRO_BASE_URL`：新版模型地址，默认 `https://www.rightapi.ai/image-pro`。
- `RIGHTAPI_TASKS_BASE_URL`：任务查询地址，默认 `https://www.rightapi.ai/v1/tasks`。

若换成绑定新版绘图渠道的新 Key，按中转站要求可将
`RIGHTAPI_IMAGE_PRO_BASE_URL` 设为 `https://www.rightapi.ai`（不加 `/v1`）。
不要只修改原 `RIGHTAPI_DRAW_BASE_URL` 来切换新版模型。

## 模型参数

- 新版 GPT 使用像素尺寸，支持 4K 横图与竖图；大图属于实验性范围。
- GPT Image 2 支持 `auto/low/medium/high`；Flare 和 Sunburst 另有 `xhigh/max`。
- 原两个模型保留原参数，不额外发送尚未确认支持的质量档位。
- Gemini 使用画幅比例与 `1K/2K/4K` 档位；自动画幅按首张参考图匹配。
- 所有模型保留多图参考、剪贴板输入及小图预览；输出显示实际尺寸，并按实际格式下载。
- 不提供 mask 或透明背景开关，因为新版中转文档明确暂不支持。

## 验证

```powershell
python -m unittest discover -s tests -v
python -m py_compile app.py rightapi_models.py company_image_page.py clipboard_image.py
git diff --check
```

测试覆盖 7 个模型各自的文生图和多图重绘请求、渠道地址、质量限制、
自动画幅、Gemini URL 解析、终止状态以及公司页面切换。
测试替换全部 HTTP 调用，不产生生图费用；此次新版模型未做付费出图验证。
