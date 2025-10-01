from pathlib import Path
import subprocess
import sys

candidate = Path(sys.argv[1])
print('==>', candidate)
cmd = [
    'ffprobe',
    '-v', 'error',
    '-show_entries', 'format=duration',
    '-of', 'default=noprint_wrappers=1:nokey=1',
    str(candidate),
]
print(' '.join(cmd))
proc = subprocess.run(cmd, capture_output=True, text=True)
print('stdout:', proc.stdout)
print('stderr:', proc.stderr)
print('returncode:', proc.returncode)
