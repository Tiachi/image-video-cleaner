# Qwen3.8-27B接口与16卡暂定配置

日期：2026-10-23。沿用用户此前本地服务名Qwen3.8-27B和TP2/DP8形式，使用本地既有权重/兼容vLLM-Ascend，不下载模型。过去纯文本速度不代表本项目视觉请求速度。

## 暂定服务配置

| 参数 | 初始值/理由 |
|---|---|
| OpenAI兼容API | `http://127.0.0.1:8018/v1`，model=`Qwen3.8-27B` |
| TP/DP | 2×8，共16张可见NPU |
| API进程 | 4 |
| max_model_len / batch tokens | 32768 / 32768 |
| max_num_seqs | 每DP rank8起步；没有沿用文本64序列的显存假设 |
| 生成 | temperature0、max_tokens32、enable_thinking=false；只输出数字 |
| 多模态输入 | 启用image最多32，video请求输入0；视频由客户端抽帧，以带时间戳的多图发送 |
| 图像processor | 服务端`--mm-processor-kwargs`声明max_pixels262144，实际支持与效果待模型/版本验证 |
| 缓存与调度 | prefix caching＋mamba align、chunked prefill、async scheduling、FULL_DECODE_ONLY graph |
| 多模态processor cache | 1GiB/进程缓存；4API+8DP可能约12GiB宿主缓存预算，另需在途帧/编码开销 |

启动器读本地模型config并要求vision_config，避免误用text-only服务。`model_revision`是声明的冻结身份，非权重全量SHA验收；API model alias也不能证明实际权重。启动回执保存模型config SHA、完整命令、代码SHA和逐卡显存观察。使用`torch_npu.npu.mem_get_info`避开过去torch.accelerator allocator兼容问题。

## 预处理与帧缓存的边界

磁盘缓存是**原生PNG**，FFmpeg不resize。Qwen服务随后按自己的图像processor做resize，这会影响第三步可见细节；Stage01blur仍在原生分辨率上判断。小字/OCR试验可用更大max_pixels，但应同时修改服务与Stage03声明，并用新输出目录做质量/速度比较，不能把参数变更伪装成旧标签续跑。

Stage03 `vision_processor_kwargs`记录期望的服务配置，不通过每请求覆盖服务参数。服务端实际配置应和其一致；当前客户端不会独立查询/验证实际processor，runtime明确记录processor_profile_verified=false。这样避免不同vLLM版本对每请求mm kwargs的信任开关差异。服务全局参数及多模态缓存说明见[vLLM serve官方文档](https://docs.vllm.ai/en/stable/cli/serve/)。

输入格式使用Chat Completions多图`image_url`+PNG base64，见[vLLM多模态输入](https://docs.vllm.ai/en/stable/features/multimodal_inputs/)。不是Qwen原生video输入，也没有训练UniMoE的HF-JPEG预处理合同；不声明跨模型像素等价。

## 运行与验证边界

先`bash run.sh vllm print --config <外部配置>`审阅命令，再serve。若所用vLLM版本不支持暂定优化参数，应看本次精确启动日志、调整新配置；不静默取消优化/切模型并复用旧身份。输入/依赖未指定前不启动。

本地验证是命令构造、内存门禁和HTTP协议模拟，不是910C启动成功、吞吐或标签准确率。prefix缓存减少可共享前缀的计算；processor缓存避免部分重复预处理；磁盘帧缓存减少解码，三者不能混为同一命中率，也不保证免去视觉编码或LLM推理。
