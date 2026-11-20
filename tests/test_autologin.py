import asyncio
from unittest.mock import AsyncMock
import pytest
from uibe_lecture import autologin as a
from uibe_lecture import credentials as c

class Store:
    def __init__(self): self.value={}
    def get(self,*args): return self.value
    def put(self,*args): self.value=args[-1]

class Locator:
    def __init__(self): self.fill=AsyncMock(); self.click=AsyncMock()
    async def get_attribute(self,name): return "/cas/login?service=https%3A%2F%2Fea.uibe.edu.cn%2FUser%2FLogin%2Fcas.html"
    async def count(self): return 1
    async def is_visible(self): return True
    async def all(self): return []

class Page:
    url='https://cas.uibe.edu.cn/cas/login?service=https%3A%2F%2Fea.uibe.edu.cn%2FUser%2FLogin%2Fcas.html'
    def __init__(self): self.items={}; self.route=AsyncMock(); self.wait_for_load_state=AsyncMock(); self.wait_for_function=AsyncMock()
    def locator(self,s): return self.items.setdefault(s, Locator())

def test_origin_pin():
    for url in ['http://cas.uibe.edu.cn/cas/login','https://cas.uibe.edu.cn.evil.test/cas/login','https://ea.uibe.edu.cn/cas/login','https://cas.uibe.edu.cn:444/cas/login','https://cas.uibe.edu.cn/other']:
        assert not a.allowed_login_url(url)
    assert a.allowed_login_url(Page.url)
    assert not a.allowed_login_url('https://cas.uibe.edu.cn/cas/login?service=x')
    assert not a.allowed_login_url(Page.url.replace('https://','https://bad@'))

def test_no_configuration(monkeypatch):
    monkeypatch.setattr(a,'load_credentials',lambda:None)
    assert asyncio.run(a.try_saved_login(Page(),Store()))=='not_configured'

def test_one_submission_then_manual(monkeypatch):
    monkeypatch.setattr(a,'load_credentials',lambda:('test-account','test-password'))
    p=Page();s=Store()
    assert asyncio.run(a.try_saved_login(p,s))=='submitted'
    p.items[a.SUBMIT_SELECTOR].click.assert_awaited_once()
    assert asyncio.run(a.try_saved_login(p,s,lambda:99999999999))=='manual_login_required'
    assert 'password' not in repr(s.value)

def test_challenge_blocks_before_credentials(monkeypatch):
    p=Page(); challenge=Locator(); p.locator(a.CHALLENGE_SELECTOR).all=AsyncMock(return_value=[challenge])
    def forbidden(): raise AssertionError('must not load secrets')
    monkeypatch.setattr(a,'load_credentials',forbidden)
    assert asyncio.run(a.try_saved_login(p,Store()))=='challenge_required'

def test_fill_failure_is_sanitized(monkeypatch):
    monkeypatch.setattr(a,'load_credentials',lambda:('test','secret-marker'))
    p=Page();p.locator(a.PASSWORD_SELECTOR).fill.side_effect=ValueError('secret-marker')
    s=Store()
    assert asyncio.run(a.try_saved_login(p,s))=='failed'
    assert 'secret-marker' not in repr(s.value)

def test_unsafe_page_never_loads_credentials(monkeypatch):
    p=Page();p.url='https://evil.test/cas/login'
    monkeypatch.setattr(a,'load_credentials',lambda:pytest.fail('secret read'))
    assert asyncio.run(a.try_saved_login(p,Store()))=='not_login_page'

def test_input_validation():
    with pytest.raises(c.CredentialError): c.save_credentials('', 'test')
    with pytest.raises(c.CredentialError): c.save_credentials('test', '')



def test_foreign_form_blocks_secret_read(monkeypatch):
    p=Page();p.locator("#loginForm").get_attribute=AsyncMock(return_value="https://evil.test/cas/login")
    monkeypatch.setattr(a,'load_credentials',lambda:pytest.fail('secret read'))
    assert asyncio.run(a.try_saved_login(p,Store()))=='unsafe_form'

def test_stdin_invalid_input_sanitized(monkeypatch):
    import io,sys
    monkeypatch.setattr(sys,'stdin',io.StringIO('not-json-secret-marker'))
    with pytest.raises(c.CredentialError,match='Invalid credential input') as exc:
        c.configure_stdin()
    assert 'secret-marker' not in str(exc.value)


def test_hidden_challenge_does_not_block(monkeypatch):
    p=Page();ch=Locator();ch.is_visible=AsyncMock(return_value=False)
    p.locator(a.CHALLENGE_SELECTOR).all=AsyncMock(return_value=[ch])
    monkeypatch.setattr(a,'load_credentials',lambda:None)
    assert asyncio.run(a.try_saved_login(p,Store()))=='not_configured'

def test_route_rejects_wrong_post(monkeypatch):
    monkeypatch.setattr(a,'load_credentials',lambda:('test','dummy'))
    p=Page();assert asyncio.run(a.try_saved_login(p,Store()))=='submitted'
    guard=p.route.call_args.args[1]
    from types import SimpleNamespace
    route=SimpleNamespace(request=SimpleNamespace(method='POST',url='http://cas.uibe.edu.cn/cas/login'),abort=AsyncMock(),continue_=AsyncMock())
    asyncio.run(guard(route));route.abort.assert_awaited_once()


def test_dom_visible_but_handler_not_ready_never_loads_secrets(monkeypatch):
    p=Page();p.wait_for_function.side_effect=TimeoutError('initialization timeout')
    monkeypatch.setattr(a,'load_credentials',lambda:pytest.fail('secret read'))
    assert asyncio.run(a.try_saved_login(p,Store()))=='login_page_not_ready'
    p.wait_for_load_state.assert_awaited_once_with('load',timeout=10000)
    assert p.wait_for_function.call_args.kwargs['timeout']==10000

def test_load_timeout_does_not_try_credentials(monkeypatch):
    p=Page();p.wait_for_load_state.side_effect=TimeoutError()
    monkeypatch.setattr(a,'load_credentials',lambda:pytest.fail('secret read'))
    assert asyncio.run(a.try_saved_login(p,Store()))=='login_page_not_ready'
    p.wait_for_function.assert_not_awaited()

def test_initialization_navigation_rechecked(monkeypatch):
    p=Page()
    async def navigate(*args,**kwargs): p.url='https://evil.test/'
    p.wait_for_function.side_effect=navigate
    monkeypatch.setattr(a,'load_credentials',lambda:pytest.fail('secret read'))
    assert asyncio.run(a.try_saved_login(p,Store()))=='unsafe_navigation'
