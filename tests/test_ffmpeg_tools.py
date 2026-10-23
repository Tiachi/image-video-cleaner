from pathlib import Path
import subprocess
import sys
import unittest

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


if __name__=='__main__': unittest.main()
