"""Windowless guest input; text insertion deliberately stays window-bound."""
import math

MODIFIERS={'ctrl','alt','shift','win'}
MOUSE_ACTIONS={'click','double_click','right_click','scroll','drag','move'}


def definitions():
    number={'type':'integer','minimum':0};text={'type':'string'}
    base={'capture':text,'coordinate_space':{'type':'string','enum':['screenshot_pixels','normalized_1000','view_pixels']},'view_id':text}
    fields={**base,'action':{'type':'string','enum':sorted(MOUSE_ACTIONS)},**{k:number for k in ('x','y','from_x','from_y','to_x','to_y')},
            'duration_ms':{'type':'integer','minimum':1,'maximum':2000},'direction':{'type':'string','enum':['up','down','left','right']},'amount':{'type':'integer','minimum':1,'maximum':30}}
    return {'name':'desktop_mouse','description':'宽松桌面鼠标，不传pid/window_id、不激活窗口。使用新鲜桌面capture及坐标：click/double_click/right_click/move填x,y；scroll另填direction,amount；drag只填from_x,from_y,to_x,to_y,duration_ms。操作后回读，投递不等于完成。原click等仍为绑定窗口的严格操作。',
            'parameters':{'type':'object','properties':fields,'required':['action','capture','coordinate_space'],'additionalProperties':False}}


def key_arguments(tool,args):
    # Old explicit targets are accepted only for compatibility and discarded;
    # they must never be activated by this public shortcut route.
    if tool=='hotkey':
        keys=args.get('keys',[])
        if not isinstance(keys,list) or not 2<=len(keys)<=5:raise ValueError('Hotkey requires modifiers and one key')
        key,mods=keys[-1],keys[:-1]
    else:key,mods=args.get('key'),args.get('modifiers',[])
    if not isinstance(key,str) or not key.strip() or len(key)>64 or not isinstance(mods,list) or len(mods)>4 or any(m not in MODIFIERS for m in mods) or len(set(mods))!=len(mods):raise ValueError('Invalid desktop key syntax')
    return {'action':'key','key':key.lower(),'modifiers':mods}


def validate_native(args):
    if not isinstance(args,dict):raise ValueError('Desktop input object required')
    action=args.get('action')
    if action=='key':
        if set(args)!={'action','key','modifiers'}:raise ValueError('Desktop keys cannot contain text, coordinates or window targets')
        key_arguments('press_key',args);return
    if action not in MOUSE_ACTIONS:raise ValueError('Unknown desktop mouse action')
    fields={'action','from_x','from_y','to_x','to_y','duration_ms'} if action=='drag' else {'action','x','y'}
    if action=='scroll':fields|={'direction','amount'}
    if set(args)!=fields:raise ValueError('Unexpected or missing desktop mouse parameters')
    for k in fields&{'x','y','from_x','from_y','to_x','to_y'}:
        if type(args[k]) not in (int,float) or not math.isfinite(args[k]) or args[k]<0:raise ValueError('Invalid desktop coordinate')
    if action=='drag' and (type(args['duration_ms']) is not int or not 1<=args['duration_ms']<=2000):raise ValueError('Invalid desktop drag duration')
    if action=='scroll' and (args['direction'] not in {'up','down','left','right'} or type(args['amount']) is not int or not 1<=args['amount']<=30):raise ValueError('Invalid desktop scroll')


def driver_call(args):
    validate_native(args);p={k:v for k,v in args.items() if k!='action'};p['scope']='desktop'
    action=args['action']
    if action=='key':return 'press_key',p
    if action=='move':return 'move_cursor',p
    if action in {'click','double_click','right_click'}:
        p.update(button='right' if action=='right_click' else 'left',count=2 if action=='double_click' else 1)
        return 'click',p
    if action=='scroll':p['by']='line'
    return action,p
