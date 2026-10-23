"""Single user configuration; derive immutable flat configs for existing stages."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlparse

import mechanical_clean as core
import text_qa_clean as text_stage
import media_qa_clean as media_stage
import vllm_service as service

BOUND1 = {'source','media_root','allowed_media_roots','output','scratch_root','ffmpeg','ffprobe'}
BOUND2 = {'stage1_delivery','output','api_base','model','model_revision','scratch_root'}
BOUND3 = {'stage2_delivery','output','frame_cache_root','api_base','model','model_revision','scratch_root','ffmpeg','ffprobe'}
BOUND_SERVICE = {'runtime_root','served_model_name','port','model_revision','max_images_per_prompt','vision_max_pixels'}


def settings(default, bound):
    return {k:deepcopy(v) for k,v in default.items() if k not in bound}


DEFAULT = {
    'schema_version':1,
    'paths':{'source':None, 'media_root':None, 'allowed_media_roots':[], 'work_root':None,
             'stage01_output':None, 'stage02_output':None, 'stage03_output':None,
             'frame_cache_root':None, 'vllm_runtime_root':None, 'scratch_root':None},
    'tools':{'python':None, 'vllm_python':None, 'ffmpeg':'ffmpeg', 'ffprobe':'ffprobe'},
    'api':{'api_base':'http://127.0.0.1:8018/v1', 'model':'qwen3.8-27b',
           'model_revision':None, 'service_mode':'existing'},
    'stage01':settings(core.DEFAULT,BOUND1),
    'stage02':settings(text_stage.DEFAULT,BOUND2),
    'stage03':settings(media_stage.DEFAULT,BOUND3),
    'vllm':settings(service.DEFAULT,BOUND_SERVICE),
}


def emit(event,value):
    print('[pipeline-config] '+event+' '+json.dumps(value,ensure_ascii=False,allow_nan=False),flush=True)


def load(path):
    core.require(path.stat().st_size<=256*1024,'unified config exceeds256KiB')
    return json.loads(path.read_bytes(),object_pairs_hook=core.unique_keys,
                      parse_constant=core.invalid_number,parse_float=core.safe_float)


def absolute(value,name,optional=False):
    if value is None and optional: return None
    core.require(isinstance(value,str) and Path(value).is_absolute(),name+' must be an absolute path')
    result=Path(value).resolve()
    core.require(core.outside_repo(result),name+' must be outside code')
    core.require(result.parent!=result,name+' must not be filesystem root')
    return result


def overlaps(a,b):
    return a==b or a in b.parents or b in a.parents


def resolve(c):
    # Old configs remain valid; an existing API needs only its URL and served alias.
    core.require(isinstance(c,dict),'unified config must be an object')
    c=deepcopy(c)
    c.setdefault('vllm',deepcopy(DEFAULT['vllm']))
    if isinstance(c.get('api'),dict):
        c['api'].setdefault('model_revision',None)
        c['api'].setdefault('service_mode','existing')
    core.require(isinstance(c,dict) and set(c)==set(DEFAULT),'unified config keys differ from template')
    core.require(type(c['schema_version']) is int and c['schema_version']==1,'unsupported schema_version')
    for key in ('paths','tools','api','stage01','stage02','stage03','vllm'):
        core.require(isinstance(c[key],dict) and set(c[key])==set(DEFAULT[key]),'invalid '+key+' keys')
    for section,key in (('stage01','clarity'),('stage01','text'),('stage03','vision_processor_kwargs')):
        core.require(isinstance(c[section][key],dict) and set(c[section][key])==set(DEFAULT[section][key]),
                     'invalid '+section+'.'+key+' keys')
    paths=c['paths']; work=absolute(paths['work_root'],'paths.work_root')
    source=absolute(paths['source'],'paths.source',True)
    media=absolute(paths['media_root'],'paths.media_root',True)
    roots=paths['allowed_media_roots']
    core.require(isinstance(roots,list),'allowed_media_roots must be a list')
    roots=[absolute(value,'allowed_media_roots') for value in roots] or ([media] if media else [])
    out={key:absolute(paths[key],'paths.'+key,True) or (work/name).resolve() for key,name in (
        ('stage01_output','stage01'),('stage02_output','stage02'),('stage03_output','stage03'),
        ('frame_cache_root','frame_cache'),('vllm_runtime_root','vllm_runtime'))}
    scratch=absolute(paths['scratch_root'],'paths.scratch_root',True)
    mode=c['api']['service_mode']
    core.require(mode in ('existing','managed'),'api.service_mode must be existing or managed')
    model=absolute(c['vllm']['model_path'],'vllm.model_path',True) if mode=='managed' else None
    protected=roots+([source] if source else [])+([model] if model else [])
    snapshots=(work/'config_snapshots').resolve()
    writable=[work,snapshots,*out.values()]+([scratch] if scratch else [])
    for path in writable:
        core.require(not any(overlaps(path,p) for p in protected),
                     'derived path overlaps immutable source/media/model: '+str(path))
    peers=list(out.values())+([scratch] if scratch else [])
    core.require(not any(overlaps(a,b) for i,a in enumerate(peers) for b in peers[i+1:]),
                 'stage outputs/cache/runtime/scratch must not overlap each other')
    # Snapshot files must not live inside stage identities or persistent cache.
    core.require(not any(overlaps(snapshots,p) for p in peers),
                 'config_snapshots overlaps an output/cache/runtime/scratch root')
    text_stage.api_location(c['api']['api_base'])
    text_stage.model_identity(c['api'])
    for key in ('python','vllm_python'):
        core.require(c['tools'][key] is None or isinstance(c['tools'][key],str) and c['tools'][key].strip(),
                     'invalid tools.'+key)
    for key in ('ffmpeg','ffprobe'):
        core.require(isinstance(c['tools'][key],str) and c['tools'][key].strip(),'invalid tools.'+key)
    api={k:deepcopy(c['api'][k]) for k in ('api_base','model','model_revision')}
    common={'scratch_root':str(scratch) if scratch else None}
    tools={k:c['tools'][k] for k in ('ffmpeg','ffprobe')}
    s1=dict(deepcopy(c['stage01']),**common,**tools,source=str(source) if source else None,
            media_root=str(media) if media else None,allowed_media_roots=[str(p) for p in roots],
            output=str(out['stage01_output']))
    s2=dict(deepcopy(c['stage02']),**common,**api,stage1_delivery=str(out['stage01_output']/'full/delivery'),
            output=str(out['stage02_output']))
    s3=dict(deepcopy(c['stage03']),**common,**api,**tools,
            stage2_delivery=str(out['stage02_output']/'full/delivery'),output=str(out['stage03_output']),
            frame_cache_root=str(out['frame_cache_root']))
    processor=s3['vision_processor_kwargs']
    core.require(type(processor['max_pixels']) is int and 4096<=processor['max_pixels']<=16777216,
                 'invalid stage03.vision_processor_kwargs.max_pixels')
    core.require(type(s3['max_images_per_request']) is int and 1<=s3['max_images_per_request']<=32,
                 'invalid stage03.max_images_per_request')
    vllm=dict(deepcopy(c['vllm']),model_path=str(model) if model else None,
              runtime_root=str(out['vllm_runtime_root']),served_model_name=api['model'],
              model_revision=api['model_revision'],port=text_stage.api_location(api['api_base'])[1],
              max_images_per_prompt=s3['max_images_per_request'],vision_max_pixels=processor['max_pixels'])
    return {'stage01':s1,'stage02':s2,'stage03':s3,'vllm':vllm,
            'snapshot_root':str(snapshots), 'service_mode':mode,
            'python':c['tools']['python'], 'vllm_python':c['tools']['vllm_python']}


def snapshot(c,resolved,stage):
    unified_sha=hashlib.sha256(core.encoded(c)).hexdigest()
    root=Path(resolved['snapshot_root'])/unified_sha
    path=root/(stage+'.json'); data=core.encoded(resolved[stage])
    binding={'schema_version':1,'unified_config_sha256':unified_sha,
             'effective_config_sha256':hashlib.sha256(data).hexdigest(),'stage':stage}
    with core.writer_lock(root):
        for file,payload in ((path,data),(root/(stage+'.binding.json'),core.encoded(binding))):
            if file.exists():
                core.require(not file.is_symlink() and file.stat().st_size==len(payload) and file.read_bytes()==payload,
                             'immutable config snapshot changed: '+str(file))
            else:
                with file.open('xb') as stream:
                    stream.write(payload); stream.flush(); text_stage.os.fsync(stream.fileno())
    return path


def interpreter(resolved,stage):
    value=(resolved['vllm_python'] if stage=='vllm' else None) or resolved['python'] or sys.executable
    found=shutil.which(value)
    core.require(found is not None,'configured Python executable unavailable: '+value)
    return found


def child_command(resolved,stage,phase,config_path,rows=None):
    allowed={'stage01':{'pilot','run'},'stage02':{'pilot','run'},
             'stage03':{'pilot','run','cache'},'vllm':{'print','serve'}}
    core.require(phase in allowed[stage],'unsupported phase for '+stage)
    scripts={'stage01':'mechanical_clean.py','stage02':'text_qa_clean.py',
             'stage03':'media_qa_clean.py','vllm':'vllm_service.py'}
    if stage=='vllm':
        core.require(resolved['service_mode']=='managed',
                     'existing API mode never starts a service; set api.service_mode=managed to launch locally')
        api=urlparse(resolved['stage03']['api_base'])
        core.require(api.scheme=='http' and api.hostname in ('127.0.0.1','localhost'),
                     'managed service binds HTTP127.0.0.1; use existing mode for remote/HTTPS APIs')
    cmd=[interpreter(resolved,stage),'-B','-u',str(core.REPO/'scripts'/scripts[stage]),
         phase,'--config',str(config_path)]
    if rows is not None:
        core.require(stage!='vllm' and phase in ('pilot','cache') and type(rows) is int and 1<=rows<=10000,
                     '--rows only supports pilot/cache,1..10000')
        cmd+=['--rows',str(rows)]
    return cmd


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    subs=parser.add_subparsers(dest='command',required=True)
    init=subs.add_parser('init-config'); init.add_argument('path',type=Path)
    show=subs.add_parser('show'); show.add_argument('--config',type=Path,required=True)
    for stage in ('stage01','stage02','stage03','vllm'):
        p=subs.add_parser(stage); p.add_argument('--config',type=Path,required=True)
        phases=('print','serve') if stage=='vllm' else (('pilot','run','cache') if stage=='stage03' else ('pilot','run'))
        p.add_argument('--phase',choices=phases,default='print' if stage=='vllm' else 'pilot')
        if stage!='vllm': p.add_argument('--rows',type=int)
    args=parser.parse_args()
    if args.command=='init-config':
        path=absolute(str(args.path),'config path'); core.save(path,DEFAULT)
        emit('CONFIG_CREATED',{'path':str(path),'template':'configs/pipeline.example.json'}); return
    c=load(args.config); resolved=resolve(c)
    if args.command=='show':
        emit('RESOLVED',dict(resolved,media_scan=False,model_calls=0,writes=0)); return
    # Validate command/rows before writing a snapshot or starting a child.
    command=child_command(resolved,args.command,args.phase,Path('unused.json'),getattr(args,'rows',None))
    path=snapshot(c,resolved,args.command)
    command[command.index('--config')+1]=str(path)
    emit('EXECUTE',{'stage':args.command,'phase':args.phase,'effective_config':str(path),'argv':command})
    result=subprocess.run(command,check=False)
    core.require(result.returncode==0,'child stage failed with status '+str(result.returncode))
    emit('PASS',{'stage':args.command,'phase':args.phase})


if __name__=='__main__':
    try: main()
    except Exception as exc:
        emit('FAIL',{'error':type(exc).__name__+': '+str(exc),'original_writes':0}); sys.exit(1)
