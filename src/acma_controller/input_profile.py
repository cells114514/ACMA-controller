"""Controller-derived input requirements, never trusted model declarations."""
MOUSE={'click','double_click','right_click','drag','scroll','move_cursor'}
KEYBOARD={'type_text','set_value','press_key','hotkey'}


def infer(spec):
    tool=spec.get('tool');args=spec.get('arguments',{})
    if tool=='desktop_input':return {'mouse':args.get('action')!='key','keyboard':args.get('action')=='key'}
    mouse=spec.get('kind')=='desktop' and tool in MOUSE
    keyboard=spec.get('kind')=='desktop' and tool in KEYBOARD
    if mouse and any(args.get(k) for k in ('modifier','modifiers')):keyboard=True
    if keyboard and any(k in args for k in ('x','y')):mouse=True
    return {'mouse':mouse,'keyboard':keyboard}


def bind(spec):
    expected=infer(spec)
    declared=spec.get('input')
    if declared is not None and (not isinstance(declared,dict) or set(declared)!={'mouse','keyboard'}
            or any(type(v) is not bool for v in declared.values()) or declared!=expected):
        raise ValueError('Input declaration must match controller-derived tool requirements')
    spec['input']=expected
    return expected
