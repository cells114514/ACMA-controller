from pathlib import Path
import tempfile
import unittest
try:from PIL import Image
except ImportError:Image=None
from acma_controller.visuals import prepare,point,arguments


@unittest.skipIf(Image is None,'Install the imaging extra for screenshot processing tests')
class ViewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source.png'
        Image.new('RGB',(3200,2000),'blue').save(self.source)

    def test_resize_is_bounded_and_reversible(self):
        view=prepare(self.source,self.root/'views','capture')
        self.assertEqual(view['image_size'],[1280,800])
        self.assertEqual(point(view,100,100),[250,250])
        with Image.open(view['image_path']) as image:self.assertEqual(image.size,(1280,800))

    def test_crop_has_original_offset_and_stable_identity(self):
        view=prepare(self.source,self.root/'views','capture',region=[100,200,500,600])
        self.assertEqual(point(view,50,20),[150,220])
        again=prepare(self.source,self.root/'views','capture',region=[100,200,500,600])
        self.assertEqual(view['view_id'],again['view_id'])
        mapped=arguments({'capture':'capture','view_id':view['view_id'],'x':50,'y':20},view)
        self.assertEqual((mapped['x'],mapped['y']),(150,220));self.assertNotIn('view_id',mapped)

    def test_crop_and_point_bounds_are_enforced(self):
        with self.assertRaises(ValueError):prepare(self.source,self.root/'views','capture',region=[0,0,4000,600])
        view=prepare(self.source,self.root/'views','capture')
        for x in (-1,1280,float('nan')):
            with self.assertRaises(ValueError):point(view,x,20)
        with self.assertRaises(ValueError):arguments({'capture':'other','x':0,'y':0},view)
