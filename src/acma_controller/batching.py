"""Ordered, bounded batches. References resolve only against earlier results."""
import copy
import time

ALLOWED={'list_apps','list_windows','get_window_state','get_desktop_state','get_screen_size',
         'get_cursor_position','health_report','launch_app','bring_to_front','click','double_click',
         'right_click','type_text','replace_field','press_key','hotkey','scroll','drag','desktop_drag',
         'move_cursor','find_elements','scroll_to_element','screenshot_view'}


def definitions(supported=None):
    return [
        {'name':'execute_batch','description':'顺序执行最多16个已接入桌面动作；不是并发。失败、结果未知、验证不匹配、暂停或到期立即停止，余项逐项标为not_executed，不重试。不含VM生命周期、异步命令、计划或嵌套批次。可用{"$result":0,"path":"/matches/0/element_token"}引用前面子结果中的新引用；索引从0开始。',
         'parameters':{'type':'object','properties':{'actions':{'type':'array','minItems':1,'maxItems':16,'items':{
             'type':'object','properties':{'tool':{'type':'string','enum':sorted(ALLOWED if supported is None else ALLOWED&set(supported))},'arguments':{'type':'object'}},
             'required':['tool','arguments'],'additionalProperties':False}}},'required':['actions'],'additionalProperties':False}},
        {'name':'screenshot_view','description':'只读生成已登记截图的模型视图，默认最大1280×960、120万像素；region=[左,上,右,下]为原始截图像素，可局部保留原图细节。返回原capture、view_id、尺寸、裁剪和映射。视图不会延长原截图有效期。随后像素操作用coordinate_space=view_pixels及view_id，仍受原capture与窗口检查。',
         'parameters':{'type':'object','properties':{'capture':{'type':'string'},'region':{'type':'array','minItems':4,'maxItems':4,'items':{'type':'integer','minimum':0}},
             'max_width':{'type':'integer','minimum':64,'maximum':2560},'max_height':{'type':'integer','minimum':64,'maximum':2560}},'required':['capture'],'additionalProperties':False}}
    ]


def pointer(value,path):
    if not isinstance(path,str) or len(path)>512 or (path and not path.startswith('/')):raise ValueError('Invalid result JSON pointer')
    for key in path.split('/')[1:] if path else []:
        key=key.replace('~1','/').replace('~0','~')
        if isinstance(value,list):
            if not key.isdecimal():raise ValueError('Invalid result array index')
            value=value[int(key)]
        elif isinstance(value,dict):value=value[key]
        else:raise ValueError('Result path is not traversable')
    return value


def resolve(value,results,index,depth=0):
    if depth>16:raise ValueError('Batch argument nesting exceeds 16')
    if isinstance(value,dict):
        if '$result' in value:
            if set(value)!={'$result','path'} or type(value['$result']) is not int or not 0<=value['$result']<index:
                raise ValueError('References must point to an earlier completed result')
            return copy.deepcopy(pointer(results[value['$result']]['result'],value['path']))
        return {k:resolve(v,results,index,depth+1) for k,v in value.items()}
    if isinstance(value,list):return [resolve(v,results,index,depth+1) for v in value]
    return value


def disposition(result):
    if not isinstance(result,dict):return 'unknown'
    state=result.get('state');effect=result.get('effect');verification=(result.get('verification') or {}).get('status')
    execution=result.get('execution') or {}
    if result.get('error') or result.get('failure') or result.get('refusal') or state in {'failed','refused','unknown','running','queued','unverified','cancelled','timed_out','stopped'}:
        return 'failed' if state in {'failed','refused','cancelled','timed_out','stopped'} or result.get('refusal') else 'unknown'
    if effect in {'unknown','refused','unverifiable'} or execution.get('effect')=='unknown' or verification in {'unknown','mismatch','refused','waiting','unverifiable'}:
        return 'failed' if verification in {'mismatch','refused'} or effect=='refused' else 'unknown'
    if (result.get('post_readback') or {}).get('readback_error'):return 'unknown'
    if (result.get('post_readback') or {}).get('state') not in {None,'succeeded'}:return 'unknown'
    return 'succeeded'


def execute(actions,dispatch,guard,seconds=30):
    if not isinstance(actions,list) or not 1<=len(actions)<=16:raise ValueError('Batch needs 1..16 actions')
    for item in actions:
        if not isinstance(item,dict) or set(item)!={'tool','arguments'} or item['tool'] not in ALLOWED or not isinstance(item['arguments'],dict):
            raise ValueError('Invalid or unsupported batch action; no action dispatched')
    started=time.monotonic();results=[];halt=None
    for index,item in enumerate(actions):
        step_start=time.monotonic()
        if halt is None:
            try:
                guard()
                if time.monotonic()-started>=seconds:raise ValueError('Batch deadline reached')
            except Exception as error:halt={'index':index,'reason':str(error),'state':'interrupted'}
        if halt is not None:
            results.append({'index':index,'tool':item['tool'],'status':'not_executed','dispatched':False,'reason':halt['reason'],'seconds':0})
            continue
        try:
            args=resolve(item['arguments'],results,index)
        except (ValueError,KeyError,IndexError) as error:
            halt={'index':index,'reason':str(error),'state':'failed'}
            results.append({'index':index,'tool':item['tool'],'status':'refused','dispatched':False,'reason':str(error),'seconds':0});continue
        try:result=dispatch(item['tool'],args)
        except Exception as error:result={'state':'unknown','error':str(error),'retry_action':False}
        state=disposition(result)
        results.append({'index':index,'tool':item['tool'],'status':state,'seconds':round(time.monotonic()-step_start,3),'result':result})
        if state!='succeeded':halt={'index':index,'reason':'Earlier action failed or its result is not confirmed; inspect evidence, do not replay completed steps','state':state}
    return {'status':'completed' if halt is None else 'interrupted' if halt['state']=='interrupted' else 'halted',
            'steps':results,'completed_count':sum(s['status']=='succeeded' for s in results),
            'not_executed_count':sum(s['status']=='not_executed' for s in results),'halt':halt,
            'seconds':round(time.monotonic()-started,3),'retry_action':False,'atomic':False,
            'instruction':'Completed steps are not rolled back. Continue from evidence; never replay the whole batch after partial execution.'}
