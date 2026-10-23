# Image Video Cleaner：模型 Agent 完整项目交接

更新日期：2026-10-23。对象：第一次接触本项目、没有此前聊天上下文的编码/运维/质量审阅Agent。

**一句话：这是独立的图片/视频视觉QA三阶段清洗工具，保留原始标注与媒体，逐步产出分流数据、属性、统计和可续跑回执；默认调用用户已启动的Qwen3.8-27B OpenAI兼容API。**

文档编写时实现基线为`8a98786fde0667269d84c5b31ae2070ef040fec4`，完整服务器手册基线为`70a66ce07af429cd4fe9e16968b6d8088637bd6f`。后续Agent须先核对当前分支/HEAD；不要把这些历史基线当作永远最新版本。

## 1. 接手后先做什么

1. 读仓库根[AGENTS.md](../AGENTS.md)，再读本文。用户最新指令和适用环境规则优先；本文提供上下文，不授予超出用户任务的操作权限。
2. 核对`git status --short --branch`、`git remote -v`、`git rev-parse HEAD`，保留已有修改，确认当前仓库不是旧Dataset仓库。
3. 看[验证状态](validation.md)，分清实现、合成测试、真实服务器验证。
4. 按任务阅读[配置说明](configuration.md)、[三阶段合同](pipeline_contract.md)、[帧缓存设计](frame_cache_reference.md)；实际部署按[完整命令手册](server_deployment.md)。
5. 需要改行为时，读对应源码和测试后再改。文档与代码不一致应查明并修复，不通过重新解释用户需求来迎合旧实现。

若只接收这一份文档、无法访问私有仓库，可以据此了解目标与合同，但**不能据此声称已审查最新代码、实际数据或服务**。应让用户提供必要源码/执行结果，不猜未提供的路径和运行状态。

## 2. 用户究竟要什么：已确认的需求与取舍

用户此前在`Tiachi/Dataset`做过图片重构、音视频质量/任务分类和音频清洗。这次希望借鉴历史经验，建立**新的独立私有项目**，范围限定为图片和视频画面，不处理声音。不要把历史音频、6M音视频池、1M/3M图片池的结果冒充这个新项目的产物。

| 用户已确认事项 | 当前实现/边界 |
|---|---|
| 三步清洗，每一步都要有中间产物和统计 | 每阶段独立accepted/rejected/review/audit、summary/report、identity/receipt；模型阶段另有逐QA属性 |
| 1：媒体存在、清晰度、n-gram复读、空问题/答案 | FFmpeg blurdetect和确定规则；另有保守精确重复检查 |
| 2：纯文本QA匹配、拒答/明显错误、质量分、CoT、语言 | 模型输出6整数，程序解析、分流、统计；不看媒体 |
| CoT按0/1/2定义 | **0已有有效CoT；1没有且不适合补；2没有且适合补**。无法判断用模型哨兵9，解析为null/review；不生成CoT |
| 3：带媒体QA匹配与质量评分 | 客户端抽图/帧，模型输出质量、匹配、证据帧，共4整数 |
| 帧缓存参考Unimoe2d5_inference | 参考其身份隔离、不可覆盖发布和cache-only原则；不直接加载其RGB/JPEG张量缓存 |
| 配置输入、各阶段输出、缓存、vLLM入口 | 一份外部统一JSON，自动展开阶段配置和父阶段路径 |
| 已有服务只知道URL和名字也能调用 | 只填`api_base=http://IP:端口/v1`和`model=qwen3.8-27b`；无需本地权重、revision或vLLM Python |
| 默认Qwen3.8-27B/vLLM | API别名可配，按原样发送，不自动改大小写；第三步必须是支持图片的服务 |
| 安全、速度与可运维 | 短数字输出、并发、HTTP复用、模型前便宜筛选、同切片多QA帧复用；没有真实吞吐承诺 |

用户**没有提供本项目真实输入路径**，曾明确回复“暂时没有路径”；也没有提供本项目实际API地址。不能自行选择历史数据启动全量扫描/模型任务。已授权项目代码/文档维护和GitHub常规发布，无需重复询问每个常规提交；仍须遵循环境审批、数据保护和非强制快进规则。

## 3. 项目身份与位置

| 项目 | 地址/状态 |
|---|---|
| 独立私有仓库 | [Tiachi/image-video-cleaner](https://github.com/Tiachi/image-video-cleaner) |
| 稳定分支 | `main`；开发分支`codex/<任务名>` |
| 当前本地源码位置 | `C:/Users/Tiachi/Documents/Dataset/x_temp/image-video-cleaner`；它有自己的Git仓库，运行不依赖父目录源码 |
| 建议服务器代码位置 | `/mnt/sfs/ltj/project/image-video-cleaner`；**尚未实际部署验证** |
| 建议配置 | `/mnt/sfs/ltj/media_cleaning/configs/my_dataset.json`；示例，尚无真实用户配置 |
| 建议任务输出 | `/mnt/sfs/ltj/media_cleaning/<数据集>/<版本>/`；不写入原数据根或代码仓库 |
| 历史工程 | [Tiachi/Dataset](https://github.com/Tiachi/Dataset)，通用实现适配来源提交`2e6c682c789a11006cbf4873d09088664e39ea8e` |
| 帧缓存参考 | [Tiachi/Unimoe2d5_inference](https://github.com/Tiachi/Unimoe2d5_inference)，固定参考`6f840b71b4cb0f9e151a44031fab1848ac1c4673` |

不要在独立仓库使用旧Dataset仓库的origin校验或同步入口。服务器既有的Dataset单仓库deploy key不自动拥有新私有仓库权限。SSH检查和已审阅文件传输在用户任务范围内已授权，GitHub是代码备份与协作渠道，不是直接服务器操作的硬性前置。

## 4. 当前究竟完成到哪里

| 状态 | 已有证据 |
|---|---|
| 已实现 | 三阶段、统一配置、原文字节分流、逐QA属性/统计、断点回执、PNG缓存、已有HTTP(S)API调用、可选16卡启动器 |
| 本地已验证 | 116项标准库unittest通过；Python AST、Bash语法、模板对应、链接、模拟HTTP请求与严格模型协议等 |
| 已写并检查文档 | 完整服务器手册，28个Bash代码块语法检查；不是实际部署/安装成功 |
| GitHub已维护 | 私有仓库main，代码/配置/Prompt/测试/文档；没有数据/媒体/模型负载 |
| 真实FFmpeg清洗 | **未运行验证**，本地没有FFmpeg/ffprobe |
| 真实Qwen服务/910C启动、速度、标签准确率 | **未验证**；模拟服务通过不等于真实视觉能力/质量通过 |
| 本项目实际清洗量、交付数据池 | **没有**，没有指定输入，未运行真实数据 |
| UniMoE正式训练格式合规 | **未声明**；当前是原文分流工具，不是格式转换、注册或训练集发布器 |

PASS只能按具体阶段解释：程序执行/回执校验/统计守恒通过，不是人工真值认证。API模型别名不能证明真实权重；空输入通过也不能当作完成大量清洗。

## 5. 总体数据流和行/QA语义

```text
不可变原始JSONL + 原始图片/视频
    │
    ├─ Stage01 机械规则和FFmpeg清晰度
    │     ├─ rejected / review（保留，不自动恢复）
    │     └─ full/delivery/accepted.jsonl + audit/summary/receipt
    │                        │
    ├─ Stage02 文本模型审阅 ← 只接Stage01正式accepted
    │     ├─ rejected / review
    │     └─ full/delivery/accepted.jsonl + attributes/audit/summary/receipt
    │                        │
    └─ Stage03 视觉模型审阅 ← 只接Stage02正式accepted
          ├─ annotation路径 → 实际切片 → 原生PNG帧缓存 → 图片API
          ├─ rejected / review
          └─ full/delivery/accepted.jsonl + attributes/audit/summary/receipt
```

关键不变量：

- 所有分流JSONL保留输入的**原始行字节**，不重新序列化训练行，不改问题/答案，不转码/覆盖原媒体。
- 一行可有多个QA，判断和属性是逐QA的；**分流是整行的**。有明确reject则整行rejected，否则有review则整行review，其余accepted。
- 以`original_source_row + qa_index`追溯，并绑定原始行SHA；不能仅靠可能重复的id拼接。
- Stage02不能接Stage01的pilot或review；Stage03不能跳过Stage02的正式通过回执。
- audit覆盖本阶段所有受审行；模型attributes覆盖本阶段受审QA，包含row_status，不能把整个attributes当作通过集。
- 此前轮次可作语境，但不是已经核验的画面事实。不得让模型看到后续轮答案造成泄漏，不把此前评分再喂回模型造成锚定。

### 5.1 支持的入口

一行一个JSON对象；支持顶层字符串`question/answer`，或`conversations`中交替human/gpt、user/assistant，使用from/value或role/content。媒体是`images/videos`字符串列表，或`image_path/video_path`单路径别名；冲突/不支持结构待复核。多视频的切片边界数组必须与列表对应，不能把歧义的标量套给多视频。

相对路径以media_root为基准，绝对路径必须位于allowed_media_roots。拒绝URL、目录穿越、越界软链、目录/非正常媒体引用。额外原始字段原样保留但不当作媒体输入；**即使audios中写着MP4，也不自动推断为视频**。

以下仅为合成结构示例，不是真实数据：

```json
{"id":"synthetic-001","video_path":"clips/demo.mp4","video_start":12.0,"video_end":28.0,"question":"What is the person holding?","answer":"A red cup."}
```

这里使用区间是12~28秒，不是整段MP4时长，也不是问答语音的播放时长。

## 6. Stage01：机械清洗合同

| 检查 | 方法 | 默认分流 |
|---|---|---|
| 空问题/空答案 | 去掉图片/视频占位符、空白/不可见格式字符再判空 | reject；明确坏文本先阻止不必要媒体解码 |
| 媒体不存在/零字节 | exact annotation-derived路径检查 | reject |
| IO/解码/结构问题 | 有界探测；不猜修复 | review |
| 文本内部复读 | NFKC/casefold；英文按词、汉字按字的4-gram，至少32 token，重复比例≥0.6 | review；不是全库近重复去重 |
| 精确重复 | 规范QA、规范媒体/切片及其余上下文，忽略顶层id，保留首次 | 后续精确重复reject；同模板不同媒体不算重复 |
| 清晰度 | FFmpeg blurdetect，图片第一帧，视频实际切片8帧均值 | mean_blur大于校准阈值时按配置review/reject |

重复比例为`(全部n-gram数 − 不同n-gram数)/全部n-gram数`；长度不足不按该阈值判复读。跨块精确重复索引在scratch SQLite中，由已发布part重建，防止中断块误抑制自身重跑行。

**清晰度两个阈值默认null，代表未校准，不代表关闭检查。** 未校准样本进入review；需用户结合真实样本观看/blur值校准，不能随意设一个数字让全部通过。不能仅用分辨率判断清晰，不能声称存在跨所有视频的通用blur阈值。

Stage01仅缓存blur测量数值，不保存PNG帧。Stage03首次抽帧不是命中Stage01帧缓存。

## 7. Stage02：纯文本模型合同

源码：[text_qa_clean.py](../scripts/text_qa_clean.py)；严格解析与路由：[text_contract.py](../scripts/text_contract.py)；权威完整Prompt：[text_qa_quality_v1.txt](../prompts/text_qa_quality_v1.txt)。

模型看到当前问题、参考答案、此前QA及同一题的显式`cot/reasoning_process/rationale`。这些是待审内容，不是指令或已核验真值。不发送原ID、媒体路径、图片、第一步分数或后续答案。显式CoT若无法与多QA对应，进入待复核，不猜对应关系。

只返回英文逗号连接的6个整数，如`8,1,0,0,1,2`：

| 顺序 | 属性 | 编码 |
|---|---|---|
| 1 | quality_score | 1~10；长度不等于质量，简短事实答案也可高分 |
| 2 | qa_match | 0明确不匹配、1匹配、2未定 |
| 3 | refusal | 0正常、1无效拒答、2有明确文本前提支持的合理拒答、3未定 |
| 4 | obvious_error | 0未发现文本可证明错误、1明确核心错误、2未定；0不是画面事实已验证 |
| 5 | cot_class | **0已有有效CoT、1没有且不适合补、2没有且适合补**；9未定→null |
| 6 | language | 1中文、2英文、3其他、4混合、0未知 |

分流规则：明确QA不匹配、无效拒答、文本可证明错误剔除；出现这些坏标签却同时给>4分，标签矛盾进入review。未知/协议错误/服务失败/超长文本review；显式minimum_score才按质量分剔除。不是依赖not/无法/没有等关键词一刀切拒答。

CoT0必须是相关、连贯、文本层面可用的推导/解释，空think标签不算；CoT1包括直接识别/OCR/简单事实类；CoT2强调多步计算、比较整合或因果推断，**不意味着当前数据有足够证据自动补写**。当前项目绝不生成、拼接或训练额外CoT。

## 8. Stage03：带媒体模型合同

源码：[media_qa_clean.py](../scripts/media_qa_clean.py)；解析：[media_contract.py](../scripts/media_contract.py)；权威完整Prompt：[media_qa_quality_v1.txt](../prompts/media_qa_quality_v1.txt)。

每行准备帧一次，供该行所有QA复用。发送有编号和时间描述的PNG图片序列，以及当前QA、此前语境、明确同题CoT。Stage02标签只继承到输出属性，不再给模型看。采用Chat Completions的image_url data URL，不将客户端文件路径交给模型服务读盘。

只返回4整数，如`8,1,2,5`：

| 顺序 | 属性 | 约束 |
|---|---|---|
| 1 | media_quality_score | 1~10；基于可见媒体证据的训练有用性 |
| 2 | media_alignment | 0画面积极证据明确反驳、1画面支持关键答案、2证据不足/关联不明 |
| 3、4 | evidence_frame1/2 | 0无证据，或本次请求1~N帧号；第二证据必须不同于第一 |

alignment0须有可见反驳证据且分数≤4，剔除；alignment1须有证据，按显式minimum_score判断；alignment2始终review，不以低分冒充证实错误。非法证据/标签冲突/格式或服务错误review，不自动纠正模型输出。

**稀疏抽帧没看到某事不等于某事未发生。** 精确时间区间、全视频计数、时序、因果只有可见证据足够时才支持。这个接口是带时间描述的多图，不是Qwen原生video时间编码输入，不保证与全视频视觉理解等价。

### Prompt为何这样设计

先用低成本确定规则，再用纯文本语义过滤，最后才花视觉成本；让模型判断，JSON组装/计数交给程序。分别看文本自洽和画面证据，避免流畅长答案掩盖虚构；CoT不为加长而补，缺证据宁可review。格式约束提高输出可处理性，不等于准确率保证；仍需人工抽验。

## 9. 帧缓存：最容易被误解的部分

实现：[frame_cache.py](../scripts/frame_cache.py)，策略版本`native-png-midpoints-v1`。详细参考仓库固定源码及差异见[缓存设计](frame_cache_reference.md)。

- 原生PNG，不resize、不解码声音；模型服务可按自己的processor再次缩放。
- 视频默认8帧，时间为`start+(end-start)*(i+0.5)/8`；缺start为0，缺end优先取视频流时长。**实现边界**：ffmpeg_tools.probe在视频流duration缺失/N/A时会回退format.duration，后者可能受较长音轨影响；“从不以容器时长扩展画面区间”是目标，不是现有代码已完全保证的行为。此类媒体必须纳入pilot，必要时另行实现拒绝不确定时长或可靠视频流时间获取。
- 保存requested_sec、实际pts_sec、relative_pts_sec、clip_start/end；重复PTS、越界、数量不足不伪造成功。
- 身份包括dataset namespace、profile、源规范路径及size/mtime_ns/inode/device、切片、帧策略、FFmpeg/ffprobe和提取器源码身份、预算。源视频不全量散列，依赖原数据不可变及读前后stat检查。
- QA、Prompt、模型权重不进入PNG缓存身份，所以兼容profile下不同QA/模型判断可复用；模型processor配置另行声明，不是已验证远端参数。
- staging中写PNG/manifest，校验完成后发布；命中仍核验元数据与派生PNG SHA，仍需stat原文件和读PNG，不是零IO。
- 单缓存根单作业写锁，作业内线程锁防重复解码；不要同时让cache job和judge job写同一根。
- `read_write`缺失时准备缓存；`require_hit`缺失立即停止，不暗中回退解码。
- `stage03 cache`只CPU预缓存和统计，无模型判断。PARTIAL_FRAME_CACHE不是全部准备成功。
- 默认64GiB不是百万视频保证。容量/损坏/cache-only缺失属于基础设施失败，应停止，不能把后续全池样本判坏。失败/中断预留可能继续占容量账本，不自动清理未知目录。

三种缓存不能混称：磁盘PNG减少解码；服务processor cache减少部分图像预处理；prefix caching减少LLM可共享前缀计算。它们不保证省去视觉编码或所有推理。

## 10. 配置与API：默认只调用已有服务

统一模板：[pipeline.example.json](../configs/pipeline.example.json)。解析器[pipeline_config.py](../scripts/pipeline_config.py)导出严格阶段配置；执行前在外部work_root/config_snapshots下按SHA保留不可覆盖快照。

| 区域 | 作用 |
|---|---|
| paths | source、媒体基准/允许根、work_root，以及可选各阶段输出/缓存/runtime/scratch覆盖 |
| tools | 清洗Python、可选模型Python、FFmpeg/ffprobe绝对路径或PATH名称 |
| api | api_base、model；可选model_revision、service_mode |
| stage01 | 机械并发/块大小、清晰度、n-gram、精确重复 |
| stage02 | 文本并发/超时/输入限制/质量阈值 |
| stage03 | 帧采样、缓存命名空间/预算、图片预算、视觉并发/质量阈值 |
| vllm | 只在自行启动服务时用；existing模式可省略整个段 |

下面是合法api片段，**不是完整配置文件**：

```json
"api": {
  "api_base": "http://实际IP:实际端口/v1",
  "model": "qwen3.8-27b"
}
```

未写service_mode默认existing，未写revision默认null。HTTP/HTTPS、IP/主机名/IPv6可用；base只填到/v1，不附/chat/completions。模型名原样发送，不通过改别名猜测模型；不要求本地权重/版本/vLLM环境。

HTTP客户端直连、不继承环境代理、不跟随重定向、不自动重试，HTTPS使用默认TLS证书校验。每线程复用连接，发送temperature0、max_tokens32、enable_thinking=false。响应最多64KiB。当前没有API key配置入口，401/403不能假装已适配认证。

仅知道alias时记录`model_identity_status=endpoint_alias_only`、revision=null、model_weights_verified=false；提供revision也只是未验证的声明。远端在同别名下更换权重无法由客户端自动察觉，续跑前须确认服务未变；变更用新输出。

自行启动才设`service_mode=managed`，须本机HTTP地址、已有本地权重/冻结声明、正确推理Python；启动器绑定127.0.0.1，16卡TP2/DP8、4API、每rank8序列起步，prefix/async/chunked prefill/decode graph。核验16卡可见及显存，不停止其他作业，不下载权重。vLLM版本兼容、真实模型架构及视觉速度仍待目标环境验证。细节见[服务说明](qwen_vllm.md)。

### 默认参数要点

| 参数 | 当前默认 |
|---|---|
| Stage01 workers / part_rows | 4 / 128 |
| Stage02 workers / part_rows | 64 / 128 |
| Stage03 workers / part_rows | 8 / 32 |
| 文本/视觉请求超时 | 120秒 |
| 当前QA文本JSON字符上限 | 24000；超长不截断，review |
| 每视频帧数 / 每请求最多图片 | 8 / 32 |
| vision_processor_kwargs.max_pixels | 262144；是期望服务profile，不独立验证或逐请求覆盖远端 |
| frame_cache_limit_mb | 65536 |
| 质量阈值 | Stage02/03 minimum_score均null，不擅自默认8或9 |
| 机械清晰度阈值 | 图片/视频均null，须校准 |

确切范围和所有预算以源码DEFAULT/校验器和模板为准；不要把本表当完整Schema。`show`可在真实输入尚未部署时解析配置，不会验证真实API/文件/模型可用。

## 11. 产物、统计和续跑

```text
work_root/
  config_snapshots/<统一配置SHA>/<stage>.json + <stage>.binding.json
  stage01/{pilot-100,full}/
    identity.json
    parts/part-000000/... + receipt.json
    delivery/{accepted,rejected,review,audit}.jsonl
    delivery/{summary.json,report.md,receipt.json}
  stage02/{pilot-100,full}/... + delivery/attributes.jsonl
  stage03/{pilot-100,full}/... + delivery/attributes.jsonl
  stage03/{cache-100,cache-full}/... # 预缓存报告，不是质量标签
  frame_cache/<dataset>/<profile>/<key前缀>/<key>/00.png ... manifest.json manifest.sha256
  vllm_runtime/                     # 可选managed服务
```

`pilot-100`是前100行，不是随机样本；正式run从完整输入开始。Stage02属性含stage1_accepted_row、original_source_row、source_row_sha256、qa_index、row_status、label_status、labels、cot_candidate。Stage03使用stage2_accepted_row，并保留text_labels、media_labels和evidence。字段详见两个write_attributes实现，不能丢掉row_status后直接当作训练标签。

统计区分行与QA、全体合法模型结果与通过集、原因可重叠计数、语言/CoT/分数/匹配、模型失败/token，以及父阶段量和累计保留率。Stage03缓存观察跨恢复块累计，不能误读成此次新增文件数或实测吞吐。

### 续跑身份

identity绑定配置、源stat、运行信息/提交、相关源码/Prompt/工具身份和limit；part保存输入字节偏移/行号、片段及输出SHA。完成块必须连续且校验正确；恢复时读取原输入前缀核验，而不是无条件跳过所有旧IO。

临时SQLite索引/数值缓存放可丢弃scratch，默认系统临时目录，也可指定已存在的外部scratch_root。默认限额8192MiB；持久续跑依据parts/回执，不靠保存SQLite。输入单行上限16MiB，不能静默截断；原始批次目标阈值16MiB是在追加一行后触发分块，实际批次可超过该目标，不能将其称作严格内存上限。scratch检查也不等于进程RAM上限。

发布经过staging、校验、receipt和原子/不可覆盖发布；完整delivery可检查后复用。缓存和已有交付损坏时停止，不自动覆写或清理。

同命令、同配置/提交/服务复用完成块。**未完成块可能重复请求；不提供exactly-once API语义。** 请求超时可能服务已执行，当前无逐请求持久支付账本；不得把“客户端不自动重试”说成“续跑不会重复计费”。任何大规模重新发请求前先核对完成块、失败范围和服务状态。

改变规则/阈值/模型/Prompt/代码时用新输出。只改Stage02则换Stage02及其下游输出根，复用Stage01；只改Stage03可换Stage03输出并复用兼容帧缓存。不要运行中同步代码，不能删除identity强行混跑新旧结果。

## 12. 代码地图与建议阅读顺序

| 文件 | 责任/关键入口 |
|---|---|
| [run.sh](../run.sh) | 用户入口：pipeline、stage01/02/03、vllm、test；OPD_PYTHON选择外层Python |
| [pipeline_config.py](../scripts/pipeline_config.py) | load/resolve/snapshot/child_command：单一配置、路径保护、阶段衔接、existing/managed、独立解释器 |
| [mechanical_clean.py](../scripts/mechanical_clean.py) | read_config/analyse/run/check_part/publish/decision：通用协调器、分流、去重、scratch、回执与统计 |
| [text_checks.py](../scripts/text_checks.py) | QA解析、去占位符判空、normalize、repetition、fingerprint |
| [media_checks.py](../scripts/media_checks.py) | references/resolve/Inspector：只读允许的视觉媒体路径，清晰度与数值缓存 |
| [ffmpeg_tools.py](../scripts/ffmpeg_tools.py) | 有界子进程、工具检查、ffprobe视频流时间、blurdetect和PTS解析 |
| [text_qa_clean.py](../scripts/text_qa_clean.py) | 父阶段回执/逐行溯源、Judge HTTP、文本载荷/CoT对应、analyse、属性/统计 |
| [text_contract.py](../scripts/text_contract.py) | 6整数严格协议与确定性路由 |
| [media_qa_clean.py](../scripts/media_qa_clean.py) | 继承文本HTTP传输/父标签、FrameCache、多图载荷、证据、视觉属性/统计及cache命令 |
| [media_contract.py](../scripts/media_contract.py) | 4整数协议、证据编号与矛盾约束 |
| [frame_cache.py](../scripts/frame_cache.py) | PNG提取、源/切片/profile身份、容量账本、manifest/SHA、cache-only、锁 |
| [vllm_service.py](../scripts/vllm_service.py) | 仅可选本地服务：config/command/memory_gate/serve，自有进程组生命周期 |
| [sync_pull_ff_only.sh](../scripts/sync_pull_ff_only.sh) | 核对私有origin、干净main、禁用本次代理、fetch和仅快进 |

建议顺序：pipeline_config → mechanical_clean的run/decision/publish → text/media_checks → text_qa_clean+contract → frame_cache → media_qa_clean+contract → 对应测试。不要只读包装脚本就断言恢复或统计正确。

## 13. 测试与修改规则

Python>=3.10，清洗端标准库；FFmpeg/ffprobe是外部工具。测试命令：

```bash
bash run.sh test
```

当前116项测试分布在以下文件（数量可能随后续提交变化）：

| 测试 | 覆盖 |
|---|---|
| [test_mechanical_clean.py](../tests/test_mechanical_clean.py)、[test_ffmpeg_tools.py](../tests/test_ffmpeg_tools.py) | QA/路径/机械分流、字节保护、去重/恢复、FFmpeg有界调用/时间与错误 |
| [test_text_qa_clean.py](../tests/test_text_qa_clean.py) | CoT编码、文本协议、合理拒答、阶段溯源/属性、上下文、模型失败与完整块复用 |
| [test_media_qa_clean.py](../tests/test_media_qa_clean.py) | 证据约束、切片/PTS、PNG身份/损坏/容量、cache-only、预缓存零请求、三阶段追溯 |
| [test_standalone_contract.py](../tests/test_standalone_contract.py) | 独立仓库导入、视觉范围、不读取audios、模板DEFAULT一致、16卡服务命令/显存门禁 |
| [test_pipeline_config.py](../tests/test_pipeline_config.py) | 单配置派生、路径互斥、快照不覆盖、只读show、阶段CLI/解释器及existing/managed |
| [test_existing_api.py](../tests/test_existing_api.py) | 本地模拟HTTP文本/图片、连接复用、别名大小写、HTTPS选择、错误不重放/重定向 |

测试不读取真实媒体、不启动真实模型；不能替代目标服务器FFmpeg/视觉pilot。当前Windows环境的普通沙箱不允许测试回环TCP，116项通过是在允许回环的测试进程完成；不能把环境权限问题修改成跳过核心测试后声称协议已验证。

常见修改应该同时检查：

- 改标签编码/Prompt：解析器、路由、属性字段/显示名、统计和现有CoT三类必须一致。
- 改默认参数：源码DEFAULT、config模板、统一配置派生、服务参数与测试同步。
- 改路径/适配器：仅允许根、别名冲突、多视频切片歧义、未知QA结构和原文保护。
- 改缓存：更新版本/profile身份，验证同切片复用与不同切片隔离、PTS、损坏/容量、cache-only零解码。
- 改恢复：完成块校验、原输入前缀、统计守恒、跨块重复及未知执行结果，不能只写“重复跑一次就行”。

## 14. 运维、Git与数据安全

原标注/媒体不可覆盖、移动、删除或转码替换。共享存储先exact stat、有界目录清单和标注引用允许列表，不对/mnt/sfs大树执行无界find/du/ls-R/全树ffprobe/哈希。

仓库只存代码、无秘密模板、Prompt、测试、文档。原/派生JSONL、媒体、模型、SQLite、缓存、完整日志、实际私密配置/密钥不可提交。`.gitignore`不是安全认证，仍须逐文件审阅、精确路径暂存；不git add .或-A。

同步要求核对owner/repo/private、分支、干净状态、仅快进和远端SHA。不要force push、自动stash/reset/clean；保留未知工作。常规代码/文档发布已获用户持续授权，不再机械重复询问，但不能跳过环境审批。Git传输失败可使用官方GitHub Git Database API作代码备份；必须核对文件树、父提交、内容和远端SHA，不上传数据，不把本地未发布提交说成已发布。

服务器下载（Git、依赖、模型等）**默认禁止使用用户个人Clash/代理**。检查代理键/环境，单次操作用直连或批准路由，不改无关持久设置。历史GitHub排障授权不构成以后下载的个人代理通行证。

长逻辑进已审阅仓库脚本，用户只执行短入口；不得让用户在长寿命交互shell直接set -e/-u/pipefail、exit/exec或粘贴长heredoc。严格模式在子脚本内。SSH失败后提供已发布代码对应的短手动命令续做，不反复要求修映射，也不凭空新建重复任务。

若未来被要求转成/注册为UniMoE正式训练数据，须重新读取用户指定的`C:/Users/Tiachi/Downloads/data_format.md`及当时生效规则；当前原文分流格式与自有README不能证明满足该模板。模板缺失不能猜。本文不改变该训练交付合同，也不把本工程归到D-Spark加速数据工作中。

## 15. 下一位Agent应推进的事项

**当前没有在跑的服务器任务，也没有待续跑的本项目真实数据任务。** 最新请求是文档交接。以下是后续部署条件满足后的工作，不是授权立即执行全量：

1. 获取或核实真实JSONL、媒体相对根/允许根、期望外部工作目录，以及已有API URL/确切alias。
2. 只读少量真实行确认支持的QA/媒体结构；有特殊结构先做明确适配和合成测试，不猜audios或其他字段。
3. 部署干净main，核对Python>=3.10、FFmpeg/ffprobe/blurdetect，复用已有服务；第三步须确认视觉输入能力。
4. 按完整手册做机械校准pilot；结合画面决定blur阈值，换新输出，再正式Stage01。
5. Stage02/03各pilot抽验：标签质量、模型错误、review/拒绝原因、证据与PTS、token/速率/资源；再决定分数阈值/预算/并发和正式运行。
6. 记录真实服务器提交、配置、输入/输出回执、样本量、耗时、质量抽验，更新验证文档；只回传允许的聚合和裁剪审阅资料，不上传原数据。

仍未解决/不可假定：最佳blur阈值、真实标签准确率、图片数/像素预算与服务兼容性、稀疏帧对时序/计数任务的覆盖、视频流duration缺失时的容器时长回退、64GiB是否够用、大池吞吐和总耗时、远端权重身份、是否需要服务认证、正式训练格式交付。新增部署/优化任务时，优先在真实pilot中覆盖这些边界，而不是为了让PASS数量好看而放宽保护。

## 16. 可直接转发给下一位模型的接手指令

> 请阅读私有Tiachi/image-video-cleaner仓库的AGENTS.md和docs/AGENT_HANDOFF.md，再按任务查对应源码/测试。项目仅清洗图片和视频画面，三阶段为机械→文本QA→视觉QA，每步保留原文字节分流、属性、统计和回执。CoT编码固定0已有、1无且不适合、2无且适合，不生成CoT。默认调用已有vLLM，只需/v1 URL和准确模型名，不要求权重路径。当前代码已做116项合成测试，但尚未提供真实输入/API，未运行实际清洗；不要把历史Dataset百万池成果当成本项目结果。保留原数据/已有工作，续跑核对identity与parts，不对不确定请求盲目重发，不用个人代理，不上传数据；完整服务器步骤在docs/server_deployment.md。先报告你核实的当前HEAD、任务范围、真实已知/未知项，再继续用户授权的工作。
