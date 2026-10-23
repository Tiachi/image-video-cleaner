# 服务器部署、配置与三阶段运行命令手册

日期：2026-10-23。仓库：私有 [Tiachi/image-video-cleaner](https://github.com/Tiachi/image-video-cleaner)。仅处理图片和视频画面，不读声音。本手册以**复用已有vLLM服务**为默认流程；只有最后的可选章节需要本机模型权重和16张NPU。

当前状态：代码与合成测试已验证；下面的服务器目录是建议部署位置，尚未实际部署。没有收到真实标注路径和API地址，不能声称已在服务器完成清洗或模型验收。各代码块按章节依次执行；任一步FAIL先处理报错，不继续下一阶段。尖括号/中文占位须替换为真实值。

## 0. 目录与运行顺序

| 内容 | 建议位置 |
|---|---|
| 代码 | `/mnt/sfs/ltj/project/image-video-cleaner` |
| 用户配置 | `/mnt/sfs/ltj/media_cleaning/configs/my_dataset.json` |
| 清晰度校准试跑 | `/mnt/sfs/ltj/media_cleaning/my_dataset/calibration` |
| 正式任务及中间产物 | `/mnt/sfs/ltj/media_cleaning/my_dataset/v1` |
| 默认帧缓存 | `/mnt/sfs/ltj/media_cleaning/my_dataset/v1/frame_cache` |
| 原始JSONL和图片/视频 | 原位置只读，不复制到代码仓库 |

顺序：部署 → 检查Python/FFmpeg → 创建配置 → 检查API → 机械校准与正式运行 → 文本试跑与正式运行 → 视觉试跑与正式运行。**没有必要提前对全池抽帧**；第三步默认按需缓存，避免为已被前两步剔除的数据解码。

## 1. 首次拉取代码

先查看已有网络配置的**键名**，不打印可能带凭据的代理值：

```bash
git config --show-origin --name-only --get-regexp 'proxy'
env | cut -d= -f1 | grep -iE '^(http|https|all)_proxy$'
```

没有匹配时不输出是正常现象。下面克隆命令只对本次Git禁用HTTP代理，不修改全局设置，不使用个人Clash流量：

```bash
mkdir -p /mnt/sfs/ltj/project
cd /mnt/sfs/ltj/project
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  git -c http.proxy= -c http.https://github.com.proxy= \
  -c http.https://github.com/Tiachi/image-video-cleaner.git.proxy= \
  clone --branch main --single-branch https://github.com/Tiachi/image-video-cleaner.git image-video-cleaner
```

这是私有仓库，需要当前服务器的GitHub凭据有**该仓库**读取权限。若HTTPS提示密码，应使用GitHub访问令牌或已有凭据管理器，不把令牌写入命令、URL、配置或发到聊天。Dataset的repository deploy key不自动拥有新仓库权限；认证失败应先给该新仓库配置现有账号/批准的SSH密钥访问权限，不通过个人代理绕过。

如果代码目录已经存在，**跳过clone，不覆盖目录**，改用：

```bash
cd /mnt/sfs/ltj/project/image-video-cleaner
bash scripts/sync_pull_ff_only.sh
```

同步脚本核对origin/main、要求干净工作区、禁用本次Git代理并仅快进；不stash/reset/clean。然后查看部署身份：

```bash
git remote -v
git status --short --branch
git rev-parse HEAD
```

origin须为`Tiachi/image-video-cleaner`，分支须为`main`，没有文件修改。**运行任务期间不更新代码**；同一任务续跑保持原配置、Prompt、代码提交及模型服务。

## 2. 准备Python和FFmpeg

### 2.1 Python：使用已有3.10及以上版本

清洗端只有Python标准库，**不用pip install，也不用安装torch/transformers/vllm**。先检查：

```bash
python3 --version
command -v python3 ffmpeg ffprobe
```

若当前`jp3`仍是Python3.9，可使用此前服务器日志中的3.12解释器；先确认这个文件确实存在：

```bash
/usr/local/python3.12.13/bin/python3 --version
export OPD_PYTHON=/usr/local/python3.12.13/bin/python3
```

如果服务器的解释器在其他位置，将最后一个路径替换成真实Python>=3.10绝对路径。`OPD_PYTHON`只影响当前终端；每次新开终端需重新设置，或者使用下文的固定解释器入口。可选创建不下载任何包的隔离环境：

```bash
mkdir -p /mnt/sfs/ltj/runtime
"$OPD_PYTHON" -m venv --without-pip /mnt/sfs/ltj/runtime/image-video-cleaner-py312
export OPD_PYTHON=/mnt/sfs/ltj/runtime/image-video-cleaner-py312/bin/python
```

只有已设好`OPD_PYTHON`时才运行这段；若现有解释器够用，可直接跳过venv。

### 2.2 检查媒体工具，缺失时再安装

```bash
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -filters | grep -w blurdetect
```

最后一条应看到`blurdetect`。如果工具不在PATH但已有可执行文件，下文在`tools.ffmpeg`和`tools.ffprobe`中填写它们的绝对路径，无需重复安装。

只有缺失时，先看操作系统：

```bash
cat /etc/os-release
```

**Debian/Ubuntu、且当前有root权限**可执行下列命令。命令仅对本次下载禁用环境/apt代理，不改持久网络设置：

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  apt-get -o Acquire::http::Proxy=DIRECT -o Acquire::https::Proxy=DIRECT update
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  apt-get -o Acquire::http::Proxy=DIRECT -o Acquire::https::Proxy=DIRECT install -y git ca-certificates ffmpeg tmux
```

**使用dnf的系统、且当前有root权限**可尝试已有系统仓库：

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  dnf --setopt=proxy=_none_ install -y git ca-certificates ffmpeg tmux
```

两组安装命令只选符合系统的一组；普通账号由管理员执行。部分企业系统仓库不提供FFmpeg，此时使用已批准的工具安装渠道或已有FFmpeg/ffprobe绝对路径，不擅自添加第三方仓库。安装后重新执行上面的版本和blurdetect检查。

### 2.3 运行本地合成测试

```bash
cd /mnt/sfs/ltj/project/image-video-cleaner
bash run.sh test
```

该命令不读取真实数据或调用真实模型；包含本机回环模拟API测试，要求本机回环连接可用。工具/模型的真实兼容性仍以实际pilot为准。

## 3. 创建唯一一份配置

```bash
mkdir -p /mnt/sfs/ltj/media_cleaning/configs
bash run.sh pipeline init-config /mnt/sfs/ltj/media_cleaning/configs/my_dataset.json
```

已有配置时跳过init：它拒绝覆盖。用VS Code打开此文件，保留模板其他字段，先修改下列内容：

| 字段 | 应填内容 |
|---|---|
| `paths.source` | 真实输入JSONL的绝对路径；一行一个JSON对象 |
| `paths.media_root` | 媒体相对路径的基准目录；输入全为绝对媒体路径时也填实际媒体根 |
| `paths.allowed_media_roots` | 允许读取的媒体根列表；单根可保留`[]`，自动采用media_root |
| `paths.work_root` | 首次校准填`/mnt/sfs/ltj/media_cleaning/my_dataset/calibration` |
| `tools.python` | 与当前OPD_PYTHON一致的实际Python>=3.10绝对路径 |
| `tools.ffmpeg` / `tools.ffprobe` | PATH可找到时保持默认名称，否则填现有可执行文件绝对路径 |
| `api` | 用下面两个字段替换整个api段即可 |
| `stage03.cache_namespace` | 该数据集的英文短名，例如`my_dataset`；不同数据集使用不同名称 |

已有服务的API配置：

```json
"api": {
  "api_base": "http://实际IP:实际端口/v1",
  "model": "qwen3.8-27b"
}
```

模型名须与服务的served-model-name完全一致，包括大小写。无需模型目录、版本号、本地推理Python或NPU信息；`tools.vllm_python`保持null，`vllm.model_path`保持null，整个`vllm`段也可删掉。默认existing模式只调用已有服务。

`stage01_output/stage02_output/stage03_output/frame_cache_root`保持null即可自动按work_root派生；若覆盖它们必须是独立外部目录，不能嵌入原媒体根。多个媒体根显式填allowed_media_roots，不能填整个`/mnt/sfs`绕过限制。

输入支持`images/videos`列表或`image_path/video_path`，QA支持交替`conversations`或顶层`question/answer`。实际切片使用video_start/video_end。仅有audios字段不能当作视觉输入；本项目不会猜测其中是否藏着视频。输入若为JSON数组或其他结构，应先单独确认适配，不通过改后缀假装JSONL。

检查展开路径和阶段衔接：

```bash
bash run.sh pipeline show --config /mnt/sfs/ltj/media_cleaning/configs/my_dataset.json
```

`show`只检查配置和展示路径，不扫描媒体、不发模型请求；不等于源数据/模型已验收。更多字段见[配置手册](configuration.md)。

## 4. 检查已有vLLM服务

用真实IP和端口替换下列地址：

```bash
curl --noproxy '*' --fail --silent --show-error --connect-timeout 5 --max-time 15 http://实际IP:实际端口/v1/models
```

应返回模型列表，核对其中id与配置的`qwen3.8-27b`相同。这一步不生成文本。已有API支持远程HTTP/HTTPS；调用不使用环境代理。

这只证明地址/模型列表可访问，不证明视觉输入可用。文本能力由第二步pilot验证，图片输入由第三步pilot验证。当前客户端面向无需API key的已有vLLM服务；401/403先处理服务认证配置，不能靠填权重路径解决。**复用已有服务时不运行任何vllm serve命令。**

## 5. 第一步：机械清洗

### 5.1 清晰度校准试跑

```bash
bash run.sh pipeline stage01 --config /mnt/sfs/ltj/media_cleaning/configs/my_dataset.json --phase pilot --rows 100
cat /mnt/sfs/ltj/media_cleaning/my_dataset/calibration/stage01/pilot-100/delivery/report.md
head -n 3 /mnt/sfs/ltj/media_cleaning/my_dataset/calibration/stage01/pilot-100/delivery/audit.jsonl
```

pilot是前100行，主要查结构、路径和兼容性，不是随机质量验收。逐行audit的`media[].measurement.mean_blur`是FFmpeg结果，越大越模糊；视频按实际切片抽8帧。结合真实图片/视频观看结果，分别确定`image_threshold`、`video_threshold`，不提供未经校准的通用数字。

默认阈值null意味着**尚未校准**，不是关闭清晰度检查：可测的样本也会因`clarity_threshold_uncalibrated`进review。必须校准实际使用的媒体类型，否则没有正式通过样本供后续处理。无需另一类型时该类型阈值可保留null。

### 5.2 填写阈值，换正式输出目录

仍编辑同一份my_dataset.json：

1. 把`paths.work_root`改为`/mnt/sfs/ltj/media_cleaning/my_dataset/v1`，保留旧校准产物。
2. 填实际使用类型的清晰度数值阈值；`stage01.clarity.action`可保持`review`，将模糊样本隔离，后续只处理accepted。如果希望明确剔除模糊样本，可显式改为`reject`。
3. `stage01.text`默认4-gram、至少32 token、重复比例0.6、review；它检测单条问题/回答内部复读，不是全库模糊去重。
4. `stage02.minimum_score`、`stage03.minimum_score`先保持null，先看打分分布再决定质量阈值。

```bash
bash run.sh pipeline show --config /mnt/sfs/ltj/media_cleaning/configs/my_dataset.json
bash run.sh pipeline stage01 --config /mnt/sfs/ltj/media_cleaning/configs/my_dataset.json --phase pilot --rows 100
cat /mnt/sfs/ltj/media_cleaning/my_dataset/v1/stage01/pilot-100/delivery/report.md
```

确认路径解析、清晰度和分流合理后正式运行：

```bash
bash run.sh pipeline stage01 --config /mnt/sfs/ltj/media_cleaning/configs/my_dataset.json --phase run
cat /mnt/sfs/ltj/media_cleaning/my_dataset/v1/stage01/full/delivery/report.md
```

正式run从完整原输入开始，pilot不算正式前缀，也不能作为第二步输入。空问题/答案、缺失/零字节媒体明确剔除；不确定项保留review。大池机械阶段会实际解码抽帧，速度不等于只读JSONL。

## 6. 第二步：纯文本QA质量清洗

必须等第一步正式交付完成。第二步自动读取Stage01的正式accepted与回执，无需另填输入：

```bash
bash run.sh pipeline stage02 --config /mnt/sfs/ltj/media_cleaning/configs/my_dataset.json --phase pilot --rows 100
cat /mnt/sfs/ltj/media_cleaning/my_dataset/v1/stage02/pilot-100/delivery/report.md
head -n 3 /mnt/sfs/ltj/media_cleaning/my_dataset/v1/stage02/pilot-100/delivery/attributes.jsonl
```

检查分数、QA匹配、拒答/错误、语言、CoT标签与模型失败数；没有真实媒体证据时不能把文本打分当视觉真值。CoT为0已有、1没有且不适合补、2没有且适合补；程序不生成CoT。模型格式错误、超长或服务失败进入review，不自动重试。

确认小样本后运行：

```bash
bash run.sh pipeline stage02 --config /mnt/sfs/ltj/media_cleaning/configs/my_dataset.json --phase run
cat /mnt/sfs/ltj/media_cleaning/my_dataset/v1/stage02/full/delivery/report.md
```

默认并发64，配置位置为`stage02.workers`。不要因纯文本阶段快就直接把第三步提到同样并发；视觉帧、请求体和模型显存开销不同。

## 7. 第三步：带图片/视频帧的QA质量清洗

必须等第二步正式交付完成。默认`read_write`模式边准备帧边评审；同切片多QA复用帧：

```bash
bash run.sh pipeline stage03 --config /mnt/sfs/ltj/media_cleaning/configs/my_dataset.json --phase pilot --rows 100
cat /mnt/sfs/ltj/media_cleaning/my_dataset/v1/stage03/pilot-100/delivery/report.md
head -n 3 /mnt/sfs/ltj/media_cleaning/my_dataset/v1/stage03/pilot-100/delivery/attributes.jsonl
```

第三步API收到文本和PNG帧，服务无需挂载客户端的媒体目录。先核对图片输入确实被服务接受、引用帧有效、模型错误没有成片发生，再正式运行：

```bash
bash run.sh pipeline stage03 --config /mnt/sfs/ltj/media_cleaning/configs/my_dataset.json --phase run
cat /mnt/sfs/ltj/media_cleaning/my_dataset/v1/stage03/full/delivery/report.md
```

默认并发8，每视频8帧，按video_start/video_end使用区间采样。缓存上限默认65536MiB（64GiB），仅为初始限额：大池通常需按存储预算在正式启动前调大。达到限额会停止，不能把后续数据算坏。模型未在抽帧中看到某事件不代表事件不存在；证据不足进入review。

### 可选：先纯CPU预缓存

如果希望把解码和模型调用分开，可在第三步pilot/run之前执行；不是必选项：

```bash
bash run.sh pipeline stage03 --config /mnt/sfs/ltj/media_cleaning/configs/my_dataset.json --phase cache --rows 100
cat /mnt/sfs/ltj/media_cleaning/my_dataset/v1/stage03/cache-100/delivery/report.md
```

去掉`--rows 100`则准备第二步整个正式通过集，产物为`stage03/cache-full/delivery`。这会解码所有所需切片且消耗帧缓存空间；`FRAME_CACHE_PREPARED`与`PARTIAL_FRAME_CACHE`须区分。预缓存不打分、不调用模型。缓存和模型任务不要同时写同一个缓存根。

只有全部所需缓存齐备时才选择`frame_cache_mode=require_hit`，缺失直接停止；切换模式须给Stage03新输出目录，保留原身份。默认read_write无需这一额外操作。

## 8. 交付文件在哪里、怎么看

正式任务路径以本手册v1为例：

| 阶段 | 人工统计 | 程序统计 | 通过数据 |
|---|---|---|---|
| 机械 | `stage01/full/delivery/report.md` | 同目录`summary.json` | 同目录`accepted.jsonl` |
| 文本 | `stage02/full/delivery/report.md` | 同目录`summary.json` | 同目录`accepted.jsonl` |
| 视觉 | `stage03/full/delivery/report.md` | 同目录`summary.json` | 同目录`accepted.jsonl` |

这些相对路径均位于`/mnt/sfs/ltj/media_cleaning/my_dataset/v1`。最终可交付候选是第三步accepted；**每步仍保留自己的统计和中间数据**：

- `accepted.jsonl`：本阶段通过的原始行；不会修改QA或原媒体。
- `rejected.jsonl`：明确剔除行；`review.jsonl`：待复核行，不自动进入下一步。
- `audit.jsonl`：所有受审行的原因、原始行号和测量/模型结果。
- `attributes.jsonl`：第二、三步逐QA属性，含行分流状态；不能把全部属性当作全部通过。
- `summary.json/report.md`：行/QA数量、分流、原因、分数/标签、token、前序及累计保留量。
- `receipt.json`、上级`identity.json`、`parts/`：身份、校验和与断点；不要手工修改。

多QA一行采用整行分流：有明确坏QA整行剔除，否则有待复核则整行review。PASS表示脚本执行和统计通过，不表示人工认证或正式UniMoE训练格式转换完成。

## 9. 长任务、断点与更新

建议在tmux中运行正式阶段，避免断开VS Code终端影响任务：

```bash
tmux new -s ivclean
```

在会话内进入项目、重新设置OPD_PYTHON，再运行上述某一阶段run命令。按`Ctrl+B`后按`D`退出显示但保留会话；重新进入：

```bash
tmux attach -t ivclean
```

失败或中断后，**同配置、同提交、同服务**重新执行原命令即可复用已完成且校验正确的parts。未完成块可能重发请求；不承诺模型API恰好只执行一次。修改规则、阈值、Prompt、代码或服务权重时使用新输出目录，不删除旧identity/parts伪装续跑。

只改变某一阶段参数时可以只换该阶段输出根：例如修改文本阈值，给`paths.stage02_output`和`paths.stage03_output`设置新目录，复用Stage01正式交付；修改视觉阈值，只给stage03新目录，可复用同身份帧缓存。配置工具自动衔接新目录。若所有输出保持null，修改work_root会让三步都变成新任务。

任务结束且不再续跑旧提交时更新代码：

```bash
cd /mnt/sfs/ltj/project/image-video-cleaner
bash scripts/sync_pull_ff_only.sh
git rev-parse HEAD
```

## 10. 可选：本项目自己启动16卡Qwen服务

**已有URL和模型名时整节跳过。** 这里假设兼容Qwen3.8-27B视觉输入的vLLM/vLLM-Ascend、NPU驱动和本地权重已经安装；本项目不下载模型、不安装Ascend推理栈。

把统一配置改为：`api.service_mode="managed"`、`api.api_base="http://127.0.0.1:8018/v1"`，填写`api.model_revision`（实际冻结版本声明）、`vllm.model_path`（本地已有权重绝对路径）、`tools.vllm_python`（推理环境Python绝对路径）。model仍与服务别名一致。

先看卡和命令：

```bash
npu-smi info
bash run.sh pipeline vllm --config /mnt/sfs/ltj/media_cleaning/configs/my_dataset.json --phase print
```

在独立终端启动，清洗终端执行前述阶段命令：

```bash
bash run.sh pipeline vllm --config /mnt/sfs/ltj/media_cleaning/configs/my_dataset.json --phase serve
```

初始TP2/DP8、4API进程、每rank最多8序列、prefix caching/async/decode graph，启动前逐卡显存和端口检查。启动器不停止其他服务，实际参数兼容性与视觉吞吐须在目标910C环境验证。详细说明见[Qwen服务配置](qwen_vllm.md)。

## 11. 常见阻塞速查

| 现象 | 处理 |
|---|---|
| clone/fetch permission denied | 核对新私有仓库权限；Dataset的单仓库deploy key不通用；不切个人代理 |
| Python版本过旧 | 设置OPD_PYTHON到已有3.10+；同时更新tools.python |
| FFmpeg/blurdetect缺失 | 安装匹配系统的软件包，或配置现有ffmpeg/ffprobe绝对路径 |
| Stage01几乎全部review | 先查clarity_threshold_uncalibrated、媒体路径/QA结构，不直接跑后两步 |
| parent receipt/identity missing | 前一步尚未完成正式run，或输入误接pilot/错误目录 |
| HTTP404/model not found | base填到/v1而非/chat/completions，核对/v1/models返回的模型id |
| HTTP401/403 | 服务要求认证；当前无API key配置入口，先确认服务的访问方式 |
| 第三步不支持图片/上下文过长 | 先核对服务的视觉能力和图片数/上下文上限；调低并发或帧数/图片预算后用新输出目录 |
| cache capacity/corruption | 先处理精确报错；限额调整用新配置/输出；不清空缓存或动原媒体 |
| identity changed | 恢复原配置/提交继续，或为变更创建新输出；不删除旧身份文件 |
| dirty checkout | 保留修改，在清楚其来源后提交或另用干净checkout；不reset/clean |
