"""Build a standalone Windows CMD containing the complete release ZIP."""
from __future__ import annotations
import base64
import hashlib
from pathlib import Path
import textwrap


BOOTSTRAP = r'''
import base64
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

def main():
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required.")
    installer = Path(os.environ["UIBE_SETUP_FILE"]).resolve()
    content = installer.read_bytes()
    encoded = content.split(b"\r\n::UIBE_PAYLOAD_BEGIN\r\n", 1)[1]
    payload = base64.b64decode(b"".join(encoded.split()), validate=True)
    if hashlib.sha256(payload).hexdigest() != "PAYLOAD_SHA256":
        raise RuntimeError("Installer payload is incomplete. Download the complete file again.")
    verify = os.environ.get("UIBE_SETUP_VERIFY_ONLY") == "1"
    stage = (Path(os.environ["UIBE_SETUP_TEST_DIR"]).resolve() if verify
             else Path(tempfile.mkdtemp(prefix="uibe-lecture-setup-")).resolve())
    stage.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for entry in archive.infolist():
            dest = (stage / entry.filename).resolve()
            if not dest.is_relative_to(stage):
                raise RuntimeError("Invalid package path")
        archive.extractall(stage)
    root = stage / "uibe-lecture-mcp"
    required = (root / "scripts/install.py", root / "run.py", root / "uibe_lecture/parser.py")
    if not all(p.is_file() for p in required):
        raise RuntimeError("Installer package is missing required files")
    if verify:
        print("VERIFY_OK: full package extracted; installer and application files exist.")
        return 0
    print("Package verified. Setup will ask whether to configure Codex (default: No).", flush=True)
    return subprocess.call([sys.executable, str(root / "scripts/install.py")], cwd=root)

try:
    result = main()
except Exception as error:
    print("安装未完成：", error, flush=True)
    result = 1
raise SystemExit(result)
'''


def build(archive: Path, destination: Path):
    payload = archive.read_bytes()
    bootstrap = BOOTSTRAP.replace("PAYLOAD_SHA256", hashlib.sha256(payload).hexdigest())
    encoded_bootstrap = base64.b64encode(bootstrap.encode("utf-8")).decode("ascii")
    encoded_payload = base64.b64encode(payload).decode("ascii")
    launcher = '''@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "UIBE_SETUP_FILE=%~f0"
python -c "import base64,os;from pathlib import Path;s=Path(os.environ['UIBE_SETUP_FILE']).read_bytes().split(b'::UIBE_BOOTSTRAP_BEGIN\\r\\n',1)[1].split(b'\\r\\n::UIBE_BOOTSTRAP_END',1)[0];exec(compile(base64.b64decode(s),'uibe_setup_bootstrap','exec'))"
set "UIBE_SETUP_RESULT=%ERRORLEVEL%"
if "%UIBE_SETUP_VERIFY_ONLY%"=="1" exit /b %UIBE_SETUP_RESULT%
echo.
if not "%UIBE_SETUP_RESULT%"=="0" echo Installation did not finish. See the message above.
pause
exit /b %UIBE_SETUP_RESULT%
::UIBE_BOOTSTRAP_BEGIN
'''
    body = (launcher + "\n".join(textwrap.wrap(encoded_bootstrap, 120))
            + "\n::UIBE_BOOTSTRAP_END\n::UIBE_PAYLOAD_BEGIN\n"
            + "\n".join(textwrap.wrap(encoded_payload, 120)) + "\n")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
    return destination


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(build(args.archive, args.output).resolve())
