"""Explicit opt-in, current-user Windows Task Scheduler watchdog. No passwords."""
from __future__ import annotations
from datetime import datetime, timedelta
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from xml.etree import ElementTree as ET


def task_name(root):
    digest = hashlib.sha256(str(Path(root).resolve()).casefold().encode()).hexdigest()[:12]
    return 'UIBE-Lecture-Recovery-' + digest


def task_xml(root, python, user, data=None, *, now=None):
    root = Path(root).resolve()
    python = Path(python).resolve()
    hidden = python.with_name('pythonw.exe')
    if not hidden.is_file():
        raise RuntimeError('pythonw.exe is required for hidden background recovery')
    ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('', ns)
    def add(parent, tag, text=None, **attrs):
        el=ET.SubElement(parent, '{'+ns+'}'+tag, attrs)
        el.text=text
        return el
    t=ET.Element('{'+ns+'}Task', {'version':'1.3'})
    triggers=add(t,'Triggers');trigger=add(triggers,'LogonTrigger')
    add(trigger,'Enabled','true');add(trigger,'ExecutionTimeLimit','PT0S');add(trigger,'UserId',user)
    # /Run is on-demand only. Give recovery its own first-install time trigger.
    # triggerBaseType requires Enabled, StartBoundary, Repetition in this order.
    timer=add(triggers,'TimeTrigger');add(timer,'Enabled','true')
    boundary=((now or datetime.now().astimezone())+timedelta(minutes=1)).isoformat(timespec='seconds')
    add(timer,'StartBoundary',boundary)
    repeat=add(timer,'Repetition');add(repeat,'Interval','PT5M')
    # Omitted Duration means repeat indefinitely (not PT0S).
    add(repeat,'StopAtDurationEnd','false')
    add(timer,'ExecutionTimeLimit','PT0S')
    principals=add(t,'Principals');principal=add(principals,'Principal',id='Author')
    add(principal,'UserId',user);add(principal,'LogonType','InteractiveToken');add(principal,'RunLevel','LeastPrivilege')
    settings=add(t,'Settings');add(settings,'MultipleInstancesPolicy','IgnoreNew')
    add(settings,'DisallowStartIfOnBatteries','false');add(settings,'StopIfGoingOnBatteries','false')
    add(settings,'StartWhenAvailable','true');add(settings,'ExecutionTimeLimit','PT0S')
    add(settings,'Enabled','true');add(settings,'Hidden','true')
    actions=add(t,'Actions',Context='Author');execute=add(actions,'Exec')
    add(execute,'Command',str(hidden))
    add(execute,'Arguments',subprocess.list2cmdline(['-m','uibe_lecture','worker','--data-dir',str(Path(data).resolve() if data else root/'data')]))
    add(execute,'WorkingDirectory',str(root))
    return ET.tostring(t,encoding='utf-16',xml_declaration=True)


def enable_startup():
    if os.name != 'nt': raise RuntimeError('Windows Task Scheduler is required')
    from .service import ROOT
    # The account is resolved by Windows, never accepted from website content.
    user=subprocess.run(['whoami'],capture_output=True,text=True,check=True).stdout.strip()
    if not user: raise RuntimeError('Unable to identify current Windows account')
    from .storage import data_dir
    payload=task_xml(ROOT,sys.executable,user,data_dir())
    with tempfile.TemporaryDirectory(prefix='uibe-startup-') as tmp:
        path=Path(tmp)/'task.xml';path.write_bytes(payload)
        result=subprocess.run(['schtasks','/Create','/TN',task_name(ROOT),'/XML',str(path),'/F'],capture_output=True)
    if result.returncode: raise RuntimeError('Task Scheduler registration failed; no credentials were included')
    started=subprocess.run(['schtasks','/Run','/TN',task_name(ROOT)],capture_output=True)
    if started.returncode:
        raise RuntimeError('Recovery task registered, but immediate start failed; retry startup enable')
    return {'enabled':True,'task':task_name(ROOT),'start_requested':True,'launcher':'Windows Task Scheduler'}


def disable_startup():
    if os.name != 'nt': raise RuntimeError('Windows Task Scheduler is required')
    from .service import ROOT
    result=subprocess.run(['schtasks','/Delete','/TN',task_name(ROOT),'/F'],capture_output=True)
    if result.returncode: raise RuntimeError('Task Scheduler removal failed or task is absent')
    return {'enabled':False,'tasks_preserved':True,'running_worker_unchanged':True}
