#!/usr/bin/env python3
"""构建三个 FunctionGraph Python ZIP，并保证相同源码产生相同摘要。"""

import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
BUILD = ROOT / "src" / "build"
FUNCTIONS = ("tenant_admin", "catalog_api", "events_audit")
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def build_one(name):
    source = SRC / (name + ".py")
    archive = BUILD / (name + ".zip")
    source_bytes = source.read_bytes()
    compile(source_bytes, str(source), "exec")

    info = zipfile.ZipInfo(source.name, date_time=ZIP_TIME)
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr(info, source_bytes)

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return {"file": str(archive.relative_to(ROOT)).replace("\\", "/"), "sha256": digest, "bytes": archive.stat().st_size}


def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    result = {name: build_one(name) for name in FUNCTIONS}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
