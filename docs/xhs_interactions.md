# 小红书评论与回复模块

本 fork 新增 `media_platform.xhs.interactions`，供本仓库及其他项目调用。模块不依赖 `media_hub`，现有搜索、正文和评论采集流程仍由原采集器负责。

## 入口与职责

| 文件 | 职责 |
| --- | --- |
| `models.py` | 明确笔记链接、正文、回复 ID 与本地登录目录；操作指纹 |
| `audit.py` | SQLite 事务发送预留、追加事件、重复操作拦截 |
| `browser.py` | 原生网页输入/回复定位、写请求拦截、单次发送与结果确认 |
| `cli.py`、`__main__.py` | 独立命令行入口 |

发送仍使用原生网页请求，没有新增猜测签名的 HTTP 写接口，也不调用带重试的采集请求方法。模块使用采集器现有 Playwright 依赖；可直接接收采集器登录后得到的异步 `Page`，无需再次登录。独立运行则使用指定的持久化 Chrome 登录目录。

## 独立 CLI

依照仓库 README 安装环境，系统需已安装 Google Chrome。从采集器根目录运行：

```powershell
.\.venv\Scripts\python.exe -m media_platform.xhs.interactions --help
.\.venv\Scripts\python.exe -m media_platform.xhs.interactions comment `
  --note-url "https://www.xiaohongshu.com/explore/<24位笔记ID>?<原链接查询参数>" `
  --text-file "C:\path\comment.txt" `
  --browser-profile "C:\path\existing-profile" `
  --audit-db "C:\path\shared-audit.sqlite3" `
  --screenshot "C:\path\preview.png"
```

回复使用 `reply` 并添加 `--comment-id "<24位目标评论ID>"`。默认仅预览，不点击发送，所有非 GET/HEAD/OPTIONS 请求均被阻断。只有明确授权该条正文后添加 `--send` 才尝试一次发送。正文按 UTF-8 文件原样读取（移除 BOM），限制 1–500 字符。

默认审计路径为当前工作目录下 `data/interactions/audit.sqlite3`；跨项目共用账号时应显式指定同一个绝对审计路径。登录目录、审计库和截图留在本地。

## 异步 Python API

独立浏览器操作：

```python
from pathlib import Path
from media_platform.xhs.interactions import Audit, Interaction, run

interaction = Interaction(
    note_url=note_url,
    text=text,
    profile=Path(existing_profile),
    comment_id="",  # 普通评论；回复时传目标评论 ID
)
audit = Audit(Path(shared_audit_path))
try:
    result = await run(interaction, audit, send=False)
finally:
    audit.close()
```

复用采集器已登录页面：

```python
from media_platform.xhs.interactions import run_on_page

# 登录上下文须在创建时指定 service_workers="block"。
# 整个上下文串行使用；页面不能安装其他会优先放行写请求的 page.route。
result = await run_on_page(
    crawler.context_page, interaction, audit, send=False
)
```

`run_on_page` 会导航到指定笔记，暂时在整个 `BrowserContext` 上安装请求防护，结束时移除自己的路由及响应监听，不关闭调用方的页面或浏览器。调用方必须确保上下文创建时禁用 Service Worker、没有其他优先于上下文防护的页面路由，并避免并发运行采集或互动。检测到已有 Service Worker 时，模块直接拒绝操作；当前采集器的默认上下文创建逻辑不自动设置禁用选项，复用时需由调用方显式设置，不能仅凭当前 worker 列表为空认定安全。

外部项目可用采集器根目录作为子进程 `cwd`，调用 `python -m media_platform.xhs.interactions`；若在 Python 中导入，运行环境须能找到采集器根目录及其依赖。当前仓库不是独立发布的 pip SDK，不要求安装整个仓库为可编辑包。

## 发送保护与状态

- 只对指定笔记及已加载的精确目标评论操作；查找最多滚动 9 轮，不自动展开全部二级评论。
- 点击前以 SQLite 事务预留发送；相同登录目录、笔记、回复 ID 及正文只能尝试一次，预览不占用名额。
- 只放行一次匹配 `edith.xiaohongshu.com/api/sns/web/v1/comment/post` 的 POST，核对 `note_id`、`content`、`target_comment_id`，拒绝图片及 @ 用户。
- HTTP 200、`success=true`、整数 `code=0` 与有效 `data.comment.id` 同时满足才确认成功。响应头等待约 15 秒，正文读取另设 15 秒上限，未知结果不重试。
- 审计事件只追加，记录 ID、时间及正文/登录目录摘要，不记录正文、Cookie 或 URL 访问参数。

| 状态 | 含义 | CLI 退出码 |
| --- | --- | --- |
| `preview` | 已完成填写预览，无发送 | 0 |
| `confirmed` | 匹配请求响应明确成功 | 0 |
| `not_dispatched` | 预留后没有放行写请求 | 2 |
| `unknown` | 请求已放行，但发送结果无法确认 | 2 |
| `error` | 参数、页面、环境或审计错误 | 2 |

任何已有发送尝试都会保留预留，包括 `unknown`、`not_dispatched` 和中断后留下的 `submitting`。先人工检查网页及审计；不通过换库、换正文或删除历史绕过未决操作。迁移前的审计数据库和指纹算法保持兼容，跨项目使用时必须共享数据库才能共享去重保护。

## 验证与限制

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_xhs_interactions -v
```

测试覆盖校验、审计、请求拦截、模拟响应、异常状态、已有页面复用及本地 Chrome 一级/二级回复定位。真实网页评论/一级回复预览已验证。2026-10-28 经用户授权，通过 `run_on_page(..., send=True)` 实际发送一条普通评论：匹配的 POST 得到 HTTP 200、`success=true`、`code=0`、有效 `data.comment.id`，页面回显与审计确认一致。

真实回复发送及 CLI 发送本次没有单独验收，不重复发送评论。页面及接口变化时停止或记录未知，不自动重试。不含私信、批量互动、图片评论或定时发送。
