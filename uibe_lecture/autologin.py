from __future__ import annotations

"""Conservative opt-in SSO login. Challenges always require the owner."""
import time
from urllib.parse import urlsplit, urljoin, parse_qs
from .credentials import load_credentials, CredentialError
from .storage import Store

# Exact school origins and selectors must be verified against the actual login page.
LOGIN_HOSTS = frozenset({"cas.uibe.edu.cn"})
USER_SELECTOR = "#loginForm #un"
PASSWORD_SELECTOR = "#loginForm #pd"
SUBMIT_SELECTOR = "#loginForm #index_login_btn, #loginForm input.login_box_landing_btn[type=submit]"
CHALLENGE_SELECTOR = "#codeImage, #a_changeCode, [id*='captcha' i], [class*='captcha' i], [class*='slider' i], input[name*='code' i], input[name*='captcha' i], input[id*='captcha' i], input[name*='otp' i], input[autocomplete='one-time-code'], iframe[src*='captcha' i]"
COOLDOWN_SECONDS = 300


def allowed_login_url(url):
    p = urlsplit(url)
    try:
        return (not p.username and not p.password and p.scheme == "https"
                and p.hostname in LOGIN_HOSTS and p.port in (None, 443)
                and p.path == "/cas/login" and parse_qs(p.query).get("service") ==
                ["https://ea.uibe.edu.cn/User/Login/cas.html"])
    except ValueError:
        return False

async def try_saved_login(page, store=None, clock=time.time):
    """One attempt until successful login or explicit reconfiguration; no secret logs."""
    store = store or Store()
    if not allowed_login_url(page.url):
        return "not_login_page"
    # Support native submit controls and the older JS-only button. Native
    # forms need no jQuery handler; legacy buttons still require one.
    try:
        await page.wait_for_load_state("load", timeout=10000)
        await page.wait_for_function("""() => {
            const form = document.querySelector('#loginForm');
            if (!form) return false;
            const native = form.querySelector('input.login_box_landing_btn[type=submit]');
            if (native && native.form === form && !native.disabled) return true;
            const button = form.querySelector('#index_login_btn');
            if (!button || button.disabled) return false;
            // CAS loads multiple jQuery versions: $ binds login, while
            // window.jQuery can refer to the slideshow's older instance.
            return [window.$, window.jQuery].some(jq => {
                if (!jq || typeof jq._data !== 'function') return false;
                const events = jq._data(button, 'events');
                return !!(events && events.click && events.click.some(
                    item => typeof item.handler === 'function'));
            });
        }""", timeout=10000)
    except Exception:
        return "login_page_not_ready"
    if not allowed_login_url(page.url):
        return "unsafe_navigation"
    for element in await page.locator(CHALLENGE_SELECTOR).all():
        if await element.is_visible():
            return "challenge_required"
    user = page.locator(USER_SELECTOR)
    password = page.locator(PASSWORD_SELECTOR)
    submit = page.locator(SUBMIT_SELECTOR)
    if await user.count() != 1 or await password.count() != 1 or await submit.count() != 1:
        return "unsupported_login_page"
    if not await user.is_visible() or not await password.is_visible() or not await submit.is_visible():
        return "unsupported_login_page"
    action = await page.locator("#loginForm").get_attribute("action")
    if action is None or not allowed_login_url(urljoin(page.url, action)):
        return "unsafe_form"
    previous = store.get("system", "autologin") or {}
    if previous.get("state") in {"attempting", "submitted", "failed"}:
        return "manual_login_required"
    if clock() - previous.get("attempt_at", 0) < COOLDOWN_SECONDS:
        return "cooldown"
    try:
        credentials = load_credentials()
    except CredentialError:
        return "credential_store_unavailable"
    if not credentials:
        return "not_configured"
    # The caller holds browser.lock, so shared attempts are serialized.
    store.put("system", "autologin", {"attempt_at": clock(), "state": "attempting"})
    try:
        async def guard(route):
            request=route.request
            if request.method.upper() == "POST" and not allowed_login_url(request.url):
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", guard)
        if not allowed_login_url(page.url):
            return "unsafe_navigation"
        await user.fill(credentials[0])
        if not allowed_login_url(page.url):
            return "unsafe_navigation"
        await password.fill(credentials[1])
        if not allowed_login_url(page.url):
            return "unsafe_navigation"
        for element in await page.locator(CHALLENGE_SELECTOR).all():
            if await element.is_visible():
                return "challenge_required"
        action = await page.locator("#loginForm").get_attribute("action")
        if action is None or not allowed_login_url(urljoin(page.url, action)):
            return "unsafe_form"
        await submit.click()
        store.put("system", "autologin", {"attempt_at": clock(), "state": "submitted"})
        return "submitted"
    except Exception:
        store.put("system", "autologin", {"attempt_at": clock(), "state": "failed"})
        return "failed"
    finally:
        credentials = None
