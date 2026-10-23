# 一份统一配置：输入、阶段产物、帧缓存与vLLM

日期：2026-10-23。推荐用户只维护一份仓库外的`image-video.local.json`。公开模板是[configs/pipeline.example.json](../configs/pipeline.example.json)，实际执行由`scripts/pipeline_config.py`展开到原有阶段入口，不需要手工同步四份配置。

## 创建、编辑、查看

```bash
bash run.sh pipeline init-config /mnt/sfs/ltj/image-video.local.json
```

用编辑器填写该文件。`init-config`不覆盖已有文件；路径均使用绝对路径。最常编辑的位置：

| 字段 | 用途 | 默认/说明 |
|---|---|---|
| paths.source | 原始JSONL入口 | 必填，原文件不变 |
| paths.media_root | JSONL相对图片/视频路径的基准目录 | 必填 |
| paths.allowed_media_roots | 允许读取的媒体目录 | 留空时自动采用media_root；多个根可显式填写 |
| paths.work_root | 本次任务的总工作目录 | 必填，建议`/mnt/sfs/ltj/media_cleaning/任务名` |
| paths.stage01_output | 机械阶段输出根 | null则`work_root/stage01` |
| paths.stage02_output | 文本阶段输出根 | null则`work_root/stage02` |
| paths.stage03_output | 带媒体阶段输出根 | null则`work_root/stage03` |
| paths.frame_cache_root | PNG帧缓存目录 | null则`work_root/frame_cache` |
| paths.vllm_runtime_root | vLLM日志、启动记录及运行缓存 | null则`work_root/vllm_runtime` |
| paths.scratch_root | 可丢弃SQLite临时工作目录 | null使用系统临时目录；指定时须已存在 |
| tools.python | 清洗阶段使用的Python | null沿用当前运行Python，不要求和模型环境一致 |
| tools.vllm_python | 自行启动服务时的vLLM环境Python | 已有API调用不使用；managed模式下可指定已有910C环境的解释器 |
| tools.ffmpeg / ffprobe | 媒体工具入口 | 默认从PATH查找，可写现有可执行文件绝对路径 |
| api.api_base | 模型API入口 | `http://IP:端口/v1`，也支持HTTPS、主机名、IPv6；默认本机8018 |
| api.model | 服务模型名 | 默认`qwen3.8-27b`，与服务的served-model-name完全一致，按原样发送 |
| api.service_mode | existing或managed | 可省略，默认existing；只调用已有服务，不启动/停止模型 |
| api.model_revision | 可选的冻结权重声明 | 已有API可省略/null；managed启动时必须填写；声明不等于验证权重 |
| vllm.model_path | 自行启动服务时的既有权重目录 | existing模式不使用，整个vllm段可省略；不下载权重 |

**已有服务只需两项API信息：**把统一配置的`api`段写为如下内容，其他数据路径和阶段规则按需填写：

```json
"api": {
  "api_base": "http://你的IP:端口/v1",
  "model": "qwen3.8-27b"
}
```

无需提供模型目录、模型版本或本地vLLM解释器。第二步发送文本，第三步发送文本和PNG帧，均调用`/v1/chat/completions`。第三步要求已有服务开启视觉输入；本地仍需FFmpeg用于机械检测/视频抽帧。程序不继承HTTP代理、不自动重试；服务别名/远端权重未核实的状态如实写入runtime，详见[接口说明](qwen_vllm.md)。

三个阶段的详细规则/并发在`stage01`、`stage02`、`stage03`中；服务TP/DP、图执行、prefix、并发在`vllm`中。`stage01.clarity`的两个阈值默认null，要先校准。质量阈值`stage02.minimum_score`与`stage03.minimum_score`各自独立，默认不按分数剔除。

```bash
bash run.sh pipeline show --config /mnt/sfs/ltj/image-video.local.json
```

`show`只读取配置并展示展开结果，不打开原始JSONL/媒体，不写快照/产物，不发模型请求。它可以在前序产物尚未生成时查看衔接关系；不代表真实输入、工具或前序回执已验收。

## 自动衔接与产物位置

第一步读取`paths.source`；第二步固定读取`stage01_output/full/delivery/accepted.jsonl`及其回执；第三步固定读取`stage02_output/full/delivery/accepted.jsonl`及其回执。修改某阶段输出根后，下游自动跟随，不另外手填accepted路径，不把pilot当正式输入。

默认展开：

```text
work_root/
  config_snapshots/<统一配置SHA>/stage01.json + stage01.binding.json
  config_snapshots/<统一配置SHA>/stage02.json + stage02.binding.json
  config_snapshots/<统一配置SHA>/stage03.json + stage03.binding.json
  config_snapshots/<统一配置SHA>/vllm.json + vllm.binding.json
  stage01/{pilot-100,full}/delivery/
  stage02/{pilot-100,full}/delivery/
  stage03/{cache-full,pilot-100,full}/delivery/
  frame_cache/<dataset>/<profile>/<clip key>/...
  vllm_runtime/
```

`delivery`中的accepted/rejected/review/audit、逐QA属性、report/summary/receipt命名保持不变。各输出根可以分别覆盖，但不能相互嵌套或与原始输入、媒体/模型目录重叠；配置快照也不能写进这些原始目录或帧缓存。

仅在managed启动时，`stage03.max_images_per_request`决定vLLM的图片数上限，`stage03.vision_processor_kwargs.max_pixels`决定服务vision_max_pixels；API URL决定服务端口、模型名和revision。existing模式不会修改远端服务的TP/DP、processor或图像上限，这些客户端值仅记录期望配置。

## 使用同一份配置执行

```bash
bash run.sh pipeline stage01 --config /mnt/sfs/ltj/image-video.local.json --phase pilot --rows 100
bash run.sh pipeline stage01 --config /mnt/sfs/ltj/image-video.local.json --phase run
```

默认复用已有API，不执行服务启动命令。已有服务就绪后依次执行：

```bash
bash run.sh pipeline stage02 --config /mnt/sfs/ltj/image-video.local.json --phase pilot --rows 100
bash run.sh pipeline stage02 --config /mnt/sfs/ltj/image-video.local.json --phase run
bash run.sh pipeline stage03 --config /mnt/sfs/ltj/image-video.local.json --phase cache
bash run.sh pipeline stage03 --config /mnt/sfs/ltj/image-video.local.json --phase pilot --rows 100
bash run.sh pipeline stage03 --config /mnt/sfs/ltj/image-video.local.json --phase run
```

自行启动时才设置`api.service_mode=managed`，填写`vllm.model_path`、`api.model_revision`和适用的`tools.vllm_python`，API用本机HTTP地址，再执行：

```bash
bash run.sh pipeline vllm --config /mnt/sfs/ltj/image-video.local.json --phase print
bash run.sh pipeline vllm --config /mnt/sfs/ltj/image-video.local.json --phase serve
```

服务在独立终端前台运行。阶段命令不自动启动模型；existing模式拒绝vllm启动命令；managed模式vllm默认phase为print，清洗阶段默认pilot。`--rows`只支持pilot/cache的1~10000行；cache不加rows为第二步通过集全量预缓存。`frame_cache_mode=require_hit`时缺失不重解码，条件与旧入口一致。

## 快照、修改与验证

统一配置按内容SHA分目录，执行前写入仅该阶段的展开配置及绑定SHA；同内容复用，不覆盖旧快照，发现被改写则停止。真实配置/快照位于外部工作目录，不进入Git。

阶段仍通过原有校验器检查真实源文件、父回执、媒体根、规则值与服务配置；不绕过Git身份、原文保护或质量门禁。更改真正影响阶段运行的规则、Prompt、模型声明或代码提交时必须用新输出目录，原有结果不会被统一配置工具清空或重写。未改变的阶段配置不因其他阶段的参数调整而强行覆盖旧产物。

本地测试覆盖阶段衔接、显式路径覆盖、API/model/vision参数同步、无副作用展示、快照防覆盖和独立模型Python入口；没有启动真实模型、扫描媒体或替用户填写未提供的真实数据位置。
