# 简洁操作手册

日期：2026-10-23。以下为指定输入和部署后的操作。当前服务器路径尚未创建，未扫描真实媒体。

## 1. 代码、数据与依赖

建议新代码checkout：`/mnt/sfs/ltj/project/image-video-cleaner`，私有仓库`Tiachi/image-video-cleaner`；数据输出`/mnt/sfs/ltj/media_cleaning/<任务名>/`。原数据仍在原位置，只读。

Python>=3.10；清洗端无pip依赖。FFmpeg/ffprobe须可用并带blurdetect；本项目不下载工具或模型。已有Qwen服务只需API地址与模型名；自行启动时才需要兼容vLLM-Ascend环境、本地权重路径与冻结revision。

在干净checkout中同步，禁用个人代理；若fetch失败停止，不覆盖当前代码：

```bash
bash scripts/sync_pull_ff_only.sh
```

## 2. 创建统一配置（推荐）

**当前推荐：只创建一份统一配置**，不再手工维护四份。模板为`configs/pipeline.example.json`，参见[统一配置字段与命令](configuration.md)：

```bash
bash run.sh pipeline init-config /mnt/sfs/ltj/image-video.local.json
bash run.sh pipeline show --config /mnt/sfs/ltj/image-video.local.json
```

先编辑source/media_root/work_root及工具位置；已有服务的api段只填`api_base`和`model`，revision和本地权重无需填写。执行show后，用`bash run.sh pipeline stage01|stage02|stage03 --config ... --phase pilot|run`。只有自行启动服务时才设置`api.service_mode=managed`并补齐权重/运行环境，然后使用`pipeline vllm --phase print|serve`。下面四份配置是兼容旧入口，使用统一配置时不用执行。

```bash
bash run.sh stage01 init-config /mnt/sfs/ltj/iv-stage01.local.json
bash run.sh stage02 init-config /mnt/sfs/ltj/iv-stage02.local.json
bash run.sh stage03 init-config /mnt/sfs/ltj/iv-stage03.local.json
bash run.sh vllm init-config --config /mnt/sfs/ltj/iv-vllm.local.json
```

init使用独占创建，不覆盖同名配置。填法：

| 配置 | 必填/需审阅内容 |
|---|---|
| stage01 | source JSONL、media_root、allowed_media_roots、独立output、现有ffmpeg/ffprobe路径；用真实样本校准两个clarity阈值 |
| stage02 | stage1_delivery指向Stage01正式full/delivery、独立output、served model和API；revision可null；默认质量阈值null |
| stage03 | stage2_delivery指向Stage02正式full/delivery、独立output、独立frame_cache_root/容量、cache_namespace、工具路径、model和API；revision可null |
| vllm | model_path、model_revision、独立runtime_root；启动时记录config SHA和命令，不散列全权重 |

source、media roots、output、scratch/cache之间禁止重叠，配置也不要写入代码目录。scratch_root若指定须为已存在的仓库外目录；SQLite仅在可丢弃scratch中，持久续跑靠parts而非数据库。

## 3. 按阶段运行

第一步不需要模型：

```bash
bash run.sh stage01 pilot --config /mnt/sfs/ltj/iv-stage01.local.json --rows 100
bash run.sh stage01 run --config /mnt/sfs/ltj/iv-stage01.local.json
```

pilot是输入前100行，便于连通性/阈值检查，不代表随机质量抽样。blur阈值null时待复核是预期，不能把空通过集冒充全部数据已经清洗可用。

已有API跳过下列启动操作。需要本项目自行启动时，另开终端使用独立服务配置，先检查将使用的命令：

```bash
bash run.sh vllm print --config /mnt/sfs/ltj/iv-vllm.local.json
bash run.sh vllm serve --config /mnt/sfs/ltj/iv-vllm.local.json
```

使用正确环境中的Python执行。serve前检查16张可见NPU与显存、端口和本地模型vision_config；只管理自己新起的进程组，不停止其他服务。服务日志/启动回执在runtime_root。暂定参数/兼容性见[模型接口说明](qwen_vllm.md)。确认服务就绪后：

```bash
bash run.sh stage02 pilot --config /mnt/sfs/ltj/iv-stage02.local.json --rows 100
bash run.sh stage02 run --config /mnt/sfs/ltj/iv-stage02.local.json
bash run.sh stage03 cache --config /mnt/sfs/ltj/iv-stage03.local.json
```

cache不需要已启动模型，默认扫第二步全部通过行；可加`--rows 100`只准备前100行。查看缓存报告：FRAME_CACHE_PREPARED意味着该范围准备完成，PARTIAL_FRAME_CACHE有失败不能当作全部命中。默认read_write仍可边准备边判；若预缓存齐全，可把frame_cache_mode改为require_hit再开始**新的**模型输出目录：

```bash
bash run.sh stage03 pilot --config /mnt/sfs/ltj/iv-stage03.local.json --rows 100
bash run.sh stage03 run --config /mnt/sfs/ltj/iv-stage03.local.json
```

同配置/commit续跑用同一命令；已完成块复用。不要运行中同步代码。规则、模型声明、Prompt或阈值变化用新output；同身份PNG缓存可继续指向原根。一个缓存根目前只允许一个job持写锁，不把预热和评审同时写同一根。

## 4. 查看成果

每阶段`full/delivery/report.md`供人看，`summary.json`供程序用；`audit.jsonl`是所有行的审计，attributes逐QA标签含row_status。统计应说明本阶段输入/通过/剔除/待复核行与QA数、各分值/分类分布及累计保留量。accepted不是人工真值认证，review不会自动进入训练。

缓存满额/损坏停止后先查看精确报错，不删除原媒体，不重命名旧交付，不清空缓存。容量账本是保守值，中断预留也占账；扩容时用明确新配置/output，不能修改旧identity伪装续跑。
