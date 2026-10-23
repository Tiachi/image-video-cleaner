"""Explicit local Qwen3.8-27B vLLM-Ascend command and owned foreground service."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import mechanical_clean as core

DEFAULT = {'model_path':None, 'model_revision':None, 'runtime_root':None,
           'served_model_name':'Qwen3.8-27B', 'port':8018, 'tensor_parallel_size':2,
           'data_parallel_size':8, 'api_server_count':4, 'max_model_len':32768,
           'max_num_batched_tokens':32768, 'max_num_seqs':8, 'gpu_memory_utilization':0.90,
           'max_images_per_prompt':32, 'vision_max_pixels':262144, 'mm_processor_cache_gb':1,
           'prefix_caching':True, 'async_scheduling':True, 'decode_graph':True}


def emit(event,value):
    print('[visual-vllm] '+event+' '+json.dumps(value,ensure_ascii=False),flush=True)


def config(path):
    core.require(path.stat().st_size<=65536,'service config too large')
    c=json.loads(path.read_bytes(),object_pairs_hook=core.unique_keys)
    core.require(isinstance(c,dict) and set(c)==set(DEFAULT),'service config keys differ from template')
    for key in ('model_path','runtime_root'):
        core.require(isinstance(c[key],str) and Path(c[key]).is_absolute() and core.outside_repo(c[key]),
                     key+' must be explicit absolute path outside code')
        c[key]=str(Path(c[key]).resolve())
    model,root=Path(c['model_path']),Path(c['runtime_root'])
    core.require(root!=model and root not in model.parents and model not in root.parents,
                 'runtime and model roots must not overlap')
    for key in ('model_revision','served_model_name'):
        core.require(isinstance(c[key],str) and c[key].strip(),'declare '+key)
    for key,low,high in (('port',1024,65535),('tensor_parallel_size',1,16),('data_parallel_size',1,16),
            ('api_server_count',1,8),('max_model_len',4096,131072),('max_num_batched_tokens',1024,131072),
            ('max_num_seqs',1,128),('max_images_per_prompt',1,32),('vision_max_pixels',4096,16777216),
            ('mm_processor_cache_gb',0,8)):
        core.require(type(c[key]) is int and low<=c[key]<=high,'invalid '+key)
    core.require(c['tensor_parallel_size']*c['data_parallel_size']==16,'this profile owns exactly16 visible NPUs')
    core.require(type(c['gpu_memory_utilization']) in (int,float) and .1<=c['gpu_memory_utilization']<=.95,
                 'invalid memory utilization')
    for key in ('prefix_caching','async_scheduling','decode_graph'):
        core.require(type(c[key]) is bool,'invalid '+key)
    return c


def command(c):
    cmd=[sys.executable,'-B','-m','vllm.entrypoints.cli.main','serve',c['model_path'],
         '--served-model-name',c['served_model_name'],'--host','127.0.0.1','--port',str(c['port']),
         '--tensor-parallel-size',str(c['tensor_parallel_size']),
         '--data-parallel-size',str(c['data_parallel_size']),
         '--data-parallel-size-local',str(c['data_parallel_size']),'--data-parallel-backend','mp',
         '--api-server-count',str(c['api_server_count']),
         '--max-model-len',str(c['max_model_len']),
         '--max-num-batched-tokens',str(c['max_num_batched_tokens']),
         '--max-num-seqs',str(c['max_num_seqs']),
         '--gpu-memory-utilization',str(c['gpu_memory_utilization']),
         '--mm-processor-cache-gb',str(c['mm_processor_cache_gb']),
         '--limit-mm-per-prompt',json.dumps({'image':c['max_images_per_prompt'],'video':0}),
         '--mm-processor-kwargs',json.dumps({'max_pixels':c['vision_max_pixels']}),
         '--enable-chunked-prefill','--generation-config','vllm']
    if c['prefix_caching']: cmd+=['--enable-prefix-caching','--mamba-cache-mode','align']
    else: cmd+=['--no-enable-prefix-caching']
    if c['async_scheduling']: cmd+=['--async-scheduling']
    if c['decode_graph']: cmd+=['--compilation-config','{"cudagraph_mode":"FULL_DECODE_ONLY"}']
    return cmd


def memory_gate(npu,c):
    expected=c['tensor_parallel_size']*c['data_parallel_size']
    core.require(npu.device_count()==expected,'visible NPU count differs; no service started')
    observations=[]
    for index in range(expected):
        free,total=npu.mem_get_info(index)
        core.require(0<=free<=total and total>0,'invalid NPU memory observation')
        observations.append({'index':index,'free':free,'total':total})
    core.require(all(x['free']>=x['total']*c['gpu_memory_utilization'] for x in observations),
                 'NPU memory occupied; no other process will be stopped')
    return observations


def close_owned_group(process):
    try: os.killpg(process.pid,signal.SIGTERM)
    except ProcessLookupError: return
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        try: os.killpg(process.pid,0)
        except ProcessLookupError: break
        # Reap the API parent while waiting for its own worker group.
        process.poll(); time.sleep(.2)
    else:
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass
    process.wait(timeout=10)


def stop_owned_service(*_):
    raise SystemExit(143)


def serve(c):
    core.require(sys.platform=='linux','serve is for the existing Linux910C vLLM environment')
    core.require(not subprocess.check_output(['git','status','--porcelain'],cwd=core.REPO,text=True,timeout=10).strip(),
                 'serve requires clean versioned checkout')
    metadata=Path(c['model_path'])/'config.json'
    core.require(metadata.is_file() and metadata.stat().st_size<=2*1024**2,'local model config missing/oversized')
    model=json.loads(metadata.read_bytes())
    core.require(isinstance(model.get('vision_config'),dict),'Stage03 requires a visual conditional-generation model')
    with socket.socket() as probe: probe.bind(('127.0.0.1',c['port']))
    # Use the NPU API, not torch.accelerator's incompatible allocator interface.
    import torch_npu
    observations=memory_gate(torch_npu.npu,c)
    root=Path(c['runtime_root']); root.mkdir(parents=True,exist_ok=True)
    with core.writer_lock(root):
        stamp=str(time.time_ns()); record=root/('launch-'+stamp+'.json'); log=root/('vllm-'+stamp+'.log')
        cmd=command(c)
        core.save(record,{'command':cmd,'config':c,'model_config_sha256':core.digest_file(metadata),
            'producer_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=core.REPO,text=True,timeout=10).strip(),
            'device_memory_before_launch':observations,'model_weights_verified':False})
        env=os.environ.copy()
        for key in list(env):
            if key.lower() in ('http_proxy','https_proxy','all_proxy'): env.pop(key)
        env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',PYTHONDONTWRITEBYTECODE='1',
                   TMPDIR=str(root/'tmp'),HF_HOME=str(root/'hf'),VLLM_CACHE_ROOT=str(root/'vllm-cache'))
        (root/'tmp').mkdir(exist_ok=True)
        emit('START',{'log':str(log),'launch':str(record),'api_base':'http://127.0.0.1:%d/v1'%c['port']})
        with log.open('xb') as stream:
            process=subprocess.Popen(cmd,env=env,cwd=root,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
            old=signal.signal(signal.SIGTERM,stop_owned_service)
            try:
                code=process.wait(); core.require(code==0,'owned vLLM exited '+str(code)+'; inspect exact log')
            finally:
                try: close_owned_group(process)
                finally: signal.signal(signal.SIGTERM,old)


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('command',choices=('init-config','print','serve'))
    p.add_argument('--config',type=Path,required=True); a=p.parse_args()
    if a.command=='init-config':
        core.require(core.outside_repo(a.config),'private config belongs outside code')
        core.save(a.config,DEFAULT); emit('CONFIG_CREATED',{'path':str(a.config)}); return
    c=config(a.config)
    if a.command=='print': emit('COMMAND',{'argv':command(c),'runtime_verified':False})
    else: serve(c)


if __name__=='__main__':
    try: main()
    except Exception as exc:
        emit('FAIL',{'error':type(exc).__name__+': '+str(exc),'other_services_signalled':False}); sys.exit(1)
