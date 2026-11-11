"""Portable JSON tool descriptions; deliberately independent of any agent SDK."""
def definitions():
    text={'type':'string'};integer={'type':'integer','minimum':1}
    target={'pid':integer,'window_id':integer}
    def obj(properties,required=()):return {'type':'object','properties':properties,'required':list(required),'additionalProperties':False}
    descriptions={
        'list_apps':('List guest applications; check running instead of treating installed apps as running.',obj({})),
        'list_windows':('List actual open guest windows.',obj({'app':text,'pid':integer})),
        'get_window_state':('Read UIA elements and optionally capture a window. References are snapshot-bound.',obj({**target,'tree_format':{'enum':['elements','markdown']},'include_screenshot':{'type':'boolean'},'full_output':{'type':'boolean'},'max_depth':integer,'max_elements':integer,'timeout_ms':integer,'since':text},target)),
        'get_desktop_state':('Capture guest desktop, not host.',obj({'include_screenshot':{'type':'boolean'}})),
        'get_screen_size':('Read guest screen dimensions and scale.',obj({})),
        'get_cursor_position':('Read guest real pointer position.',obj({})),
        'health_report':('Read guest Driver health.',obj({})),
        'launch_app':('Launch a guest EXE without shell syntax or launch arguments.',obj({'name':text},['name'])),
        'bring_to_front':('Activate exact guest PID/HWND; activation does not prove editor focus.',obj(target,target)),
        'type_text':('Append text to the foreground field. Does not select or clear text.',obj({**target,'text':text},[*target,'text'])),
        'press_key':('Send one foreground key with optional modifiers.',obj({**target,'key':text,'modifiers':{'type':'array','items':{'enum':['ctrl','alt','shift']}}},[*target,'key'])),
        'hotkey':('Send foreground modifier-plus-key sequence.',obj({**target,'keys':{'type':'array','items':text}},[*target,'keys'])),
        'click':('Click a fresh element token or original capture pixel coordinates; provide capture separately for pixels.',obj({**target,'element_token':text,'x':{'type':'number'},'y':{'type':'number'},'button':text},target)),
        'double_click':('Double click a fresh target and collect a post-readback.',obj({**target,'element_token':text,'x':{'type':'number'},'y':{'type':'number'}},target)),
        'right_click':('Right click a fresh target and collect a post-readback.',obj({**target,'element_token':text,'x':{'type':'number'},'y':{'type':'number'}},target)),
        'move_cursor':('Move the real desktop pointer using capture-grounded screen pixels.',obj({'scope':{'const':'desktop'},'x':{'type':'number'},'y':{'type':'number'}},['scope','x','y'])),
        'drag':('Drag using original screenshot coordinates and a separate fresh capture. scope=desktop selects native desktop input.',obj({**target,'scope':{'enum':['window','desktop']},**{n:{'type':'number'} for n in ['from_x','from_y','to_x','to_y']},'duration_ms':{'type':'integer','minimum':1,'maximum':2000}},[*target,'from_x','from_y','to_x','to_y'])),
        'scroll':('Scroll a guest window at capture-grounded screenshot coordinates, or its foreground viewport.',obj({**target,'x':{'type':'number'},'y':{'type':'number'},'direction':{'enum':['up','down','left','right']},'amount':{'type':'integer','minimum':1,'maximum':50},'by':{'enum':['line','page']}},[*target,'direction'])),
        'run_command':('Run PowerShell only inside the guest workspace. This directory is not a security sandbox.',obj({'script':text,'timeout_ms':{'type':'integer','minimum':100,'maximum':120000},'output_limit':{'type':'integer','minimum':256,'maximum':65536}},['script'])),
        'find_elements':('Bounded local UIA search; incomplete collection is not proof of absence.',obj({**target,'query':text,'role':text,'scope':{'enum':['window','browser','web']},'match':{'enum':['contains','exact']},'fields':{'type':'array','items':{'enum':['label','value','automation_id']}},'max_results':{'type':'integer','minimum':1,'maximum':20}},[*target,'query'])),
        'scroll_to_element':('At most six wheel gestures guided by fresh UIA geometry; no click and no model.',obj({**target,'element_token':text},[*target,'element_token'])),
    }
    from .vm_tools import definitions as vm_definitions
    for name in ('click','double_click','right_click','move_cursor','drag','scroll'):
        descriptions[name][1]['properties'].update(coordinate_space={'type':'string','enum':['screenshot_pixels','view_pixels']},view_id=text)
    from .guest_commands import definitions as command_definitions
    from .desktop_inputs import definitions as desktop_definition
    legacy=descriptions.pop('run_command')[1]
    for name in ('press_key','hotkey'):
        descriptions[name][1]['required']=[k for k in descriptions[name][1]['required'] if k not in target]
        descriptions[name]=(descriptions[name][0]+' Keys target the guest active desktop without window activation. Text insertion stays window-bound.',descriptions[name][1])
    descriptions['press_key'][1]['properties']['modifiers']['items']['enum']=['ctrl','alt','shift','win']
    tools=[{'name':name,'description':d,'parameters':schema} for name,(d,schema) in descriptions.items()]+vm_definitions()+command_definitions(legacy)+[desktop_definition()]
    tools += [{'name':name,'description':'Query or cancel the original ordinary guest command; never replay it.','parameters':obj({'action_id':text},['action_id'])} for name in ('command_status','command_cancel')]
    from .batching import definitions as workflow_definitions
    return tools+workflow_definitions([t['name'] for t in tools]+['screenshot_view'])
