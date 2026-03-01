# xiaohongshu-skill

自动发布、搜索和提取小红书（Xiaohongshu/RED）内容的命令行工具。

通过 Chrome DevTools Protocol (CDP) 实现自动化，支持多账号管理、无头模式运行，仅依赖 `requests` 和 `websockets` 两个第三方库。

## 功能特性

- **内容发布**：支持图文发布和长文发布两种模式
- **搜索笔记**：按关键词搜索小红书笔记，支持多种筛选条件
- **笔记详情提取**：获取笔记完整内容和评论
- **网页内容提取**：通过 URL 提取标题、内容和图片
- **多账号支持**：各账号 Cookie 隔离，独立 Chrome Profile
- **无头模式**：后台运行，无需显示浏览器窗口
- **图片下载**：自动下载图片，添加 Referer 绕过防盗链
- **登录检测**：自动检测登录状态，未登录时切换到有窗口模式扫码

## 项目结构

```
xiaohongshu-skill/
├── config/
│   ├── accounts.json             # 账号配置（gitignored）
│   └── accounts.json.example     # 配置模板
├── docs/
│   └── claude-code-integration.md
├── images/
│   └── publish_temp/             # 临时图片目录（gitignored）
├── scripts/
│   ├── account_manager.py        # 多账号管理
│   ├── cdp_feed_detail.py        # 笔记详情提取
│   ├── cdp_publish.py            # 核心发布引擎（CDP 自动化）
│   ├── cdp_search.py             # 搜索功能
│   ├── chrome_launcher.py        # Chrome 进程管理
│   ├── image_downloader.py       # 图片下载（防盗链绕过）
│   └── publish_pipeline.py       # 统一发布入口
├── requirements.txt
└── README.md
```

## 安装

### 环境要求

- Python 3.10+
- Google Chrome 浏览器
- Windows 操作系统（目前仅测试 Windows）

### 安装依赖

```bash
pip install -r requirements.txt
```

## 快速开始

### 1. 首次登录

```bash
python scripts/cdp_publish.py login
```

在弹出的 Chrome 窗口中扫码登录小红书。

### 2. 发布图文内容

```bash
# 无头模式（推荐）
python scripts/publish_pipeline.py --headless \
    --title "文章标题" \
    --content "文章正文" \
    --image-urls "https://example.com/image.jpg"

# 有窗口模式（可预览）
python scripts/publish_pipeline.py \
    --title "文章标题" \
    --content "文章正文" \
    --image-urls "https://example.com/image.jpg"

# 从文件读取内容
python scripts/publish_pipeline.py --headless \
    --title-file title.txt \
    --content-file content.txt \
    --image-urls "https://example.com/image.jpg"

# 使用本地图片
python scripts/publish_pipeline.py --headless \
    --title "文章标题" \
    --content "文章正文" \
    --images "C:\path\to\image.jpg"
```

### 3. 发布长文

```bash
python scripts/publish_pipeline.py --headless \
    --mode long-article \
    --title "长文标题" \
    --content-file article.txt
```

长文模式支持自动排版模板选择，发布后可选择模板样式。

### 4. 搜索笔记

```bash
# 基本搜索
python scripts/cdp_search.py search --keyword "关键词"

# 带筛选条件
python scripts/cdp_search.py search --keyword "关键词" --sort-by 最新 --note-type 图文

# 限制结果数量
python scripts/cdp_search.py search --keyword "关键词" --limit 5

# 无头模式
python scripts/cdp_search.py --headless search --keyword "关键词"

# 输出原始 JSON
python scripts/cdp_search.py search --keyword "关键词" --raw
```

#### 搜索筛选选项

| 参数 | 可选值 |
|------|--------|
| `--sort-by` | 综合、最新、最多点赞、最多评论、最多收藏 |
| `--note-type` | 不限、视频、图文 |
| `--publish-time` | 不限、一天内、一周内、半年内 |
| `--search-scope` | 不限、已看过、未看过、已关注 |
| `--location` | 不限、同城、附近 |

### 5. 提取笔记详情

```bash
# 提取单条笔记
python scripts/cdp_feed_detail.py detail --feed-id <笔记ID>

# 加载评论
python scripts/cdp_feed_detail.py detail --feed-id <笔记ID> --load-comments

# 批量提取
python scripts/cdp_feed_detail.py batch --feed-ids <ID1> <ID2> <ID3>

# 无头模式
python scripts/cdp_feed_detail.py --headless detail --feed-id <笔记ID>
```

### 6. 多账号管理

```bash
# 列出所有账号
python scripts/cdp_publish.py list-accounts

# 添加新账号
python scripts/cdp_publish.py add-account myaccount --alias "我的账号"

# 登录指定账号
python scripts/cdp_publish.py --account myaccount login

# 使用指定账号发布
python scripts/publish_pipeline.py --account myaccount --headless \
    --title "标题" --content "正文" --image-urls "URL"

# 设置默认账号
python scripts/cdp_publish.py set-default-account myaccount

# 切换账号（清除当前登录，重新扫码）
python scripts/cdp_publish.py switch-account
```

## 命令参考

### publish_pipeline.py

统一发布入口，一条命令完成全部流程。

```bash
python scripts/publish_pipeline.py [选项]

选项:
  --title TEXT           文章标题
  --title-file FILE      从文件读取标题
  --content TEXT         文章正文
  --content-file FILE    从文件读取正文
  --image-urls URL...    图片 URL 列表
  --images FILE...       本地图片文件列表
  --mode MODE            发布模式：image-text（默认）或 long-article
  --headless             无头模式（无浏览器窗口）
  --account NAME         指定账号
  --auto-publish         自动点击发布（跳过确认）
  --temp-dir DIR         图片临时下载目录
```

退出码：0 = 成功，1 = 未登录，2 = 错误

### cdp_publish.py

底层发布控制，支持分步操作。

```bash
# 检查登录状态
python scripts/cdp_publish.py check-login

# 填写表单（不发布）
python scripts/cdp_publish.py fill --title "标题" --content "正文" --images img.jpg

# 长文发布
python scripts/cdp_publish.py long-article --title "标题" --content "正文"

# 选择排版模板
python scripts/cdp_publish.py select-template --template-name "模板名"

# 点击发布按钮
python scripts/cdp_publish.py click-publish

# 账号管理
python scripts/cdp_publish.py login
python scripts/cdp_publish.py list-accounts
python scripts/cdp_publish.py add-account NAME [--alias ALIAS]
python scripts/cdp_publish.py remove-account NAME [--delete-profile]
python scripts/cdp_publish.py set-default-account NAME
python scripts/cdp_publish.py switch-account
```

### cdp_search.py

搜索小红书笔记。

```bash
python scripts/cdp_search.py [--headless] [--account NAME] search [选项]

选项:
  --keyword TEXT         搜索关键词（必填）
  --sort-by TEXT         排序方式
  --note-type TEXT       笔记类型
  --publish-time TEXT    发布时间
  --search-scope TEXT    搜索范围
  --location TEXT        位置筛选
  --limit N              最大结果数
  --raw                  输出原始 JSON
```

### cdp_feed_detail.py

提取笔记详情和评论。

```bash
# 单条笔记
python scripts/cdp_feed_detail.py [--headless] [--account NAME] detail [选项]

选项:
  --feed-id ID           笔记 ID（必填）
  --load-comments        是否加载评论
  --scroll-speed SPEED   评论加载速度：slow / normal / fast

# 批量提取
python scripts/cdp_feed_detail.py [--headless] [--account NAME] batch [选项]

选项:
  --feed-ids ID...       笔记 ID 列表（必填）
  --load-comments        是否加载评论
```

### chrome_launcher.py

Chrome 浏览器管理。

```bash
# 启动 Chrome
python scripts/chrome_launcher.py
python scripts/chrome_launcher.py --headless

# 重启 Chrome
python scripts/chrome_launcher.py --restart

# 关闭 Chrome
python scripts/chrome_launcher.py --kill
```

## 技术特点

- **CDP 直连**：通过 WebSocket 直接控制 Chrome，无需 Selenium/Playwright，避免驱动版本兼容问题
- **最小依赖**：仅依赖 `requests` 和 `websockets`
- **选择器集中管理**：所有 CSS 选择器集中在 `SELECTORS` 字典中，页面结构变化时易于维护
- **智能登录处理**：无头模式下检测到未登录自动切换有窗口模式扫码

## 支持各种 Skill 工具

本项目可作为 Claude Code、OpenCode 等支持 Skill 的工具使用，只需将项目复制到 `.claude/skills/xiaohongshu-skill/` 目录，并添加 `SKILL.md` 文件即可。

详见 [docs/claude-code-integration.md](docs/claude-code-integration.md)

## 注意事项

1. **仅供学习研究**：请遵守小红书平台规则，不要用于违规内容发布
2. **登录安全**：Cookie 存储在本地 Chrome Profile 中，请勿泄露
3. **选择器更新**：如果小红书页面结构变化导致发布失败，需要更新 `cdp_publish.py` 中的选择器

## 许可证

MIT License
