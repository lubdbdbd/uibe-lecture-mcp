import asyncio
import json
from pathlib import Path
import sys
import tomllib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
from install import update_config


def test_config_preserves_other_servers():
    old='model="existing"\n[mcp_servers.existing]\ncommand="keep"\n'
    result=update_config(old,Path(r"C:\Python\python.exe"),Path(r"C:\A Folder\UIBE"))
    parsed=tomllib.loads(result)
    assert parsed["mcp_servers"]["existing"]=={"command":"keep"}
    assert parsed["model"]=="existing"
    assert parsed["mcp_servers"]["uibe-lecture"]["args"][-1]=="mcp"
    assert update_config(result,Path(r"C:\Python\python.exe"),Path(r"C:\A Folder\UIBE"))==result


def test_mcp_schema_and_readonly_call(tmp_path,monkeypatch):
    monkeypatch.setenv("UIBE_DATA_DIR",str(tmp_path))
    from uibe_lecture.mcp_server import mcp
    async def run():
        tools=await mcp.list_tools()
        names={t.name for t in tools}
        assert len(names)==11
        assert {"monitoring_create","registration_schedule","auth_login","tasks_status"}<=names
        rule=next(x for x in tools if x.name=="monitoring_create")
        assert rule.inputSchema["properties"]["rule"]
        result=await mcp.call_tool("tasks_status",{})
        assert "worker" in str(result) and "jobs" in str(result)
    asyncio.run(run())


def test_mcp_protocol_handshake_and_tool_call(tmp_path,monkeypatch):
    monkeypatch.setenv("UIBE_DATA_DIR",str(tmp_path))
    from mcp.shared.memory import create_connected_server_and_client_session
    from uibe_lecture.mcp_server import mcp
    async def run():
        async with create_connected_server_and_client_session(mcp) as session:
            tools=await session.list_tools()
            assert len(tools.tools)==11
            result=await session.call_tool("tasks_status",{})
            assert not result.isError
            data=result.structuredContent or json.loads(next(c.text for c in result.content if c.type=="text"))
            assert data["jobs"]==[]
            assert data["worker"]["alive"] is False
    asyncio.run(run())


def test_windows_session_encryption(tmp_path,monkeypatch):
    import os
    import pytest
    if os.name!="nt":
        pytest.skip("Windows DPAPI")
    monkeypatch.setenv("UIBE_DATA_DIR",str(tmp_path))
    from uibe_lecture.auth import save_session,load_session,session_path
    sample={"cookies":[{"name":"TEST","value":"not-a-real-cookie","domain":"ea.uibe.edu.cn"}]}
    save_session(sample)
    loaded=load_session()
    assert loaded["cookies"]==sample["cookies"]
    assert loaded["saved_at"]>0
    assert b"not-a-real-cookie" not in session_path().read_bytes()


def test_codex_configuration_is_explicit_opt_in(monkeypatch):
    from install import choose_codex
    monkeypatch.setattr(sys.stdin,'isatty',lambda:True)
    assert not choose_codex(ask=lambda _: '')
    assert not choose_codex(ask=lambda _: 'n')
    assert choose_codex(ask=lambda _: 'yes')
    assert choose_codex(True,ask=lambda _:None)
    assert not choose_codex(False,ask=lambda _:None)
    monkeypatch.setattr(sys.stdin,'isatty',lambda:False)
    assert not choose_codex(ask=lambda _: (_ for _ in ()).throw(AssertionError()))


def test_host_neutral_connection_output(capsys):
    from install import connection_config,print_connection
    python=Path('C:/Fixture Python/python.exe');root=Path('C:/Fixture App')
    config=connection_config(python,root)['mcpServers']['uibe-lecture']
    assert config=={'command':str(python),'args':[str(root/'run.py'),'mcp'],'env':{'UIBE_DATA_DIR':str(root/'data')}}
    print_connection(python,root)
    output=capsys.readouterr().out
    assert 'Python:' in output and 'CLI:' in output and 'Data:' in output
    assert json.loads(output[output.index('{'):])['mcpServers']['uibe-lecture']==config


def test_installer_no_codex_keeps_existing_config(tmp_path,monkeypatch,capsys):
    import install
    import pytest
    if install.os.name!='nt': pytest.skip('Windows installer')
    home=tmp_path/'codex';home.mkdir()
    config=home/'config.toml';config.write_text('model="fixture"')
    monkeypatch.setenv('CODEX_HOME',str(home))
    monkeypatch.setattr(sys,'argv',['install.py','--configure-only','--no-login','--no-codex'])
    monkeypatch.setattr(install.importlib.util,'find_spec',lambda _:object())
    monkeypatch.setattr(install.subprocess,'run',lambda *a,**k: (_ for _ in ()).throw(AssertionError('unexpected subprocess')))
    install.main()
    assert config.read_text()=='model="fixture"'
    assert list(home.iterdir())==[config]
    assert 'mcpServers' in capsys.readouterr().out
