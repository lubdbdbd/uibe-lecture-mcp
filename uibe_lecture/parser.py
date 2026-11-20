from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
from .models import Lecture, Busy, ORIGIN, dt

DATE = re.compile(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?")


class SiteError(RuntimeError):
    code = "SITE_CHANGED"


class SubmissionCancelled(SiteError):
    code = "SUBMISSION_CANCELLED"


class FormExpired(SiteError):
    code = "FORM_EXPIRED"


class AuthRequired(SiteError):
    code = "AUTH_REQUIRED"


class RateLimited(SiteError):
    code = "RATE_LIMITED"

    def __init__(self, seconds=60):
        self.seconds = max(1, float(seconds))
        super().__init__(f"学校要求等待 {self.seconds:g} 秒")


def soup_of(html: str):
    # Browsers keep the first duplicate attribute. The live form has a second,
    # empty action=""; html.parser's default replacement would erase its URL.
    soup = BeautifulSoup(html, "html.parser", on_duplicate_attribute="ignore")
    text = soup.get_text(" ", strip=True)
    login_hint = re.search(r"请先登录|登录已过期|请重新登录|(?:操作提示\s*)?请登录", text)
    login_redirect = soup.select_one('meta[http-equiv="refresh"][content*="Login"], a[href*="/User/Login/index"]')
    if soup.select_one('input[type="password"]') or login_hint or login_redirect:
        raise AuthRequired("请运行 auth login 续接学校登录")
    return soup


def site_url(path: str, base=ORIGIN) -> str:
    result = urljoin(base, path)
    u = urlsplit(result)
    if u.scheme != "https" or u.netloc != "ea.uibe.edu.cn" or u.username or u.password:
        raise SiteError("学校响应包含非本站地址")
    return result


def pagination(soup, url):
    out = set()
    for a in soup.select('.pagination a[href], .pages a[href], .page a[href], a[rel="next"]'):
        href = a.get("href", "")
        if not href or href.startswith(("#", "javascript:")):
            continue
        target = site_url(href, url)
        # Pagination can only visit the same controller's index route.
        controller = "/User/Mine/" if "/User/Mine/" in url else "/User/Class/"
        if not urlsplit(target).path.startswith(controller + "index"):
            raise SiteError("分页地址结构变化")
        out.add(target)
    return sorted(out)


def parse_lectures(html: str, url: str):
    soup = soup_of(html)
    table = next((t for t in soup.select("table") if "报名时间" in t.get_text() and "活动时间" in t.get_text()), None)
    if table is None:
        raise SiteError("未找到课程列表表格")
    found = []
    for row in table.select("tr"):
        a = row.select_one('a[href*="/User/Class/viewclass/classid/"]')
        if not a:
            continue
        cells = row.find_all("td", recursive=False)
        if len(cells) < 7:
            raise SiteError("课程表列数变化")
        m = re.search(r"/classid/([A-Za-z0-9_-]+)\.html", a["href"])
        times = [DATE.findall(cells[i].get_text(" ", strip=True)) for i in (1, 2)]
        if not m or any(len(x) != 2 for x in times):
            raise SiteError("课程 ID 或时间字段缺失")
        platform = cells[0].select_one(".text-muted")
        parts = re.sub(r"^所属平台[：:]\s*", "", platform.get_text(strip=True) if platform else "").split("/", 1)
        apply = cells[-1].select_one('a[path*="/User/Mine/apply/classid/"]')
        cid = m[1]
        found.append(Lecture(id=cid, title=a.get_text(" ", strip=True), start=dt(times[0][0]), end=dt(times[0][1]),
            opens=dt(times[1][0]), closes=dt(times[1][1]), credits=float(cells[3].get_text(strip=True)),
            platform=parts[0].strip(), category=parts[1].strip() if len(parts)>1 else "", organizer=cells[4].get_text(strip=True),
            status=cells[5].get_text(strip=True), detail_url=site_url(a["href"]),
            apply_url=site_url(apply["path"]) if apply else site_url(f"/User/Mine/apply/classid/{cid}.html")))
    return found, pagination(soup, url)


@dataclass
class Apply:
    state: str
    message: str
    remaining: int | None = None
    action: str | None = None
    fields: dict[str, str] = field(default_factory=dict, repr=False)
    course_id: str | None = None
    session_version: int | None = field(default=None, repr=False)
    received_monotonic: float | None = field(default=None, repr=False)


def parse_apply(html: str, expected_id: str, *, soup=None) -> Apply:
    soup = soup if soup is not None else soup_of(html)
    form = soup.select_one("form#form1")
    if form is None:
        raise SiteError("报名表单缺失")
    text = form.get_text(" ", strip=True)
    quota = re.search(r"剩余名额[：:]\s*(\d+)", text)
    remaining = int(quota[1]) if quota else None
    if "您已报名该课程" in text:
        return Apply("enrolled", "您已报名该课程", remaining)
    if "报名未开始" in text:
        return Apply("not_open", "报名未开始", remaining)
    if re.search(r"报名已结束|报名截止|已停止报名", text):
        return Apply("closed", text[:300], remaining)
    if re.search(r"不符合|不能报名|无法报名|不允许|仅限|限制报名", text):
        return Apply("ineligible", text[:300], remaining)
    if re.search(r"验证码|人机验证", text) or form.select_one('input[name*="captcha"], input[name*="verify"]'):
        return Apply("interaction_required", "页面需要完成验证", remaining)
    button = form.select_one('#submit1[lay-submit], button[lay-submit]')
    if not button or "符合报名条件" not in text:
        return Apply("unknown", text[:300], remaining)
    fields = {}
    for element in form.select("input[name]"):
        if element.has_attr("disabled") or element.get("type") in ("submit", "button", "file"):
            continue
        if element.get("type") in ("checkbox", "radio") and not element.has_attr("checked"):
            continue
        fields[element["name"]] = element.get("value", "")
    if not fields.get("classid"):
        raise SiteError("表单 classid 缺失")
    if "SPAM" not in fields:
        raise SiteError("表单 SPAM 字段缺失")
    action = site_url(form.get("action", ""))
    if action != ORIGIN + "/User/Mine/apply.html" or form.get("method", "").upper() != "POST":
        raise SiteError("报名表单提交地址或方法变化")
    return Apply("full" if remaining == 0 else "ready", "符合报名条件", remaining, action, fields, expected_id)


def parse_reservations(html: str, url: str):
    soup = soup_of(html)
    table = next((t for t in soup.select("table") if "报名时间" in t.get_text() and "课程名称" in t.get_text()), None)
    if table is None:
        raise SiteError("未找到我的课程表格")
    out = []
    for row in table.select("tr"):
        a = row.select_one('a[href*="/User/Mine/bookdetail/id/"]')
        if not a:
            continue
        cells = row.find_all("td", recursive=False)
        out.append({"title":a.get_text(strip=True), "detail_url":site_url(a["href"]),
                    "registered_at":cells[5].get_text(strip=True) if len(cells)>5 else None})
    return out, pagination(soup, url)


def detail_period(html: str) -> Busy:
    soup = soup_of(html)
    heading = next((h for h in soup.select("h5") if h.get_text(strip=True) == "活动时间"), None)
    if heading:
        # Read the nearest container with exactly one interval, not the table's mislabeled column.
        for ancestor in (heading.parent, heading.parent.parent):
            dates = DATE.findall(ancestor.get_text(" ", strip=True))
            if len(dates) == 2:
                return Busy(start=dt(dates[0]), end=dt(dates[1]))
    raise SiteError("报名详情缺少可确认的活动起止时间")
