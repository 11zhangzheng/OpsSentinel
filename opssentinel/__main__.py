import argparse
import os
from pathlib import Path

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="OpsSentinel monitoring and controlled recovery")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path(".opssentinel"))
    parser.add_argument("--config", type=Path)
    demo = parser.add_mutually_exclusive_group()
    demo.add_argument("--demo", dest="demo", action="store_true", help="Run an isolated local HTTP fault exercise")
    demo.add_argument("--no-demo", dest="demo", action="store_false")
    parser.set_defaults(demo=False)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口应在 1–65535 之间")
    if args.host not in {"localhost", "127.0.0.1", "::1"} and len(os.getenv("OPS_API_TOKEN", "")) < 24:
        parser.error("非本机监听必须配置至少 24 字符的 OPS_API_TOKEN")
    from .app import create_app
    uvicorn.run(create_app(data_dir=args.data_dir, demo=args.demo, config_path=args.config),
                host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
