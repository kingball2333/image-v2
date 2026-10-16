# 旧中转多模型配置

适配依据：2026-10-14 的 RightAPI 绘图文档。

- 总览：https://docs.rightapi.ai/docs/rc_draw/
- GPT：https://docs.rightapi.ai/docs/rc_draw/openai.html
- Gemini：https://docs.rightapi.ai/docs/rc_draw/gemini.html
- 任务查询：https://docs.rightapi.ai/docs/rc_draw/tasks.html

## Streamlit 部署

旧中转继续从 Streamlit Secrets 的 `MY_API_KEY` 或同名环境变量读取密钥。
公司页面继续使用独立的 `COMPANY_API_KEY`。

提交时必须包含 `app.py`、`company_image_page.py`、`rightapi_models.py` 和 `image_results.py`；测试文件也建议一并提交。
无需安装新依赖或修改 `requirements.txt`。

默认地址：

| 模型 | 地址前缀 | 图生图方式 |
| --- | --- | --- |
| GPT Image 2 VIP、原 GPT Image 2.5 | `https://www.rightapi.ai/draw` | 原 `/v1/images/generations` + `image` 数组 |
| GPT Image 2、2.5 Flare、2.5 Sunburst | `https://www.rightapi.ai/draw-pro` | `/v1/images/edits` + `image` 数组 |
| Nano Banana Pro、Nano Banana 2.1 | `https://www.rightapi.ai/draw-pro` | `/v1beta/models/{model}:generateContent` + `inline_data` |

默认保留渠道前缀，因为现有旧版密钥访问统一 `/v1/models` 时，中转站要求继续使用渠道前缀。
2026-10-16 查询确认：新版渠道前缀从 `/image-pro` 改为 `/draw-pro`。
`/draw/v1/models` 与 `/draw-pro/v1/models` 的只读查询均成功，原 `/image-pro/v1/models` 返回 404。

新模型并不保证所有账号均有权限，最终以密钥权限和渠道状态为准。

## 可选地址覆盖

仅在实际更换密钥类型或中转地址后调整；现有部署无需修改。
以下配置是**环境变量**，不是普通 Streamlit Secrets 键：

- `RIGHTAPI_DRAW_BASE_URL`：原模型地址，默认 `https://www.rightapi.ai/draw`。
- `RIGHTAPI_IMAGE_PRO_BASE_URL`：新版模型地址，默认 `https://www.rightapi.ai/draw-pro`。

若以前手动设置过 `RIGHTAPI_IMAGE_PRO_BASE_URL=https://www.rightapi.ai/image-pro`，
需要改成 `https://www.rightapi.ai/draw-pro` 或移除此覆盖，让代码使用新默认值。
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

两个页面的文生图、图生图结果均保存到当前浏览器会话。
结果下方可选择最近 5 个版本，填写补充修改要求、上传或粘贴额外参考图，
点击“根据这张图继续生成”。选中结果自动作为第一张参考图，补充图按顺序排在后面。
每轮成功后选中新版本，并重置新一轮的输入；失败保留已有版本。
旧中转沿用页面当前的模型、尺寸和质量，公司页面沿用公司接口和尺寸设置。
不会隐式重发原始参考图或上一轮补充图，需要继续使用时请在本轮再次添加。
每次点击继续生成会进行一次新的图生图调用，按对应中转站计费。
这不是服务端聊天记忆：每轮以选中结果图片和本轮要求进行编辑。
会话结束、重新打开页面或服务重启后记录可能丢失，需要长期保存的图片请下载。

```powershell
python -m unittest discover -s tests -v
python -m py_compile app.py rightapi_models.py company_image_page.py clipboard_image.py image_results.py
git diff --check
```

测试覆盖 7 个模型各自的文生图和多图重绘请求、渠道地址、质量限制、
自动画幅、Gemini URL 解析、终止状态以及公司页面切换。
连续调整回归测试还覆盖新结果逐轮作为首张参考图、附加图片不会跨轮重复、
失败保留结果、参考图总数限制、历史版本上限以及跨页面隔离。
自动测试替换全部 HTTP 调用，不产生生图费用。

### 2026-10-16 付费实测

另行使用本机 `MY_API_KEY` 和项目的真实请求构造、提交、轮询、图片解析函数，
提交 30 次绘图请求：29 次成功，原 VIP 模型 1 次返回 `generate failed`；
其图生图因无成功基准图跳过，未自动重试失败任务。

| 模型 | 文生图实测 | 双参考图重绘 |
| --- | --- | --- |
| 原 GPT Image 2.5 | 原有参数成功，不传额外 quality | 成功 |
| GPT Image 2 | auto / low / medium / high 全部成功 | auto 成功 |
| GPT Image 2.5 Flare | auto / low / medium / high / xhigh / max 全部成功 | high 成功 |
| GPT Image 2.5 Sunburst | auto / low / medium / high / xhigh / max 全部成功 | high 成功 |
| Nano Banana Pro | 1K / 2K / 4K 全部成功 | 1K 成功 |
| Nano Banana 2.1 | 1K / 2K / 4K 全部成功 | 1K 成功 |
| 原 GPT Image 2 VIP | 上游任务失败一次 | 未测 |

GPT 新版质量档位以 1024x1024 测试，返回尺寸一致；原 GPT Image 2.5
请求 1024x1024 却实际返回 1254x1254，界面按实际尺寸显示。
两款 Gemini 分别返回 1024x1024、2048x2048、4096x4096。
多图重绘使用茶壶图和黄色色卡，已视觉核对保留主体并修改背景。

按公开请求单价估算，成功请求合计 4.44 美元；若失败请求也计费则为 4.57 美元，
实际以账户账单为准。单次成功不代表持续稳定，也未证明高质量档一定有肉眼可见提升。
未覆盖 GPT 大尺寸与各质量的交叉组合，或图片编辑的全部质量档位。

逐项任务号、状态、耗时和图片放在 `output/imagegen/rightapi-matrix-20261016/`，
属于本地测试产物，不必随部署提交。
