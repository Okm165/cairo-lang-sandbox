import os
import subprocess
import sys

cmd = [
    sys.executable, "-u", "-s", "-m", 'starkware.cairo.lang.create_cairo_lang_zip'
] + sys.argv[1:]
proc = subprocess.run(cmd)
sys.exit(proc.returncode)
