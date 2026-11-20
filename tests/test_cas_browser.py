"""Browser-backed fixtures; no school requests or real credentials."""
import asyncio
import pytest
from uibe_lecture import autologin as a
from uibe_lecture.auth import CAS_LOGIN_URL

class Store:
    def __init__(self): self.value = {}
    def get(self, *args): return self.value
    def put(self, *args): self.value = args[-1]

@pytest.mark.parametrize('legacy', [False, True, 'dual_jquery'])
def test_supported_cas_forms_submit_once_without_network(monkeypatch, legacy):
    monkeypatch.setattr(a, 'load_credentials', lambda: ('fixture-user', 'fixture-password'))
    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='chrome', headless=True)
            try:
                page = await browser.new_page()
                button = '<span id="index_login_btn"><input class="login_box_landing_btn" type="button"></span>' if legacy else '<input class="login_box_landing_btn" type="submit" value="Login">'
                script = '''window.submissions=0;
                const form=document.querySelector('#loginForm');
                form.addEventListener('submit', e=>{e.preventDefault();window.submissions++});'''
                if legacy:
                    script += '''const b=document.querySelector('#index_login_btn');
                    const handler=()=>form.requestSubmit();b.addEventListener('click',handler);
                    window.jQuery={_data:()=>({click:[{handler}]})};'''
                    if legacy == 'dual_jquery':
                        script += 'window.$=window.jQuery;window.jQuery={_data:()=>undefined};'
                html = '<form id="loginForm" action="'+CAS_LOGIN_URL+'" method="post"><input id="un"><input id="pd" type="password">'+button+'</form><script>'+script+'</script>'
                await page.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
                await page.goto(CAS_LOGIN_URL)
                store = Store()
                assert await a.try_saved_login(page, store) == 'submitted'
                assert await page.evaluate('window.submissions') == 1
                assert await page.locator('#un').input_value() == 'fixture-user'
                assert await a.try_saved_login(page, store) == 'manual_login_required'
                assert await page.evaluate('window.submissions') == 1
            finally:
                await browser.close()
    asyncio.run(run())
