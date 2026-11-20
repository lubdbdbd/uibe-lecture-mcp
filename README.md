# 成长惠园自动讲座工具 · 跨宿主 MCP

根据指令抓取课程、筛选候选、持续发现新课程、到点自动报名、满员捡漏、核实结果。适配 `https://ea.uibe.edu.cn`，时间默认东八区。

## 安装

1. 解压本次提供的完整源码 ZIP，双击 install.cmd。需要 Python 3.11+ 和 Chrome。安装器会补齐依赖，将工具复制到 %LOCALAPPDATA%\UIBE-Lecture，并询问是否配置 Codex MCP（默认不配置）。
2. 安装器打开本机 setup：先询问是否将账号密码保存在 Windows 凭据管理器（默认不保存），再打开学校官方登录页；登录完成后另行询问是否启用后台恢复。会话使用当前 Windows 用户的 DPAPI 加密。
3. 在所选宿主添加下文 STDIO 配置，调用 `auth_status` 检查接入。

之后可以直接说：

“帮我自动抢下周的劳动实践，至少 0.2 学分，周二下午不要，最多成功两场；满员后等十分钟。”

已接入的宿主把条件交给后台执行器。无需手动打开报名页面、刷新或点击提交。

运行期间电脑需开机、联网、不休眠。关闭对话不影响后台任务；重启电脑后让已接入的宿主调用 `worker_start`。会话过期时工具保留任务，调用 `auth_login` 续接后自动继续。

## 功能与命令

所有命令输出 JSON；在工具目录运行。

| 功能 | MCP | 命令行 |
|---|---|---|
| 登录/检查 | `auth_login` / `auth_status` | `python run.py auth login` / `auth status` |
| 抓取与筛选 | `lectures_list` | `python run.py list --criteria filters.json` |
| 课程详情 | `lecture_details` | `python run.py show 课程ID` |
| 单场抢报 | `registration_schedule` | `python run.py reserve 课程ID --instruction "抢此课程"` |
| 预检演练 | 同上，`dry_run=true` | 同上追加 `--dry-run` |
| 持续筛选抢报 | `monitoring_create` | `python run.py monitor example-rule.json` |
| 已报名列表 | `registrations_list` | `python run.py reservations` |
| 任务/规则/日志 | `tasks_status` | `python run.py status` |
| 停止后续请求 | `task_stop` | `python run.py stop 任务或规则ID` |
| 核实结果 | `registration_verify` | `python run.py verify 任务ID` |
| 恢复后台 | `worker_start` | `python run.py start` |

筛选支持：关键词任一/全部匹配、排除词、平台、分类、组织方、日期范围、星期、每日时段、最低学分、最长活动时长和不可用时间段。`date_until` 为不包含的右端点；`weekdays` 周一为 1、周日为 7；关键词在标题、平台、类别、组织方中匹配。

`example-rule.json` 是示例，不会随安装自动启用。`mode=monitor` 只记录候选；`mode=enroll` 自动报名。`max_success` 限制该规则已成功及仍在进行的报名数量，结果待核实的任务保留名额。时间窗口为同一天内的起止时刻。

学校已报名课程的冲突检查读取详情中的真实活动时间。额外课表、Notion 日程等由已接入的宿主转成 `criteria.busy` 后传入。

## 0.2.0 抢报机制

- 开抢前60秒预热报名页和连接，最后5秒至开放后20秒快速检查。最后5秒起，以学校报名页实际开放为准，出现可提交表单就立即提交。
- 每场独立执行，最多8场同时处理；先修课程优先进入队列。刷新、提交、结果核实各自调度，慢响应不会阻塞其他课程。
- 提交优先于排队刷新。全账号快速刷新间隔至少250毫秒；就绪表单的单次提交可抢先使用请求通道，仍遵守学校限流和 Retry-After。
- 根据HTTPS Date的多次采样区间估计学校服务器时差，并显示估计误差。报名页实际状态决定是否提交。
- 表单令牌只保留在内存；会话更新后重新读取表单。单场只有一个在途提交；提交前用一次数据库事务复核取消指令并记录提交意图。
- 学校明确回复“未开始”时快速重试；满员后按 watch_minutes 捡漏，开放后20秒内继续快速检查，随后间隔2秒。
- 提交后只读确认“您已报名该课程”。超时或未知响应会自动核实最多2分钟，期间不重复提交。重启后继续核实已有提交。
- 网络中断不会累计8次就提前终止任务；恢复后继续执行，仍受任务截止时间约束。
- 有待执行任务时请求Windows保持唤醒，任务结束后释放；电脑关机和断网需要恢复。
- 用户明确不考虑时间冲突时传 criteria.ignore_conflicts=true；默认保留冲突检查。
- task_stop 会在实际发送前再次检查取消状态；已成功的学校报名保留。

### 离线性能验证

运行 `python scripts/benchmark_race.py`：模拟三场同时开放、每次响应80毫秒、一秒后名额耗尽；不会连接学校或使用真实登录。

本次同机对比：升级前成功1/3场，升级后成功3/3场。具体计时记录随发布包提供；真实执行计时可在任务的 submit_timing 和 server_reply 日志中查看。

## 文件与维护

`data/state.sqlite3` 保存任务、规则、课程缓存和结果；`data/session.bin` 保存登录会话；`data/worker.log` 记录后台进程错误。不要把 `data` 目录上传或放入代码仓库。

安装器只新增自己标记的 MCP 配置段，并保留其他服务。配置已有同名且非本安装器管理的条目时，需先核对。Python 路径变更后重新运行安装器。

检查：`python -m pytest -q`。测试使用模拟学校响应，不占用真实课程名额。

## 页面适配

适配课程列表、报名页面、数字 classid、SPAM令牌、POST地址、前端AJAX提交方式、已报名提示和活动时间。重复action属性按浏览器采用第一个值处理。

安装包只含程序、测试和说明，不包含账号、Cookie、个人任务或报名表令牌。

## 0.1.1 登录状态修复

- 专用 Chrome/Edge 资料目录保留学校统一认证状态；退出浏览器后从加密快照恢复会话 Cookie。
- 服务器更新 Cookie 后立即原子保存，MCP 与后台进程共享；旧请求不能覆盖新登录。
- 后台每十分钟只读访问一次学校页面以续接会话，MCP 重连时恢复后台进程。
- 会话失效时先尝试无窗口恢复；首次迁移或学校统一认证也过期时，在专用窗口登录一次。
- 登录工具返回 starting、window_open、saved 或 failed，只有浏览器页面确实创建后返回 window_open。
- 已排队报名任务和原 session.bin 保留。安装包不包含账号、Cookie 或个人任务数据。


## Optional Windows automatic SSO login

Opt in using `python run.py auth configure` in a local terminal. Both account and
password input are hidden. Credentials are stored only in the current Windows
user's Credential Manager, under `UIBE-Lecture/cas.uibe.edu.cn`. Remove this opt-in
with `python run.py auth forget`. Never put a password in command arguments,
source files, logs, `.env` files, or a repository.

An explicitly authorized automation can instead call `auth credentials-set --stdin`
with a private non-TTY pipe containing one JSON line with `username` and `password`.
The input must not pass through shell command text, shell history, or a file.
Only a success flag and storage label are returned.

Normal session reuse and periodic keepalive remain the first choice. If session
recovery reaches the verified school CAS page, the saved credentials may be filled
and the real login button clicked once. Credential entry and authentication POSTs
are restricted to HTTPS `cas.uibe.edu.cn/cas/login` with the exact HTTPS school
callback. Visible CAPTCHA, slider, OTP or other challenge controls stop automatic
entry. Failed or unconfirmed submissions block further attempts until a successful
manual login or explicit credential reconfiguration. No CAPTCHA is bypassed.

Keeping the computer running cannot override school-side session expiration or
mandatory verification. The browser's “remember username” control is not a
persistent-login guarantee. Password auto-login does not remove any school policy.


### 0.2.1 verification and packaging

Automatic login waits for the CAS page load and its real jQuery click handler,
with a bounded timeout, before filling or submitting. A visible input alone is
not treated as a ready login page. HTTPX SOCKS support is included for execution
environments using an authorized SOCKS proxy; no proxy bypass is performed.

Build source archives from tracked source only (`git archive`), never from the
installed application directory. Exclude `data/`, browser profiles, session files,
credential exports, logs, and local verification scripts. Credential Manager
values are not part of the package. `python run.py auth forget` deletes the saved
Windows login credential; it does not log the user out of existing sessions.


### 0.2.3 automatic login recovery

- Bound the course index's delayed login redirect, then navigate only to the
  pinned HTTPS CAS service when the trusted school index remains logged out.
- Scope controls to the verified login form. Accept a ready native submit or a
  bound legacy button. Check both school jQuery instances: the login handler
  can belong to `$` while `window.jQuery` points to a different version.
- Preserve CAPTCHA, exact destination/form checks, one-attempt guard, encrypted
  session storage and Windows Credential Manager. No registration replay.
- Regression checks: `python -m pytest -q`. Browser fixture tests require local
  Google Chrome and use only intercepted pages and dummy credentials.


## Share this source package with another Windows user

Distribute the newly built source ZIP or its freshly generated installer. Each
recipient installs and signs in to their own school account. Never copy an
existing installation's `data/`, browser profile, session snapshot or credentials.
The installer now opens `setup` in the recipient's local terminal:

1. Choose whether to save their own credentials in Windows Credential Manager
   (default No, hidden input, no password in chat or command arguments).
2. Complete the official school login. CAPTCHA/MFA stay with the user.
3. After verified login, separately opt into background recovery (default No).
   This registers a least-privilege task for the current Windows user, at logon
   and every five minutes while logged in, using `pythonw.exe` without a window.
4. The worker resumes previously authorized jobs independently of the chat.
   The PC must stay powered on and connected; no service runs while logged out.

Run `python run.py setup` to revisit local onboarding. Noninteractive setup is
rejected. Existing credentials are not erased if the user declines saving new
ones; use `python run.py auth forget` to remove them. `--no-login` skips onboarding.
`python run.py startup enable` is an explicit opt-in; `startup disable` removes
only the recovery trigger and preserves jobs and the running worker.

Worker starts are serialized. The OS singleton lock, not an old heartbeat,
prevents duplicate workers; a ten-second launch grace prevents startup storms.
A held lock with stale heartbeat is reported unhealthy and is never force-killed.

Packaging: `python scripts/build_release.py --source-only` uses an explicit
source allowlist. No `.git`, local state, logs, historical verification reports,
legacy patch scripts or stale installers enter the ZIP. Omit `--source-only`
to regenerate the standalone installer from that exact new ZIP. Do not ship the
historical `downloads/UIBE-Lecture-Setup.cmd` without rebuilding it.


With background opt-in, Task Scheduler launches the long-running worker directly
and is asked to start it immediately. This keeps it outside the terminal or agent
harness process tree. If an existing worker holds the singleton lock, the scheduled
attempt exits; a later five-minute attempt recovers after that worker exits.
Without opt-in, the detached CLI worker can still be terminated by a harness that
kills its full process tree. Task Scheduler registration/start failures are
reported; setup never claims that persistent recovery is enabled after an error.

### Background recovery trigger semantics (0.2.4)

After local opt-in, Task Scheduler starts the worker on demand immediately. An independent TimeTrigger starts about one minute after setup and repeats every five minutes indefinitely; a separate LogonTrigger starts it at subsequent logons. Omitted Duration/EndBoundary means indefinite repetition. ExecutionTimeLimit PT0S allows a long-running worker; IgnoreNew avoids concurrent task instances. The user must remain logged in and the computer runnable.

XML order follows [Microsoft Task Scheduler Schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema); omitted repeat duration follows [RepetitionPattern.Duration](https://learn.microsoft.com/en-us/windows/win32/taskschd/repetitionpattern-duration).

## 国产宿主快速接入（v0.2.4）

解压完整 ZIP，安装 Python 3.11+ 和 Chrome，再双击 install.cmd。安装器补齐依赖并复制到 %LOCALAPPDATA%\UIBE-Lecture；随后询问是否配置 Codex，默认 No。使用 WorkBuddy、DSH 或 QwenWork 时直接回车，然后完成本地 setup。命令行也可运行 install.cmd --no-codex；--codex 明确选择配置 Codex，--no-login 只跳过本地登录引导。

安装窗口会打印本机 Python、run.py、data 的完整路径和可复制的 mcpServers JSON。配置不含账号密码。重新查看解释器：python -c "import sys;print(sys.executable)"。先只调用 tasks_status 核验连接，再按自己的指令安排报名。

### 腾讯 WorkBuddy
在自定义连接器的配置 MCP 入口，把安装窗口打印的 JSON 合并进现有配置，保留其他服务器。结构如下，替换示例路径为安装窗口显示的实际值：
```json
{"mcpServers":{"uibe-lecture":{"command":"C:/Python/python.exe","args":["C:/UIBE-Lecture/run.py","mcp"],"env":{"UIBE_DATA_DIR":"C:/UIBE-Lecture/data"}}}}
```
[WorkBuddy 官方连接器文档](https://open.workbuddy.cn/en/docs/connector)说明 STDIO 的 command/args 配置。本包提供通用服务端，桌面宿主实际版本仍需连接验证。

### DeepSeek Harness（DSH）
使用官方桥接插件 @deepseek-ai/dsh-mcp-client。把以下内容保存为自己的 uibe.patch.yml，替换路径：
```yaml
- insert:
    - id: mcp-uibe-lecture
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: uibe-lecture
        transport: stdio
        command: 'C:/Python/python.exe'
        args: ['C:/UIBE-Lecture/run.py', mcp]
        env:
          UIBE_DATA_DIR: 'C:/UIBE-Lecture/data'
        startupTimeoutMs: 90000
        toolCallTimeoutMs: 180000
```
用 dsh web --patch <uibe.patch.yml的绝对路径> 启动。长期使用时合并到 $DSH_HOME/profiles/web/cordis.patch.yml，保留原有内容。先调用 mcp__uibe-lecture__tasks_status。
[官方 MCP 指南](https://deepseek-harness.github.io/deepseek-harness/en/guide/mcp-memory#bring-another-mcp-server)及[桥接插件说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)。官方桥接插件 0.2.2-rc.5 的 STDIO 工具发现和只读状态调用已用隔离数据验证。

### QwenWork 桌面版
在 QwenWork 桌面左侧打开 扩展 → 连接器 → + 添加 → 手动填写配置，选择 STDIO，服务器名称填 uibe-lecture。将下面整条启动命令粘贴到「命令」一栏，替换为安装窗口打印的两条实际路径，保留路径两侧的引号：
```text
"C:/Python/python.exe" "C:/UIBE-Lecture/run.py" mcp
```
另在环境变量填写 UIBE_DATA_DIR，值为安装窗口的 data 路径。添加后展开工具列表，并在新任务里先调用 tasks_status。
[千问办公桌面官方文档](https://docs.qwenwork.cn/features/connectors)。这里指提供 STDIO 入口的千问办公（QwenWork）桌面产品；“千问 App 工作助理”是否提供同样入口尚未核实。

本机凭据只通过 setup 的隐藏输入交互保存。不要把账号密码填入 MCP JSON、YAML、聊天或分享包。
