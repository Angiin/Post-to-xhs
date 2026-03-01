# publish - 小红书内容发布、搜索与提取

通过 CDP 自动化操作小红书，支持发布图文/长文、搜索笔记、提取笔记详情与评论。

## 前置条件

- 已安装 Python 3.10+ 和 Google Chrome
- 已安装依赖：`pip install -r requirements.txt`
- 首次使用需登录：`python scripts/cdp_publish.py login`

## 发布内容

根据用户输入自动判断发布方式，简化发布流程：

- 用户提供完整内容 + 图片/图片URL → 直接进入发布流程
- 用户提供网页 URL → 使用 WebFetch 提取内容和图片 → 适当总结 → 确认后发布

### 标题规则

标题长度 ≤ 38，计算规则：
- 中文字符和中文标点：每个计 2
- 英文字母/数字/空格/ASCII标点：每个计 1

### 图文发布

```bash
python scripts/publish_pipeline.py --headless \
    --title "标题" \
    --content "正文" \
    --image-urls "URL1" "URL2"
```

图文模式**必须提供图片**（`--image-urls` 或 `--images`）。

### 长文发布

```bash
python scripts/publish_pipeline.py --headless \
    --mode long-article \
    --title "标题" \
    --content-file article.txt
```

### 从文件读取

```bash
python scripts/publish_pipeline.py --headless \
    --title-file title.txt \
    --content-file content.txt \
    --image-urls "URL1" "URL2"
```

### 发布参数

| 参数 | 说明 |
|------|------|
| `--title TEXT` | 标题文本 |
| `--title-file FILE` | 从 UTF-8 文件读取标题 |
| `--content TEXT` | 正文文本 |
| `--content-file FILE` | 从 UTF-8 文件读取正文 |
| `--mode` | `image-text`（默认）或 `long-article` |
| `--image-urls URL...` | 图片 URL 列表 |
| `--images FILE...` | 本地图片路径列表 |
| `--headless` | 无头模式，未登录时自动切换有窗口模式 |
| `--auto-publish` | 自动点击发布（默认仅填写不发布） |
| `--account NAME` | 指定账号 |
| `--temp-dir DIR` | 图片临时下载目录 |

退出码：0 = 成功，1 = 未登录，2 = 错误

## 搜索笔记

```bash
# 基本搜索
python scripts/cdp_search.py search --keyword "关键词"

# 带筛选
python scripts/cdp_search.py search --keyword "关键词" --sort-by 最新 --note-type 图文

# 限制数量
python scripts/cdp_search.py search --keyword "关键词" --limit 5

# 有窗口模式
python scripts/cdp_search.py --headed search --keyword "关键词"

# 输出原始 JSON
python scripts/cdp_search.py search --keyword "关键词" --raw
```

### 筛选选项

| 参数 | 可选值 |
|------|--------|
| `--tab` | all、image、video、user |
| `--sort-by` | 综合、最新、最多点赞、最多评论、最多收藏 |
| `--note-type` | 不限、视频、图文 |
| `--publish-time` | 不限、一天内、一周内、半年内 |
| `--search-scope` | 不限、已看过、未看过、已关注 |
| `--location` | 不限、同城、附近 |
| `--limit` | 最大结果数（默认 20，0 = 全部） |

搜索结果包含笔记 ID、xsec_token、标题、作者、发布时间等信息。

## 提取笔记详情

需要笔记 ID 和 xsec_token（可从搜索结果中获取）。

```bash
# 提取单条笔记
python scripts/cdp_feed_detail.py detail \
    --feed-id <笔记ID> \
    --xsec-token <TOKEN>

# 加载评论
python scripts/cdp_feed_detail.py detail \
    --feed-id <笔记ID> \
    --xsec-token <TOKEN> \
    --load-comments

# 批量提取（传入 JSON 数组或文件）
python scripts/cdp_feed_detail.py batch \
    --feeds '[{"feed_id":"abc","xsec_token":"xyz"}]'

# 批量提取（从文件读取）
python scripts/cdp_feed_detail.py batch --feeds feeds.json

# 有窗口模式
python scripts/cdp_feed_detail.py --headed detail \
    --feed-id <笔记ID> --xsec-token <TOKEN>
```

### 评论加载选项

| 参数 | 说明 |
|------|------|
| `--load-comments` | 滚动加载所有评论 |
| `--click-more-replies` | 点击"展开更多回复" |
| `--max-replies-threshold N` | 跳过回复数超过 N 的展开按钮（默认 10） |
| `--max-comments N` | 最大评论数（0 = 不限） |
| `--scroll-speed` | slow / normal / fast（默认 normal） |

## 账号管理

```bash
# 列出账号
python scripts/cdp_publish.py list-accounts

# 添加账号
python scripts/cdp_publish.py add-account work --alias "工作账号"

# 登录账号
python scripts/cdp_publish.py --account work login

# 设置默认账号
python scripts/cdp_publish.py set-default-account work

# 切换账号（重新扫码）
python scripts/cdp_publish.py switch-account
```

## 注意事项

- 发布前**必须让用户确认内容**，不要直接使用 `--auto-publish`
- 图文模式必须有图片，长文模式图片可选
- 如需登录，脚本会自动切换到有窗口模式
- 搜索和详情提取默认使用无头模式
- 搜索结果中的 `xsec_token` 用于提取笔记详情，需配对使用
