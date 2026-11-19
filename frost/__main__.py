import argparse
import json
import os
from pathlib import Path

from frost.evidence import Evidence
from frost.service import Service
from frost.worker import Worker


def main():
    parser = argparse.ArgumentParser(description="霜讯采集服务")
    parser.add_argument("--database", default=os.environ.get("FROST_DATABASE", "state/frost.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="启动管理服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    worker = commands.add_parser("worker", help="执行到期来源采集")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--poll", type=int, default=5)
    worker.add_argument("--batch", type=int, default=10)
    commands.add_parser("check", help="检查持久化业务引用")
    export = commands.add_parser("export", help="导出完整采集证据包")
    export.add_argument("run_id")
    export.add_argument("destination")
    verify = commands.add_parser("verify-bundle", help="核验导出证据字节")
    verify.add_argument("path")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn
        from frost.api import create_app
        uvicorn.run(create_app(args.database), host=args.host, port=args.port)
        return 0
    service = Service(args.database)
    if args.command == "worker":
        result = Worker(service, args.poll, args.batch).with_signals(args.once)
    elif args.command == "check":
        result = service.verify()
    elif args.command == "export":
        body = Evidence(service.ledger).bundle(args.run_id)
        with Path(args.destination).open("xb") as output:
            output.write(body)
        result = {"path": str(Path(args.destination).resolve()), "bytes": len(body)}
    else:
        path = Path(args.path)
        if path.stat().st_size > 17825792:
            parser.error("证据包超过读取预算")
        result = Evidence(service.ledger).verify_bundle(path.read_bytes())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result.get("ok") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
