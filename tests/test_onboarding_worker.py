from unittest.mock import Mock,AsyncMock
import pytest
from uibe_lecture import onboarding, auth, credentials, startup, service
from uibe_lecture.storage import Store


def prepare(monkeypatch,tmp_path):
    monkeypatch.setenv('UIBE_DATA_DIR',str(tmp_path))
    monkeypatch.setattr(onboarding.sys.stdin,'isatty',lambda:True)
    login=AsyncMock(return_value={'ok':True});save=Mock();enable=Mock();worker=Mock(return_value={'started':True})
    monkeypatch.setattr(auth,'login',login)
    monkeypatch.setattr(credentials,'configure_interactive',save)
    monkeypatch.setattr(startup,'enable_startup',enable)
    monkeypatch.setattr(service,'start_worker',worker)
    return login,save,enable,worker


def test_defaults_save_nothing_and_no_startup(monkeypatch,tmp_path):
    login,save,enable,worker=prepare(monkeypatch,tmp_path)
    r=onboarding.setup(ask=lambda _: '',display=lambda _:None)
    assert r['logged_in'] and not r['credentials_opted_in'] and not r['startup_opted_in']
    save.assert_not_called();enable.assert_not_called();login.assert_awaited_once();worker.assert_called_once()


def test_separate_explicit_local_consent(monkeypatch,tmp_path):
    login,save,enable,worker=prepare(monkeypatch,tmp_path)
    answers=iter(['yes','y'])
    r=onboarding.setup(ask=lambda _:next(answers),display=lambda _:None)
    assert r['credentials_opted_in'] and r['startup_opted_in']
    save.assert_called_once();enable.assert_called_once()


def test_noninteractive_setup_does_not_touch_credentials(monkeypatch,tmp_path):
    login,save,enable,worker=prepare(monkeypatch,tmp_path)
    monkeypatch.setattr(onboarding.sys.stdin,'isatty',lambda:False)
    with pytest.raises(RuntimeError,match='interactive terminal'): onboarding.setup()
    save.assert_not_called();login.assert_not_awaited();enable.assert_not_called();worker.assert_not_called()


def test_failed_login_never_enables_background(monkeypatch,tmp_path):
    login,save,enable,worker=prepare(monkeypatch,tmp_path);login.side_effect=RuntimeError('login failed')
    with pytest.raises(RuntimeError):onboarding.setup(ask=lambda _:'n',display=lambda _:None)
    enable.assert_not_called();worker.assert_not_called()


def test_locked_worker_is_not_spawned_even_with_stale_heartbeat(monkeypatch,tmp_path):
    s=Store(tmp_path/'state.sqlite3');s.put('system','worker',{'pid':123,'heartbeat':0})
    monkeypatch.setattr(service,'worker_lock_held',lambda _:True)
    popen=Mock();monkeypatch.setattr(service.subprocess,'Popen',popen)
    r=service.start_worker(s)
    assert r['running'] and not r['healthy'] and not r['started'];popen.assert_not_called()


def test_crashed_worker_with_fresh_heartbeat_restarts_once(monkeypatch,tmp_path):
    import time
    s=Store(tmp_path/'state.sqlite3');s.put('system','worker',{'pid':123,'heartbeat':time.time()})
    monkeypatch.setattr(service,'worker_lock_held',lambda _:False)
    popen=Mock(return_value=Mock(pid=456));monkeypatch.setattr(service.subprocess,'Popen',popen)
    assert service.start_worker(s)=={'started':True,'pid':456}
    assert service.start_worker(s)['starting']
    popen.assert_called_once()
    assert popen.call_args.kwargs['env']['UIBE_DATA_DIR']==str(tmp_path)


def test_expired_launch_marker_can_recover(monkeypatch,tmp_path):
    s=Store(tmp_path/'state.sqlite3');s.put('system','worker_launch',{'pid':123,'at':0})
    monkeypatch.setattr(service,'worker_lock_held',lambda _:False)
    monkeypatch.setattr(service.subprocess,'Popen',Mock(return_value=Mock(pid=456)))
    assert service.start_worker(s)['started']


def test_watchdog_xml_is_user_scoped_and_escaped(tmp_path):
    from xml.etree import ElementTree as ET
    exe=tmp_path/'Python & tools'/'python.exe';exe.parent.mkdir();exe.touch();exe.with_name('pythonw.exe').touch()
    root=tmp_path/'app & spaces';root.mkdir()
    xml=startup.task_xml(root,exe,'DOMAIN\\fixture-user')
    tree=ET.fromstring(xml);ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    assert tree.find('.//t:LogonType',ns).text=='InteractiveToken'
    assert tree.find('.//t:RunLevel',ns).text=='LeastPrivilege'
    assert tree.find('.//t:Interval',ns).text=='PT5M'
    assert '--data-dir' in tree.find('.//t:Arguments',ns).text
    assert tree.find('.//t:WorkingDirectory',ns).text==str(root.resolve())
    assert 'Password' not in xml.decode('utf-16')


def test_hidden_interpreter_required(tmp_path):
    with pytest.raises(RuntimeError,match='pythonw'):startup.task_xml(tmp_path,tmp_path/'python.exe','fixture')

@pytest.mark.skipif(startup.os.name != 'nt', reason='Windows scheduler adapter')
@pytest.mark.parametrize('failure',[None,'create','run'])
def test_scheduler_calls_are_mocked_and_failure_is_visible(monkeypatch,tmp_path,failure):
    exe=tmp_path/'python.exe';exe.touch();exe.with_name('pythonw.exe').touch()
    monkeypatch.setattr(startup.sys,'executable',str(exe));monkeypatch.setattr(service,'ROOT',tmp_path)
    monkeypatch.setenv('UIBE_DATA_DIR',str(tmp_path/'isolated-data'))
    calls=[]
    def fake_run(args,**kwargs):
        calls.append(args)
        if args[0]=='whoami':return Mock(stdout='DOMAIN\\fixture',returncode=0)
        if '/Create' in args:
            from pathlib import Path
            from xml.etree import ElementTree as ET
            content=Path(args[args.index('/XML')+1]).read_bytes()
            ET.fromstring(content)
            assert 'isolated-data' in content.decode('utf-16')
            assert '/F' in args
            return Mock(returncode=int(failure=='create'))
        return Mock(returncode=int(failure=='run'))
    monkeypatch.setattr(startup.subprocess,'run',fake_run)
    if failure:
        with pytest.raises(RuntimeError,match='failed'):startup.enable_startup()
    else:
        assert startup.enable_startup()['start_requested']
    assert any('/Run' in c for c in calls) == (failure!='create')


def test_package_allowlist_excludes_runtime_and_stale_artifacts(tmp_path):
    import importlib.util
    import sys
    import zipfile
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root/'scripts'))
    try:
        from build_release import build_source,SOURCE_FILES
        forbidden={'data','downloads','.git','__pycache__'}
        assert not any(forbidden & set(Path(n).parts) for n in SOURCE_FILES)
        assert not any(n in SOURCE_FILES for n in ['verification.json','apply-auth-fix.ps1','Update-Login.cmd'])
        out=build_source(root,tmp_path/'source.zip')
        with zipfile.ZipFile(out) as z:
            assert 'uibe-lecture-mcp/uibe_lecture/onboarding.py' in z.namelist()
            assert 'uibe-lecture-mcp/uibe_lecture/__main__.py' in z.namelist()
    finally:sys.path.pop(0)


def test_mcp_startup_failure_never_writes_stdout(tmp_path):
    import os,subprocess,sys
    from pathlib import Path
    blocked=tmp_path/'not-a-directory';blocked.write_text('fixture')
    env=dict(os.environ,UIBE_DATA_DIR=str(blocked))
    r=subprocess.run([sys.executable,'-m','uibe_lecture','mcp'],env=env,cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=20)
    assert r.returncode!=0 and r.stdout==b'' and b'FileExistsError' in r.stderr


def test_scheduler_trigger_order_and_first_install_recovery(tmp_path):
    from datetime import datetime, timezone, timedelta
    from xml.etree import ElementTree as ET
    exe=tmp_path/'python.exe';exe.touch();exe.with_name('pythonw.exe').touch()
    now=datetime(2026,11,20,14,59,30,tzinfo=timezone(timedelta(hours=8)))
    tree=ET.fromstring(startup.task_xml(tmp_path,exe,'DOMAIN\\fixture',now=now))
    ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    tags=lambda node:[x.tag.rsplit('}',1)[-1] for x in node]
    assert tree.attrib['version']=='1.3'
    triggers=tree.find('t:Triggers',ns)
    assert tags(triggers)==['LogonTrigger','TimeTrigger']
    logon,timer=list(triggers)
    assert tags(logon)==['Enabled','ExecutionTimeLimit','UserId']
    assert tags(timer)==['Enabled','StartBoundary','Repetition','ExecutionTimeLimit']
    assert timer.find('t:StartBoundary',ns).text=='2026-11-20T15:00:30+08:00'
    repeat=timer.find('t:Repetition',ns)
    assert tags(repeat)==['Interval','StopAtDurationEnd']
    assert repeat.find('t:Interval',ns).text=='PT5M'
    assert repeat.find('t:Duration',ns) is None
    assert repeat.find('t:StopAtDurationEnd',ns).text=='false'
    assert timer.find('t:EndBoundary',ns) is None
    assert tree.find('t:Settings/t:ExecutionTimeLimit',ns).text=='PT0S'
    assert tree.find('t:Settings/t:MultipleInstancesPolicy',ns).text=='IgnoreNew'

    assert logon.find('t:ExecutionTimeLimit',ns).text=='PT0S'
    assert timer.find('t:ExecutionTimeLimit',ns).text=='PT0S'
