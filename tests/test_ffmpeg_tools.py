from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import ffmpeg_tools as f


class RunnerTests(unittest.TestCase):
    def test_stdout_stderr_can_be_separated_for_pts(self):
        output=f.run_command([sys.executable,'-c','import sys; print("out"); print("err",file=sys.stderr)'],5,True)
        self.assertEqual(tuple(x.strip() for x in output),('out','err'))

    def test_nonzero_status_does_not_look_like_a_frame(self):
        with self.assertRaisesRegex(RuntimeError,'exited 3'):
            f.run_command([sys.executable,'-c','raise SystemExit(3)'],5)

    def test_timeout_kills_only_owned_child(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            f.run_command([sys.executable,'-c','import time; time.sleep(2)'],.05)

    def test_output_cap_while_child_runs(self):
        with self.assertRaisesRegex(RuntimeError,'exceeds1MiB'):
            f.run_command([sys.executable,'-c','import sys; sys.stdout.write("x"*(2*1024*1024))'],5)

    def test_blur_label_requires_calibrated_threshold(self):
        self.assertIsNone(f.label(5.0,None))
        self.assertFalse(f.label(5.0,5.0))
        self.assertTrue(f.label(5.1,5.0))

    def test_detect_labels_eight_successful_frames(self):
        with tempfile.NamedTemporaryFile(suffix='.mp4') as media:
            media.write(b'video'); media.flush()
            frames=iter('lavfi.blur=5.0\npts_time:%s' % (i/10) for i in range(1,9))
            runtime={'ffmpeg':'ffmpeg','ffprobe':'ffprobe'}
            metadata={'duration':2.0,'pts_origin':0.0,'width':1,'height':1,'codec':'x'}
            with patch.object(f,'probe',return_value=metadata), \
                 patch.object(f,'run_command',side_effect=lambda *a,**k: next(frames)):
                result=f.detect({'path':media.name,'start':None,'end':None},runtime,4.0)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['valid_frames'],8)
        self.assertTrue(result['is_blurry'])
        self.assertAlmostEqual(result['mean_blur'],5.0)


if __name__=='__main__': unittest.main()
