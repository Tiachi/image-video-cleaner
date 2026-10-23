# 帧缓存参考、实现取舍与来源

日期：2026-10-23。按用户要求，读取了`Tiachi/Unimoe2d5_inference`的实际源码及相关运维记录，固定参考提交为 **`6f840b71b4cb0f9e151a44031fab1848ac1c4673`**。本地参考checkout只读；没有改其服务、缓存或运行结果。

## 1. 实际核对的源码

| 固定参考文件 | 核对到的做法 |
|---|---|
| [cached_video.py](https://github.com/Tiachi/Unimoe2d5_inference/blob/6f840b71b4cb0f9e151a44031fab1848ac1c4673/src/unimoe2d5_inference/vllm/cached_video.py) | source路径/大小/mtime，dataset+profile命名空间，解码器/帧数/FPS/视觉预算配置，单视频锁，THWC RGB8布局检查，完成JSON最后发布，require_hit不允许重解码 |
| [hf78_frame_cache.py](https://github.com/Tiachi/Unimoe2d5_inference/blob/6f840b71b4cb0f9e151a44031fab1848ac1c4673/src/unimoe2d5_inference/evaluation/hf78_frame_cache.py) | fixed decoder身份；HF缩放后JPEG90独立命名空间；与来源run的完整缓存对应关系 |
| [hf78_jpeg_video.py](https://github.com/Tiachi/Unimoe2d5_inference/blob/6f840b71b4cb0f9e151a44031fab1848ac1c4673/src/unimoe2d5_inference/vllm/hf78_jpeg_video.py) | JPEG cache-only，检查图数/几何/索引/token预算，不回退到RGB或其他解码器，不能二次套用不匹配的预处理 |
| [videomme_precache.md](https://github.com/Tiachi/Unimoe2d5_inference/blob/6f840b71b4cb0f9e151a44031fab1848ac1c4673/docs/videomme_frame_precache.md) | CPU预缓存独立于模型评测；按视频去重复用；缓存不等于免去后续图像处理/视觉塔/LLM |

参考是缓存**机制**，不是把评测输入原封不动搬来。原仓库特定fixed decord、2FPS、256帧、12288 token、HF JPEG90与UniMoE输入边界属于其评测协议。本项目是Qwen清洗工具，不能未经验证沿用这些数值并宣称像素合同等价。

## 2. 本项目对应实现

| 机制 | 新实现 | 差异/边界 |
|---|---|---|
| 数据集/配置隔离 | `cache_namespace/profile_hash/source_clip_key/` | 显式切片边界进入身份；不只按视频文件名 |
| 源身份 | 允许根内的规范路径+size/mtime_ns/inode/device | 不全量散列视频负载；源文件假定不可变，读前后核对 |
| 解码身份 | FFmpeg/ffprobe二进制SHA、提取器源码SHA、策略与像素/字节预算 | 用FFmpeg精确seek与PTS，不宣称和decord/HF的帧索引逐像素相等 |
| 帧数据 | 原生PNG、manifest、PNG SHA与尺寸/PTS | 不使用pickle、Torch缓存，不加载UniMoE `.npy/.pt` |
| 原子完成 | PNG和manifest写入独立staging，manifest SHA完成后原子发布目录 | 未完成目录不作为命中，不覆盖已发布项 |
| 缓存准入 | 预留空间、像素/单帧/请求字节上限、容量账本 | 当前缓存根单写者锁+线程分片锁，多个作业不能同时写同一根；不同根可并行 |
| 缓存命中 | 核验元数据和派生帧SHA，不打开原视频负载重解码 | 仍stat原媒体并读取PNG；不是完全无IO |
| 显式预热 | `stage03 cache`：只做CPU抽帧并保存独立报告 | 不发模型请求；PARTIAL_FRAME_CACHE不是全部准备成功 |
| 严格只读 | `frame_cache_mode=require_hit` | 缺失停止，不暗中解码；应先使所需缓存齐全 |
| 跨模型/Prompt复用 | 缓存键不含QA、模型权重或Prompt | 缓存是模型前处理之前的PNG；模型服务缩放配置另行记录 |

Stage01缓存的是blur**数值**，没有保存帧图；不能说Stage03直接复用Stage01不存在的PNG。Stage03首次准备后可在多QA及不同Prompt间共享。

## 3. 抽帧与实际时间

默认每个视频切片8帧，取`start+(end-start)*(i+0.5)/8`。缺start用0，缺end优先用视频流时长。当前`ffmpeg_tools.probe`在视频流duration缺失/N/A时会回退format.duration，因此“较长容器音轨不会扩展画面区间”尚非完全保证：容器回退可能受音轨时长影响。真实pilot须覆盖此分支，后续可评估拒绝不确定视频时长或获取可靠流时间，不将回退值宣称为已核实的视频有效时长。显式越界切片、重复PTS、解码帧落到区间外或不足规定帧数都不伪造成功。

缓存分别记录requested_sec、pts_sec、relative_pts_sec、clip_start/end。只有明确的`videos/video_path`字段进入视频解析；其他字段即使包含MP4也不推断为视频。PNG不缩放，Stage01清晰度不受模型缩放影响。

发送给Qwen的只是图片序列加文字时间戳，**不是保留Qwen原生video时间编码的输入协议**。不能把它称为原生视频时序等价；精确时序/瞬时事件/全局计数不足以判断时进入待复核。

## 4. 资源与失败

64GiB默认预算不是百万视频容量保证。准备前保守预留`帧数×单帧上限+元数据`，成功后结算。失败/中断预留继续占账，不自动清理未知目录；需要运维审查后用更大预算的新输出配置/缓存根续做。满额、低磁盘、缓存损坏立即停止，保留完整part；解码/格式问题逐行待复核。

`require_hit`在任何未缓存项停止，包括预缓存中失败的条目；若预缓存报告为PARTIAL，应先处理这些条目，或显式用read_write模式运行、把解码失败归待复核，不能把部分预热当作全量cache-only已就绪。

## 5. 验证状态

已做合成/模拟测试：同切片复用、切片/stat/解码器身份隔离、dataset namespace隔离、PTS边界、损坏manifest/PNG检测、容量停止、cache-only不解码、纯CPU预缓存不请求模型、三阶段逐QA追溯。未做真实FFmpeg、910C视觉推理、速度或精度试验。

来源迁移：通用三步协调器与机械/text/media合同从私有`Tiachi/Dataset`提交`2e6c682c789a11006cbf4873d09088664e39ea8e`适配。新仓库运行不依赖Dataset源码或任何历史媒体路径。此项是独立清洗工程，无D-Spark模型/科研实验决策变化；科研主索引与历史无需更新。
