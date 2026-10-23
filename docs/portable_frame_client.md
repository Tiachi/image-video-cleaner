# 帧缓存客户端外挂：已有 Qwen / vLLM 服务

2026-10-23。**已实现、合成 PNG 与本地回环 HTTP 已测试；未连接用户实际 API，未验证真实 Qwen/Ascend、清洗准确率或吞吐。**

## 1. 本项目真正要完成的事

这是图片/视频**画面 QA 清洗**：机械规则→纯文本 QA→带媒体 QA。Stage03 已经在清洗机上按实际切片抽帧，保存模型无关原生 PNG，再以多图交给远端视觉模型。本次将缓存到 HTTP 的部分拆成可单文件移植的外挂，不把 UniMoE 评测服务装进清洗项目。

对方仅提供 `http://IP:port/v1` 和准确 served model name 即可；清洗机需要已有 Python3.10+，准备缓存时还需要已有 FFmpeg/ffprobe。对方需要支持 Chat Completions 多图 `image_url`。API别名可以是 Qwen3.8-Flash 或其他名称，按原样填写；不能据此证明真实权重身份。

**对方不需要修改源码、安装本项目、挂载缓存、读取客户端文件、加载自定义 video backend 或重启服务。** 网络可达、图片容量/上下文足够是必要条件。若只能接文本，客户端不能替它增加视觉能力。

## 2. 接入方式与代码

```text
Stage02 正式通过集
  → FrameCache：按原有切片策略准备/校验 PNG
  → frame_cache_client.png_content：再核对 bytes/SHA，原字节 base64
  → image_url + 帧编号/实际PTS/切片边界文字
  → /v1/chat/completions
  → 4整数解析、证据帧关联、原文分流与属性/统计
```

| 代码 | 用途 |
|---|---|
| [frame_cache_client.py](../scripts/frame_cache_client.py) | 独立标准库模块；PNG请求封装、HTTP、缓存读取、接口预检、CLI |
| [frame_cache.py](../scripts/frame_cache.py) | 既有CPU抽帧/不可覆盖缓存/来源与切片校验；本次未改采样和缓存版本 |
| [media_qa_clean.py](../scripts/media_qa_clean.py) | Stage03调用同一PNG封装器；明确发送processor配置与保存证据 |
| [text_qa_clean.py](../scripts/text_qa_clean.py) | 保持HTTP连接复用，复用有界单次请求；Stage02不发送视觉参数 |
| [pipeline_config.py](../scripts/pipeline_config.py) | 统一配置新增显式`stage03 --phase api-check` |

单文件可以直接复制到另一台**客户端**使用，不需要pip、torch、PIL、OpenAI SDK、vLLM或本仓库其他脚本。常规三阶段仍使用仓库入口，保持逐行原文、前序回执、已完成块、属性和统计合同。

这里的“vLLM读缓存”实际是客户端读PNG后传原字节。服务器仍执行PNG解码、其模型processor、视觉编码和LLM；不是视觉特征缓存或KV缓存。

## 3. 统一配置与最短操作

在现有仓库中使用已有外部统一配置。没有配置时按[部署手册](server_deployment.md)创建并填写实际路径。`api`只需两项：

```json
"api": {
  "api_base": "http://你的IP:端口/v1",
  "model": "对方准确的served-model-name"
}
```

Stage03默认：

```json
"vision_processor_mode": "request",
"vision_processor_kwargs": {"max_pixels": 262144}
```

已有配置缺失新`vision_processor_mode`时在内存补`request`，不覆盖用户文件。其他规则、8帧、缓存、评分和默认模型别名不变。Qwen会按自己的processor处理PNG，客户端不会强制`do_resize=false`或套用UniMoE的12288token/256帧规范。

先同步干净main并做接口检查；下面配置路径沿用项目既有示例，需指向你自己的文件：

```bash
cd /mnt/sfs/ltj/project/image-video-cleaner
bash scripts/sync_pull_ff_only.sh
bash run.sh pipeline show --config /mnt/sfs/ltj/image-video.local.json
bash run.sh pipeline stage03 --config /mnt/sfs/ltj/image-video.local.json --phase api-check
```

`api-check`不要求Stage01/02已经完成，不需要FFmpeg/本地权重/vLLM Python，不读原数据或缓存。它发起`GET /v1/models`核对准确别名，然后**一次**合成颜色顺序图片请求，图数为配置`max_images_per_request`（默认32）。会实际消耗一次模型调用，**只在用户显式运行时执行**，pilot/full不偷偷重发这个探针。

报告：`<paths.work_root>/api_checks/frame-api-<time_ns>.json`。保存状态、合成期望/观察值、原响应、请求摘要、耗时、发送的processor参数和限制；不保存密钥或base64请求负载。别名不符不发视觉请求；颜色结果不对、HTTP错误、扩展参数不支持均失败，不当PASS。

预检通过仅说明模型名及这次小图行为可用。小图不证明生产分辨率、视觉token/上下文容量、processor内部参数或清洗准确率；继续用真实100行pilot审阅。

前两阶段正式完成后：

```bash
bash run.sh pipeline stage03 --config /mnt/sfs/ltj/image-video.local.json --phase cache --rows 100
bash run.sh pipeline stage03 --config /mnt/sfs/ltj/image-video.local.json --phase pilot --rows 100
```

人工审阅pilot的标签、证据帧、分数分布及review后，再明确运行full：

```bash
bash run.sh pipeline stage03 --config /mnt/sfs/ltj/image-video.local.json --phase run
```

不需要运行任何`vllm serve`命令。可先全量cache，再显式设`frame_cache_mode=require_hit`用新的Stage03输出：缺失/损坏立即停，不边评审边补解码。没有提前准备全量时不要把100行预缓存当作全量cache-only就绪。

## 4. 修复了一个原有配置失效问题

原实现的`vision_processor_kwargs.max_pixels`只用于配置/本地启动参数，Stage03继承的HTTP请求没有携带它。对于远端已有服务，不能据此声称262144已生效。本次默认把配置放入实际请求的`mm_processor_kwargs`，测试检查了服务接收到的真实JSON，而不只检查配置生成。

如果网关禁止请求级processor参数，**不自动删除参数重试**。可以人工确认后将`vision_processor_mode`显式改成`server_default`，重新执行预检；请求不发`mm_processor_kwargs`，报告记为null/未核验，已有max_pixels字段仅是声明，不再声称控制远端预算。生产内存/分辨率依远端配置，需对方确认或真实pilot。

在当前vLLM中该字段是扩展接口，非所有OpenAI兼容服务通用标准。参考：[v0.23.0请求定义](https://github.com/vllm-project/vllm/blob/v0.23.0/vllm/entrypoints/openai/chat_completion/protocol.py)。新版本可能另有准入开关；不要求对方盲目添加不适用于其版本的启动选项。

代码/processor模式改变后，**新建Stage03输出目录**；保留Stage01/02正式产物和同身份PNG缓存。旧运行不能直接混续到新版代码；原始媒体和旧结果不覆盖。

## 5. 独立使用单文件

无需三阶段依赖，可以只复制`scripts/frame_cache_client.py`。独立只读检查一个已发布缓存目录：

```bash
python3 -B scripts/frame_cache_client.py inspect --cache-entry /你的缓存条目目录
```

明确指定URL/模型名，8张合成小图预检（report必须是尚不存在的仓库外绝对路径）：

```bash
bash run.sh frame-api check --api-base http://你的IP:端口/v1 --model 对方模型名 --images 8 --output /mnt/sfs/ltj/frame-api-check-01.json
```

使用一个已有PNG缓存条目发自定义请求。`prompt-file`是待审QA文本/JSON，必须包含对应问题/答案和所需语境；这条独立命令保存响应，**不替代Stage03完整回执与分流评分**：

```bash
bash run.sh frame-api request --config /mnt/sfs/ltj/image-video.local.json --cache-entry /你的缓存条目目录 --system-file prompts/media_qa_quality_v1.txt --prompt-file /你的待审QA.txt --output /mnt/sfs/ltj/frame-api-response-01.json
```

独立`request`默认32token，与清洗短整数协议一致，不是通用长视频聊天生成配置。它核对manifest.sha256、帧数、路径、PNG SHA/尺寸和实际PTS，只读这一个条目，不扫描树、不重解码、不读源视频。来源真实性/解码器身份不是一份自洽manifest能独立认证的；完整Stage03还会通过FrameCache核对真实源与配置。

独立CLI可用`--api-key-env MY_FRAME_API_KEY`从指定环境变量取Key，只放Authorization header。不要将Key写进参数、配置或Git。**当前三阶段没有增加认证字段**；若对方接口必须认证，独立CLI可探测，生产管道认证需要另行接入，不能假称已支持。

## 6. 为何本次不改成JPEG原生视频接口

之前可行性讨论基于UniMoE JPEG缓存：vLLM确有`video/jpeg`接口和原始时间元数据支持，[v0.23.0实现](https://github.com/vllm-project/vllm/blob/v0.23.0/vllm/multimodal/media/video.py)。但本项目已经是**无损PNG、8帧视觉证据、多图接口和按frame_id评分**，本地启动器也显式`video:0`。

直接换成原生视频会改变图片/视频token语义、时间编码、模型预算和prompt，PNG→JPEG还会新增有损编码；现有实际PTS也不能无证据当成原始整型帧索引。因此本次采用客户端PNG外挂，保留已定义的清洗协议。若未来明确需要原生视频时序另设协议版本和验收，不能静默替换。

## 7. 安全、成本与验证状态

- 原始JSONL/媒体、已发布PNG、manifest和旧结果不修改；不传声音；不读UniMoE.pt或执行pickle。
- 发送前再次检查单帧/总字节预算、顺序和SHA；不丢帧、不缩放、不重新编码。不把完整缓存路径、源ID、前序评分发给模型。
- 每线程复用HTTP连接，直连不走环境代理；不跟重定向、不自动重试不确定请求。新报告独占创建，已有报告不会被覆盖或触发新请求。
- base64约增加三分之一传输量，JSON/并发另占内存；帧缓存只减少重复解码，不承诺免去模型视觉编码或某个加速倍数。
- 本地合成测试含PNG CRC/像素解码、回环HTTP实际收包、PNG原字节相等、PTS/切片边界、缓存篡改、服务默认模式、别名/错误/认证边界和单文件隔离执行。
- 未提供真实数据/API，未跑真实清洗、未部署对方插件、未验证Qwen的HF像素一致性；HTTP请求字段被接受不证明它在模型内部被使用。

研究记录评估：独立视觉清洗客户端工程，不涉及D-Spark机制、科研结论或UniMoE正式训练格式发布；不更新跨项目科研索引/历史。
