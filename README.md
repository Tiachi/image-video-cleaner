# Image Video Cleaner：图片/视频三步清洗管道

只处理**图片和视频画面**，不处理声音。默认使用已有本机 **Qwen3.8-27B + vLLM**；模型名称与API均可配置。原始JSONL和媒体保持不变，每一步独立交付中间产物与统计，支持完整块续跑。

当前状态：已实现并完成合成测试。没有指定真实输入，没有启动媒体扫描、模型或NPU；不把格式校验通过当作人工质量认证。

| 阶段 | 处理内容 | 独立产物 |
|---|---|---|
| 1. 机械清洗 | 媒体存在/零字节、FFmpeg清晰度、问题与答案内部n-gram复读、空问题/答案、保守精确重复 | 通过/剔除/待复核JSONL、媒体测量、审计与统计 |
| 2. 纯文本QA质量 | QA匹配、无效/合理拒答、文本可证明错误、1~10分、CoT0/1/2、语言 | 逐QA六项属性、质量分布、模型状态/token与阶段保留量 |
| 3. 带媒体QA质量 | 图片/实际视频切片抽帧、帧缓存、画面与QA匹配、媒体质量1~10分、证据帧 | 逐QA媒体分数/匹配/帧时间戳、沿用第二步标签、缓存及筛选统计 |

第二步只消费第一步正式通过集，第三步只消费第二步正式通过集。源行按**字节原样**保留，任一QA明确不合格则整行剔除，任一QA不确定则整行待复核；不拆多轮对话破坏上下文。原始额外字段保留但不自动作为媒体输入，不推断仅在其他字段出现的MP4。支持的输入结构及边界见[三步合同](docs/pipeline_contract.md)。

## 帧缓存：参考 Unimoe2d5_inference

已阅读参考仓库固定提交的实际代码，并采用：**数据集/采样配置隔离、文件与解码器身份、完成标记最后发布、缓存校验、显式CPU预缓存、cache-only缺失不解码**。具体源码链接、固定SHA、采用点与差异见[缓存参考与设计](docs/frame_cache_reference.md)。

本项目缓存是原生PNG，模型无关，不直接读UniMoE原始RGB `.npy` 或HF缩放后JPEG `.pt`。视频默认在`video_start/video_end`实际区间内均匀抽8帧，保留请求时间与实际解码PTS；同视频不同切片隔离，同切片多QA复用。图片缓存第一帧。`frame_cache_mode=read_write`可边准备边评审；`require_hit`只读已准备缓存、缺失即停。

**8帧没看到某事件不证明事件不存在。** 第三步只有“0明确视觉矛盾、1画面证据支持、2证据不足”，第2类进待复核。没有音频专项分支或标签。

## 每步的中间产物

```text
<外部任务目录>/
  stage01/full/
    identity.json
    parts/part-000000/... + receipt.json
    delivery/{accepted,rejected,review,audit}.jsonl
    delivery/{summary.json,report.md,receipt.json}
  stage02/full/
    identity.json + parts/...
    delivery/{accepted,rejected,review,audit,attributes}.jsonl
    delivery/{summary.json,report.md,receipt.json}
  stage03/full/
    identity.json + parts/...
    delivery/{accepted,rejected,review,audit,attributes}.jsonl
    delivery/{summary.json,report.md,receipt.json}
  stage03/cache-full/      # 显式CPU预缓存报告，不是QA质量结果
  frame_cache/<数据集>/<采样profile>/<key前缀>/<key>/
    00.png ... manifest.json manifest.sha256
```

统计同时区分行与QA、全部受审结果与通过集，保存原因、分数/语言/CoT/匹配分布、模型失败、token、源与输出SHA、代码/prompt身份、前序阶段数量与累计保留率。属性按`original_source_row + qa_index`追溯；不是靠可能重复的ID拼接。

## 快速操作

建议服务器代码目录 `/mnt/sfs/ltj/project/image-video-cleaner`，外部数据/缓存目录 `/mnt/sfs/ltj/media_cleaning/<任务名>/`。这些是**建议路径，尚未部署**。完整配置和执行顺序见[操作手册](docs/runbook.md)。

```bash
bash run.sh stage01 init-config /mnt/sfs/ltj/iv-stage01.local.json
bash run.sh stage02 init-config /mnt/sfs/ltj/iv-stage02.local.json
bash run.sh stage03 init-config /mnt/sfs/ltj/iv-stage03.local.json
bash run.sh vllm init-config --config /mnt/sfs/ltj/iv-vllm.local.json
```

填写输入、媒体根、输出、冻结模型revision、工具路径后，按步先pilot、审阅、再正式。第三步可先纯CPU预缓存：

```bash
bash run.sh stage03 cache --config /mnt/sfs/ltj/iv-stage03.local.json
bash run.sh stage03 pilot --config /mnt/sfs/ltj/iv-stage03.local.json --rows 100
bash run.sh stage03 run --config /mnt/sfs/ltj/iv-stage03.local.json
```

首次建议`minimum_score=null`先看打分分布；不擅自设置8/9分过滤。缓存容量默认64GiB，仅是起点；满额停止而不是把剩余样本当成坏数据。修改配置/prompt用新output；符合身份的模型无关帧缓存可继续复用。

## 模型接口与Prompt

默认 `http://127.0.0.1:8018/v1/chat/completions`，served-model-name=`Qwen3.8-27B`。只连接显式本机HTTP，图像用PNG data URL，不将媒体路径交给服务读盘。

| 阶段 | 模型输出 | 程序处理 |
|---|---|---|
| 2 | `质量分,QA匹配,拒答类型,明显错误,CoT类别,语言` | 严格解析六个整数，生成属性/统计 |
| 3 | `质量分,媒体匹配,证据帧1,证据帧2` | 严格解析四个整数，核验帧编号并保存证据 |

CoT：0已有有效CoT；1无且不适合补充；2无且适合补充；无法判断则模型哨兵9转null待复核。**不生成CoT。**

完整Prompt：[纯文本](prompts/text_qa_quality_v1.txt)、[带媒体](prompts/media_qa_quality_v1.txt)。设计想法是先用便宜规则/文本过滤明显问题，最后为剩余QA花视觉推理成本；短数字输出节省生成，帧缓存节省重复解码。prefix caching、processor cache与磁盘帧缓存是不同层，收益必须分别实测。

16卡vLLM暂定TP2/DP8、prefix caching、chunked prefill、async scheduling、decode graph；视觉阶段从每DP rank8序列起步。输入是多张图片，不用language-model-only。服务参数与限制见[Qwen vLLM说明](docs/qwen_vllm.md)。

## 依赖与测试

清洗端Python>=3.10，仅标准库；外部FFmpeg/ffprobe须有blurdetect。服务使用已有兼容Qwen3.8-27B的vLLM/vLLM-Ascend环境；本项目不下载权重、不自动安装推理环境。

```bash
bash run.sh test
```

真实模型正确率、FFmpeg兼容性、服务器吞吐和像素预算均待指定输入后试跑验证。工具原样分流标注，不是UniMoE正式训练格式转换/注册器。
