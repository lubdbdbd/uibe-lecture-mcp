"""Local, explicit-consent onboarding. Never exposed as an MCP tool."""
from __future__ import annotations
import asyncio
import sys


def setup(*, ask=input, display=print):
    if not sys.stdin.isatty():
        raise RuntimeError('Setup requires a local interactive terminal; run python run.py setup')
    display('Sign in only through the official school CAS page. Challenges require your action.')
    display('Optional: save your school account/password in this Windows account Credential Manager for automatic recovery. Never send passwords in chat.')
    save = ask('Save credentials locally? [y/N]: ').strip().lower() in {'y', 'yes'}
    if save:
        from .credentials import configure_interactive
        configure_interactive()
    from .auth import login
    result = asyncio.run(login())
    if not result.get('ok'):
        raise RuntimeError('Login was not verified; setup did not enable background recovery')
    display('Login verified. Tasks stay on this PC; it must be powered on and connected.')
    enable = ask('Enable background recovery at Windows logon and every 5 minutes while logged in? [y/N]: ').strip().lower() in {'y', 'yes'}
    if enable:
        from .startup import enable_startup
        worker = enable_startup()
    else:
        from .service import start_worker
        from .storage import Store
        worker = start_worker(Store())
    return {'logged_in': True, 'credentials_opted_in': save, 'startup_opted_in': enable,
            'worker': worker}
