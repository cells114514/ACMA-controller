"""Model-free, bounded scrolling with fresh UIA geometry after each gesture."""
import time


def rect(frame):
    if not frame or not all(k in frame for k in ('x','y','w','h')):return None
    if frame['w']<=0 or frame['h']<=0:return None
    return (frame['x'],frame['y'],frame['x']+frame['w'],frame['y']+frame['h'])


def intersection(a,b):
    if a is None or b is None:return None
    r=(max(a[0],b[0]),max(a[1],b[1]),min(a[2],b[2]),min(a[3],b[3]))
    return r if r[2]>r[0] and r[3]>r[1] else None


def identity(element):
    result={k:element.get(k) for k in ('role','label','automation_id','in_web_content')}
    if element.get('role') in {'Hyperlink','Document'}:result['value']=element.get('value')
    return result


def locate(state,signature):
    matches=[e for e in state.get('elements',[]) if identity(e)==signature]
    return matches[0] if len(matches)==1 else None


def geometry(state,element):
    b=state.get('window_bounds',{})
    if not all(k in b for k in ('x','y','width','height')):return {'status':'unknown','reason':'window_bounds_missing'}
    viewport=(b['x'],b['y'],b['x']+b['width'],b['y']+b['height'])
    indexed={e.get('element_index'):e for e in state.get('elements',[]) if type(e.get('element_index')) is int}
    parent=element.get('parent_index');seen=set();container=None
    while parent in indexed and parent not in seen:
        seen.add(parent);p=indexed[parent];r=rect(p.get('frame'))
        if r and p.get('role') in {'Document','Edit','List','Tree','Pane','Table','DataGrid'} and 'scroll' in p.get('actions',[]):
            if container is None:container=p
            viewport=intersection(viewport,r)
        parent=p.get('parent_index')
    # The target may itself be a viewport-sized editor/document. This does not
    # establish that text inside that element is visible.
    if container is None and element.get('role') in {'Edit','Document','List','Tree','Pane','Table','DataGrid'} and 'scroll' in element.get('actions',[]):
        container=element;viewport=intersection(viewport,rect(element.get('frame')))
    if container is None or viewport is None:return {'status':'unsupported','reason':'scroll_container_not_observable'}
    target=rect(element.get('frame'))
    if target is None:return {'status':'unknown','reason':'target_bounds_missing'}
    hit=intersection(target,viewport)
    fully=target[0]>=viewport[0] and target[1]>=viewport[1] and target[2]<=viewport[2] and target[3]<=viewport[3]
    status='in_view' if fully else 'partial' if hit else 'outside'
    if element.get('is_offscreen') is True:status='unknown'
    return {'status':status,'target_rect':target,'viewport_rect':viewport,
            'container':{k:container.get(k) for k in ('role','label','element_index')},
            'visibility':'geometric_only_occlusion_unknown','scope':'element_bounds_not_text_substring'}


def scroll_to_element(agent,args,deadline):
    target={k:args[k] for k in ('pid','window_id')}
    pinned=agent.policy.elements[(args['pid'],args['window_id'])][args['element_token']]
    signature=identity(pinned)
    # Retrieval may clip field text; resolve the token in the full source first.
    original=[e for e in getattr(agent,'element_source_state',{}).get('elements',[]) if e.get('element_token')==args['element_token']]
    if len(original)==1:signature=identity(original[0])
    started=time.monotonic();steps=[];previous=None
    result={'strategy':'bounded_wheel','native_scroll_into_view':'not_integrated',
            'steps':steps,'verification':{'status':'unknown','reason':'not_yet_observed'}}
    def finish(status,reason,state=None,element=None,g=None):
        result.update(status=status,reason=reason,scroll_count=len(steps),seconds=round(time.monotonic()-started,3))
        if g:result['geometry']=g
        if element:result['target']={k:element[k] for k in ('role','label','element_token','frame') if k in element}
        if state and element:
            observation={k:state[k] for k in ('app_name','window_title','window_bounds','snapshot_id','pid','window_id') if k in state}
            brief={k:element[k] for k in ('role','label','element_token','frame','enabled','in_web_content','automation_id') if k in element}
            observation.update(elements=[brief],element_count=1,elements_complete=False,search_filtered=True,truncated=False)
            agent.remember_read('get_window_state',target,observation);agent.window_cache=None
        result['verification']={'status':'confirmed' if status in {'already_in_view','scrolled_into_view'} else 'unknown',
                                'reason':'fresh target rectangle contained by observed scroll viewport; occlusion unknown' if status in {'already_in_view','scrolled_into_view'} else reason}
        agent.emit({'kind':'element_scroll','arguments':args,'result':result})
        return result
    while True:
        if agent.dispatcher.mode()!='running':return finish('stopped','controller_not_running')
        if time.time()>=deadline:return finish('budget_exhausted','task_deadline')
        agent.invalidate_references()
        state=agent._call_once('get_window_state',{**target,'include_screenshot':True},deadline,search_collection=True)
        element=locate(state,signature)
        if element is None:return finish('target_lost','semantic_target_missing_or_ambiguous')
        g=geometry(state,element)
        if g['status']=='in_view':return finish('already_in_view' if not steps else 'scrolled_into_view','fresh_geometry',state,element,g)
        if g['status'] in {'unknown','unsupported'}:return finish(g['status'],g['reason'] if 'reason' in g else 'offscreen_flag_conflict',state,element,g)
        t=g['target_rect'];v=g['viewport_rect']
        if g['status']=='partial' and (t[3]-t[1]>v[3]-v[1] or t[2]-t[0]>v[2]-v[0]):return finish('partially_in_view','target_larger_than_viewport',state,element,g)
        position=(tuple(t),tuple(v))
        if previous==position:return finish('no_progress','target_geometry_unchanged_after_scroll',state,element,g)
        if len(steps)>=6 or time.monotonic()-started>=15:return finish('budget_exhausted','six_scrolls_or_15_seconds_between_atomic_calls',state,element,g)
        if t[1]<v[1]:direction='up';distance=v[1]-t[1]
        elif t[3]>v[3]:direction='down';distance=t[3]-v[3]
        else:return finish('unsupported','horizontal_scroll_not_in_first_version',state,element,g)
        height=v[3]-v[1]
        by='page' if distance>=height*.8 else 'line'
        amount=max(1,min(3,int(distance/height))) if by=='page' else max(1,min(10,int(distance/40)+1))
        capture=agent.current_capture
        if not capture or capture.get('pid')!=target['pid'] or capture.get('window_id')!=target['window_id']:return finish('unsupported','current_window_capture_missing',state,element,g)
        b=state['window_bounds']
        point={'capture':capture['capture'],'coordinate_space':'screenshot_pixels',
               'x':int((v[0]+v[2])/2-b['x']),'y':int((v[1]+v[3])/2-b['y'])}
        previous=position
        step={'direction':direction,'by':by,'amount':amount,'before':g}
        # One gesture only. An unknown result raises and ends the operation;
        # there is no retry of that gesture, even if it may have reached the app.
        agent._call_once('scroll',{**target,'direction':direction,'by':by,'amount':amount},deadline,scroll_point=point)
        steps.append(step)
