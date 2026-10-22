"""Installed console entry point retaining the standalone command's arguments."""

import os
from pathlib import Path
import subprocess
import sys


def main():
    env = os.environ.copy()
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    root = str(Path(__file__).resolve().parent.parent)
    env['PYTHONPATH'] = root + os.pathsep + env.get('PYTHONPATH', '')
    return subprocess.call([sys.executable, '-m', 'blflashcommand._worker', '--cli', *sys.argv[1:]], env=env)
