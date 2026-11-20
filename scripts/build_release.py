"""Build a clean source package from an explicit public-source allowlist."""
from pathlib import Path
import argparse
import tomllib
import zipfile
from build_single_installer import build

# No runtime state, credentials, logs, git metadata, historical verification
# reports, legacy patch launchers, or previously generated installers.
SOURCE_FILES = (
    "uibe_lecture/__main__.py",
    '.gitattributes',
    '.gitignore',
    'AGENTS.md',
    'README.md',
    'example-rule.json',
    'install.cmd',
    'login.cmd',
    'status.cmd',
    'pyproject.toml',
    'run.py',
    'scripts/benchmark_race.py',
    'scripts/build_release.py',
    'scripts/build_single_installer.py',
    'scripts/install.py',
    'tests/conftest.py',
    'tests/fixtures/apply_ready_real_structure.html',
    'tests/test_auth_persistence.py',
    'tests/test_autologin.py',
    'tests/test_cas_browser.py',
    'tests/test_core.py',
    'tests/test_install_mcp.py',
    'tests/test_login_navigation.py',
    'tests/test_onboarding_worker.py',
    'tests/test_pool_recovery.py',
    'tests/test_race_engine.py',
    'tests/test_reliability.py',
    'tests/test_worker.py',
    'uibe_lecture/__init__.py',
    'uibe_lecture/auth.py',
    'uibe_lecture/autologin.py',
    'uibe_lecture/cli.py',
    'uibe_lecture/client.py',
    'uibe_lecture/credentials.py',
    'uibe_lecture/engine.py',
    'uibe_lecture/mcp_server.py',
    'uibe_lecture/models.py',
    'uibe_lecture/onboarding.py',
    'uibe_lecture/pacing.py',
    'uibe_lecture/parser.py',
    'uibe_lecture/service.py',
    'uibe_lecture/startup.py',
    'uibe_lecture/storage.py',
)


def build_source(root, output):
    root=Path(root).resolve()
    output=Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name in SOURCE_FILES:
            path=root/name
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise RuntimeError('Refusing a source link outside the package')
            archive.write(path,'uibe-lecture-mcp/'+name)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('Source archive verification failed')
        assert set(archive.namelist()) == {'uibe-lecture-mcp/'+n for n in SOURCE_FILES}
    return output


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source-only',action='store_true')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    version=tomllib.loads((root/'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    output=build_source(root, root/'dist'/f'uibe-lecture-mcp-v{version}.zip')
    print(output)
    if not args.source_only:
        print(build(output, root/'downloads'/'UIBE-Lecture-Setup.cmd'))


if __name__ == '__main__': main()
