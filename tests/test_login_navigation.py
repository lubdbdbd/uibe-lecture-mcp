import asyncio
from unittest.mock import AsyncMock
import pytest
from uibe_lecture.auth import prepare_login_page, CAS_LOGIN_URL, LOGOUT_SELECTOR

class Page:
    def __init__(self, url, logged_in=False):
        self.url = url
        self.goto = AsyncMock()
        self.wait_for_function = AsyncMock()
        self.logout = AsyncMock()
        self.logout.count.return_value = int(logged_in)
    def locator(self, selector):
        assert selector == LOGOUT_SELECTOR
        return self.logout


def test_delayed_redirect_still_on_index_uses_pinned_cas():
    p = Page('https://ea.uibe.edu.cn/User/Class/index.html')
    p.wait_for_function.side_effect = TimeoutError()
    asyncio.run(prepare_login_page(p))
    p.goto.assert_awaited_once_with(CAS_LOGIN_URL, wait_until='domcontentloaded', timeout=60000)


def test_existing_session_avoids_cas():
    p = Page('https://ea.uibe.edu.cn/User/Class/index.html', True)
    asyncio.run(prepare_login_page(p))
    p.goto.assert_not_awaited()


def test_delayed_redirect_to_login_is_followed():
    p = Page('https://ea.uibe.edu.cn/User/Class/index.html')
    async def redirect(*args, **kwargs):
        p.url = 'https://ea.uibe.edu.cn/User/Login/index.html'
    p.wait_for_function.side_effect = redirect
    asyncio.run(prepare_login_page(p))
    p.goto.assert_awaited_once()


def test_delayed_redirect_to_cas_is_not_reloaded():
    p = Page('https://ea.uibe.edu.cn/User/Class/index.html')
    async def redirect(*args, **kwargs): p.url = CAS_LOGIN_URL
    p.wait_for_function.side_effect = redirect
    asyncio.run(prepare_login_page(p))
    p.goto.assert_not_awaited()


@pytest.mark.parametrize('url', [
    'https://evil.test/User/Class/index.html',
    'https://ea.uibe.edu.cn.evil.test/User/Login/index.html',
    'https://ea.uibe.edu.cn:444/User/Login/index.html',
    'https://bad@ea.uibe.edu.cn/User/Login/index.html',
    'http://ea.uibe.edu.cn/User/Class/index.html',
    'https://ea.uibe.edu.cn/unexpected',
])
def test_untrusted_or_unexpected_page_unchanged(url):
    p = Page(url)
    asyncio.run(prepare_login_page(p))
    p.goto.assert_not_awaited()


def test_http_cas_is_upgraded_before_credentials():
    p = Page('http://cas.uibe.edu.cn/cas/login')
    asyncio.run(prepare_login_page(p))
    p.goto.assert_awaited_once_with(CAS_LOGIN_URL, wait_until='domcontentloaded', timeout=60000)
