"""폴더 정리 전 명령을 지원하는 데모 진입점."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.demo.demo_verify import *

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
