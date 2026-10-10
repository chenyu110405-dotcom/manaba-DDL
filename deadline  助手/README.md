# manaba 作业截止助手：抓取第一版

已实现龙谷大学 manaba 的只读抓取、日文正文提取、默认每 600 秒同步、手动同步，以及后续 Windows 小窗可调用的 Python/本地 JSON 接口。本机面板采用按科目分组的彩色便签，直接显示最近截止倒计时和各项作业状态。Windows 独立可执行文件留待后续阶段。

仅支持 `https://manaba.ryukoku.ac.jp/ct/home`，可以粘贴该校首页或未提交列表 URL。其他学校、Teams、aiJudge 等需要单独适配。网址中不接受账号、密码、令牌或其他查询参数。

## 启动

Windows 11：安装 Python 3.10 或更新版本后，首次双击 `setup_windows.cmd`，之后双击 `run_windows.cmd`。安装使用 PyPI 和 Playwright 官方浏览器下载源。

也可在项目目录手动执行：

```powershell
py -3 -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python -m manaba_deadline serve
```

面板地址为 `http://127.0.0.1:8765/`。点击「登录 manaba」，在独立 Chromium 窗口正常登录本人校园账号。登录成功后立即同步；之后每 10 分钟同步。面板「刷新作业」可以提前刷新；重复请求合并，抓取不会重叠。截止倒计时在显示端每秒更新，页面每 5 秒查询本地内存状态，不会每秒访问校园平台。

「更换账号／网页」清空旧作业和旧登录会话，随后用新账号登录。登录失效会暂停自动读取、显示清晰提示，并将已有数据标为旧数据。重新登录后继续。密码不会交给程序配置或聊天；应用不导入 Codex/Chrome 的已有登录状态。

每门课程使用独立颜色、统一高度 256px 的摘要便签；没有作业的课程也保留同样尺寸。首页显示最近待交课题、倒计时、截止日期和作业数量；标题最多占两行。点击「看看作业」在独立详情窗口查看完整标题、全部作业、正文及来源链接，已提交记录可按需展开，详情不会撑高便签。同步刷新保留当前课程及正文展开状态。24 小时内截止和已逾期使用强调色与文字提示，迟交截止会在原截止到达后接替倒计时。

页面上方的「从这里开始」使用统一高度 232px 的便签，集中显示已确认未提交、截止时间尚未到达且平台尚未关闭的课题，按截止先后排列。有效迟交期间的课题保留并标明「可迟交」，尚未开放的课题标明状态。科目及科目内课题也优先排列这些记录；其他记录继续保留在课程详情中。倒计时到达截止时自动退出优先区。优先区与科目区沿用相同列宽，只有一张时也不铺满整行，窗口较窄时自动切换为单列。

首页精简重复说明，使用朋友般的短句鼓励；截止时间、提交状态和异常提示仍明确显示。便签悬停、按钮按下和详情窗口打开时使用轻微动画；倒计时刷新不触发动画。遵循系统的「减少动态效果」设置。

`dashboard.html`、`dashboard.js` 从磁盘加载；后续仅修改页面样式或前端显示逻辑时，刷新浏览器即可加载新界面，无需重启服务或重新登录。后端代码改动仍需重新启动。

```powershell
# 单次同步：本人登录后只输出数量和通用状态，随后清空内存
.venv\Scripts\python -m manaba_deadline sync

# 自定义同步间隔（最小 60 秒）和本机端口
.venv\Scripts\python -m manaba_deadline serve --interval 600 --port 8766
```

macOS/Linux 使用 `python3 -m venv .venv` 和 `.venv/bin/python` 执行同样命令。本项目当前开发机已将 Chromium 安装到项目内 `.browser-runtime`，启动时使用：

```sh
PLAYWRIGHT_BROWSERS_PATH=.browser-runtime .venv/bin/python -m manaba_deadline serve
```

正常安装到 Playwright 默认位置后无需该环境变量。Ctrl+C 关闭服务并清空内存会话。浏览器临时运行目录由 Playwright 清理，不保存持久登录配置、作业缓存、截图或追踪。

## 抓取行为和字段

读取当前课程（全部年度）中周一至周六星期课程表内的课程。课程表外的「その他の曜日」、自学自习、校内调查等课程及其作业会被过滤；课程范围由网页动态识别，不绑定账号或固定课程编号。未提交汇总列表只补充这个范围内的作业，不会加入额外课程。请在 manaba 课程列表使用「曜日」视图；无法识别课程表时会提示，不扩大读取范围。

按页面实际链接补读范围内课程的小测、问卷、报告、项目列表，跟随显示的分页，去重后按截止排序。课程中隐藏的任务和已经发布但尚未开放的任务可由课程列表补齐。已经提交和已截止的任务仍保留列表元数据，不打开答案或提交记录详情。

作业字段：稳定 `id`、`course_id`、`course_name_ja`、`title_ja`、`kind`、`content_ja`、`content_status`、`starts_at`、`deadline_at`、`late_deadline_at`、`submission_status`、`availability`、`raw_status_ja`、`source_url`、`provenance`、`issues`。

- 课程、标题、正文保持日文字符，正文保留换行；界面标签、日期、状态、倒计时为中文。
- 时间统一为 `Asia/Tokyo`，JSON 使用带 `+09:00` 的 ISO 8601，精确到源页面提供的分钟或秒；`24:00` 正确换到次日。
- 空白、`未設定`、`無期限` 等截止为 `null`，显示「截止时间未知」。无法解析的时间也是 `null`，另外给出解析失败提示。不会从标题、日历按钮或经验推断日期。
- 原截止与迟交截止分别保留；页面提供迟交截止时，在原截止后切换倒计时，明确显示原截止和迟交截止。
- 聚合列表只说明「未提交」，不推断其他提交状态；课程列表和安全详情补充已提交、未提交、进行中、尚未开放等状态。
- 报告读取「問題」，小测/问卷读取开始前的「課題に関する説明」等说明。若题目需要按「スタート」才能看到，标记正文不可安全读取，不启动计时。空正文和解析失败分别标记。
- 外部教材以及尚未确认安全入口的项目详情不会打开，正文状态明确提示。未加入的项目没有本人入口时跳过。图片、附件、PDF、外部链接中的正文目前不提取，会给出提示并保留来源入口。
- 只发出白名单 GET 请求；详情不运行网页脚本、不点击按钮、不提交或取消提交，不读取学生回答、同学内容、团队讨论或校园设置。未知入口不猜测。
- 默认每次至少间隔 0.75 秒读取一页，最多 250 页/80 门课程。未知分页、字段、布局、请求失败和上限截断明确产生提示，不把解析失败当成零作业。

`complete` 表示此次列表发现和解析是否完整；`scope="weekday_timetable"`、`scope_complete` 表示星期课程表范围及其识别完整性。正文能否读取请查看每条 `content_status` 与 `issues`。遇到局部失败会返回可读取的数据并标注不完整；以前存在但本次未重新读取的作业只在允许的课程范围内保留并标为「上次数据」。已确认移出课程表的课程不会由旧快照恢复。登录失效或全部读取失败时保留上次快照并标为旧数据，只有完整同步才更新「上次完整同步」时间。

## 小窗可调用接口

```python
import asyncio
from manaba_deadline.config import ScraperConfig
from manaba_deadline.service import DeadlineService

async def main():
    async with DeadlineService(ScraperConfig(interval_seconds=600)) as service:
        service.start_scheduler()
        await service.login()  # 本人在弹出的浏览器正常登录，成功后自动同步
        view = service.assignments()  # 日文数据 + 中文倒计时；不要写入日志
        status = service.status()     # 通用状态，适合日志/状态栏
        await service.sync()          # 手动同步，内部防重叠
        # GUI 应维持该异步会话，并按本地时钟刷新显示；退出时 close()

asyncio.run(main())
```

`ManabaAdapter(transport, config).sync()` 返回 `SyncResult`，适合接入自有显示端。`DeadlineService` 提供 `request_sync()`、`request_login()`、`change_account(url)` 和定时器。更换账号必须经 `change_account` 或重新启动会话，避免混用两位用户的数据。

本机 JSON 接口只监听 `127.0.0.1`：`GET /api/status`、`GET /api/assignments`，`POST /api/login`、`POST /api/sync`、`POST /api/account`（`{"url":"..."}`）。请求需使用服务实例的内存令牌 `X-Manaba-Token`，以及准确的 Host/同源 Origin；不启用 CORS，不记录请求内容。本机页面自动带令牌。桌面集成优先使用 Python 接口，避免把令牌或私有作业数据存进文件。

## 验证和限制

```sh
.venv/bin/python -m unittest discover -s tests -v
```

测试 fixture 均为合成脱敏日文数据，与真实课程/账号无对应关系。覆盖日期/时区、课程遗漏补读、分页/去重、字段缺失、正文边界、危险链接、登录失效、限流、定时器与防重叠。

本次验证：115 项 Python 测试与 14 项 JavaScript 显示逻辑测试全部通过；Python 3.12、Beautiful Soup 4.15、Playwright 1.63（macOS）。覆盖星期课程表范围识别、范围外作业过滤、旧数据保留、课程元数据、前端文件刷新、已提交优先、迟交切换、未知截止、课程分组与未提交课题的优先显示。便签布局已用合成数据在本机浏览器检查，无横向溢出，刷新保留记录展开状态。

JavaScript 显示逻辑检查可使用 Node.js 执行 `node --test tests/test_dashboard_state.cjs`；正常启动助手无需 Node.js。

开发期间已通过用户当前浏览器会话只读核对 manaba 2.98 的聚合列表、课程筛选、报告列表（含尚未开放与已提交）、报告详情（非空/空正文/无截止）、小测开始前说明与不可加入的项目列表。用户在独立应用正常登录后，旧版已完成一次真实账号同步；本次收窄课程范围的版本已启动，实际同步需在新窗口正常登录后验证。没有启动小测、提交作业或修改校园设置。独立应用需要使用者在自己的新登录窗口完成认证，现有浏览器会话不会自动共享。开发机为 macOS，Windows 11 安装脚本和运行结构已提供，Windows 实机与 exe 打包尚未验证。

页面布局变化可能需要更新适配器；遇到无法识别的布局会显示不完整提示。产品不是学校官方客户端。

依据：[龙谷大学 e-learning 说明](https://www.ryukoku.ac.jp/campus_career/media/e_learning/)、[manaba 学生手册](https://doc.manaba.jp/doc/course2-manual/student2.976/ja/)。手册确认隐藏任务保留在课程列表、迟交截止独立存在，且 Start 会启动小测计时，所以当前实现遵循这些边界。
