"""Model-neutral screenshot views and explicit reversible pixel transforms."""
import hashlib
import json
import math
from pathlib import Path
import struct


def prepare(path,output,source_capture,*,region=None,max_width=1280,max_height=960,max_pixels=1200000):
    path=Path(path).resolve();output=Path(output)
    raw=path.read_bytes()
    if not 24<=len(raw)<=32*1024*1024 or raw[:8]!=b'\x89PNG\r\n\x1a\n':raise ValueError('Expected registered PNG up to 32MiB')
    width,height=struct.unpack('>II',raw[16:24])
    if min(width,height)<=0 or width*height>64000000:raise ValueError('Screenshot dimensions exceed processing budget')
    if type(max_width) is not int or type(max_height) is not int or not 64<=max_width<=2560 or not 64<=max_height<=2560:raise ValueError('View dimensions must be 64..2560')
    region=list(region) if region is not None else [0,0,width,height]
    if len(region)!=4 or any(type(x) is not int for x in region) or not (0<=region[0]<region[2]<=width and 0<=region[1]<region[3]<=height):raise ValueError('Crop is outside original screenshot')
    rw,rh=region[2]-region[0],region[3]-region[1]
    scale=min(1,max_width/rw,max_height/rh,math.sqrt(max_pixels/(rw*rh)))
    size=[max(1,int(rw*scale)),max(1,int(rh*scale))]
    signature=hashlib.sha256(raw).hexdigest()
    identifier=hashlib.sha256(json.dumps([source_capture,signature,region,size],separators=(',',':')).encode()).hexdigest()[:32]
    output.mkdir(parents=True,exist_ok=True)
    image=output/(identifier+'.png')
    if not image.exists():
        if region==[0,0,width,height] and size==[width,height]:image.write_bytes(raw)
        else:
            try:from PIL import Image
            except ImportError as error:raise ValueError('Screenshot transforms require the optional imaging dependency (Pillow)') from error
            with Image.open(path) as original:
                original.load()
                view=original.crop(tuple(region)).convert('RGB')
                if list(view.size)!=size:view=view.resize(tuple(size),Image.Resampling.LANCZOS)
                view.save(image,format='PNG',optimize=True)
    metadata={'view_id':identifier,'source_capture':source_capture,'source_sha256':signature,
              'source_size':[width,height],'region':region,'image_size':size,
              'source_pixels_per_view_pixel':[rw/size[0],rh/size[1]],
              'image_path':str(image.resolve()),'coordinate_space':'view_pixels',
              'authority':'derived view only; original capture identity, freshness and target checks still apply'}
    (output/(identifier+'.json')).write_text(json.dumps(metadata,ensure_ascii=False),encoding='utf-8')
    return metadata


def load(output,identifier):
    if not isinstance(identifier,str) or len(identifier)!=32 or any(c not in '0123456789abcdef' for c in identifier):raise ValueError('Invalid view ID')
    return json.loads((Path(output)/(identifier+'.json')).read_text(encoding='utf-8'))


def point(view,x,y):
    width,height=view['image_size']
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in (x,y)) or not (0<=x<width and 0<=y<height):raise ValueError('Point outside model view')
    left,top,right,bottom=view['region'];sx,sy=view['source_pixels_per_view_pixel']
    return [min(right-1,round(left+x*sx)),min(bottom-1,round(top+y*sy))]


def arguments(args,view):
    if args.get('capture')!=view['source_capture']:raise ValueError('View belongs to another source capture')
    result=dict(args);result.pop('view_id',None);result['coordinate_space']='screenshot_pixels'
    pairs=[('from_x','from_y'),('to_x','to_y')] if 'from_x' in args else [('x','y')]
    for x,y in pairs:
        if x not in args or y not in args:raise ValueError('Coordinate pair required')
        result[x],result[y]=point(view,args[x],args[y])
    return result
