---
name: playwright
description: Browser automation CLI for coding agents. Use when you need to interact with websites, including navigating pages, filling forms, clicking buttons, taking screenshots, extracting data, testing web apps, or automating any browser task.
allowed-tools: Bash(python:*)
---

# Playwright Browser Automation

## 环境要求

```bash
pip install playwright
playwright install chromium
```

---

## 完整流程（必须按顺序执行）

> **注意**：超时10分钟(600000ms)，默认最小化启动浏览器

### Step 1: 编写脚本

脚本负责搜索和抓取内容，存入md文件。摘要生成由模型完成。

```python
import asyncio
import urllib.parse
import random
import sys
import io
import os
import inspect

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from playwright.async_api import async_playwright

# 保存脚本到文件以便后续自动删除
_script_code = inspect.getsource(sys.modules[__name__])
_script_path = os.path.abspath(__file__)
with open(_script_path, 'w', encoding='utf-8') as f:
    f.write(_script_code)

def random_sleep(min_sec=1, max_sec=5):
    return asyncio.sleep(random.uniform(min_sec, max_sec))

async def human_scroll(page, min_times=2, max_times=5):
    times = random.randint(min_times, max_times)
    for _ in range(times):
        scroll_amount = random.randint(300, 800) * random.choice([1, -1])
        await page.evaluate(f'window.scrollBy(0, {scroll_amount})')
        await random_sleep(1, 3)

async def human_mouse_move(page):
    viewport = page.viewport_size
    if not viewport:
        return
    start_x, start_y = random.randint(100, viewport['width'] - 100), random.randint(100, viewport['height'] - 100)
    end_x, end_y = random.randint(100, viewport['width'] - 100), random.randint(100, viewport['height'] - 100)
    steps = random.randint(5, 15)
    for i in range(steps):
        x = start_x + (end_x - start_x) * i / steps
        y = start_y + (end_y - start_y) * i / steps
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.05, 0.15))

async def close_popup_and_expand(page):
    await asyncio.sleep(1)
    await page.keyboard.press("Escape")
    await asyncio.sleep(1)
    await page.evaluate("""() => {
        const patterns = [/展开全文/, /阅读全文/, /展开全部/, /查看全部/, /展开更多/];
        document.querySelectorAll('*').forEach(el => {
            patterns.forEach(p => { if (p.test(el.innerText)) el.click(); });
        });
    }""")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=['--start-minimized'])
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})

        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
            window.chrome = { app: { getIsInstalled: () => {} }, runtime: { id: 'abcdefghijklmnop' } };
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return getParameter.apply(this, arguments);
            };
            Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
            Object.defineProperty(screen, 'availHeight', { get: () => 1080 });
            Object.defineProperty(navigator, 'connection', { get: () => ({ downlink: 10, effectiveType: '4g' }) });
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
        """)

        keyword = "搜索关键词"  # TODO: 修改为实际关键词
        encoded = urllib.parse.quote(keyword)

        await page.goto(f"https://www.baidu.com/s?wd={encoded}&tn=news", wait_until="domcontentloaded")
        await random_sleep(2, 4)
        await human_scroll(page)
        await human_mouse_move(page)

        results = page.locator(".c-container h3 a")
        count = await results.count()

        results_data = []
        for i in range(min(5, count)):
            try:
                title = await results.nth(i).inner_text()
                href = await results.nth(i).get_attribute('href')
                if href:
                    results_data.append({'title': title, 'url': href, 'content': ''})
            except:
                pass

        for r in results_data:
            is_zhihu = "zhihu" in r.get('url', '').lower() or "知乎" in r.get('title', '')
            try:
                await page.goto(r['url'], timeout=15000, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                if is_zhihu:
                    await asyncio.sleep(random.uniform(1, 3))
                    await page.keyboard.press("Escape")
                await close_popup_and_expand(page)
                await human_scroll(page, 2, 3)
                await random_sleep(1, 2)
                r['content'] = await page.evaluate("() => document.body.innerText")
                await random_sleep(2, 4)
            except Exception as e:
                r['content'] = f"获取失败: {str(e)}"

        # 写入md文件（仅原始内容，不含摘要）
        safe_keyword = "".join(c if c.isalnum() or c in ('_', '-') else '_' for c in keyword)
        script_name = f"search_py_{safe_keyword}.py"
        filename = f"result_{safe_keyword}.md"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"# {keyword}\n\n")
            f.write("> **原始搜索结果，内容待模型处理。**\n\n")
            f.write("---\n\n")
            f.write("## 📄 详细内容\n\n")
            for i, r in enumerate(results_data):
                f.write(f"### {i+1}. {r['title']}\n\n")
                f.write(f"**来源**: {r['url']}\n\n")
                f.write(f"{r['content']}\n\n")
                f.write("---\n\n")

        print(f"搜索完成，结果已保存到: {filename}")

        # 删除临时脚本
        script_path = os.path.abspath(__file__)
        if os.path.exists(script_path):
            os.remove(script_path)
            print(f"临时脚本已删除: {script_path}")

        await browser.close()

asyncio.run(main())
```

### Step 2: 执行脚本

脚本文件名格式：`search_py_关键词.py`

```bash
powershell -Command "python C:/Users/zxl/Downloads/search_py_关键词.py"
```

脚本执行后自动删除，生成 `result_关键词.md`。

### Step 3: 模型生成摘要（必须执行）

> **⚠️ 强制要求：摘要必须100%生成，不允许跳过或失败**

读取md文件，按以下**两轮**严格操作。**每轮必须完整执行，不得省略任何步骤。**

#### 第一轮：生成详细整体摘要

**必须包含以下模块（缺一不可）：**

1. `> 概括核心要点` - 核心要点一句话概括
2. `## 📋 核心数据表` - Markdown表格
3. `## ⏱️ 发展时间线` - Markdown表格
4. `## 🔍 详细客观事件分析` - 至少3个子章节
5. `## 📊 关键对比（如有）`
6. `## 🎯 后续展望与挑战`

**输出模板：**

```markdown
# 搜索主题

> 概括核心要点

---

## 📋 核心数据表

| 项目 | 内容 |
|------|------|
| ... | ... |

---

## ⏱️ 发展时间线

| 时间 | 事件 |
|------|------|
| ... | ... |

---

## 🔍 详细客观事件分析

### 一、背景与现状
...

### 二、关键发现
...

### 三、趋势分析
...

### 四、多方观点（如有）
...

### 五、数据引用
...

---

## 📊 关键对比（如有）

| 维度 | A | B |
|------|---|---|
| ... | ... | ... |

---

## 🎯 后续展望与挑战

### 潜在机会
...

### 面临挑战
...

---

#### 第二轮：生成各条结果摘要

1. **重新读取md文件**
2. **遍历所有搜索结果**（每个结果都必须生成摘要）
3. **逐条生成摘要**（50-150字），格式如下：

```markdown
## 📄 各条结果摘要

### 1. 标题1
**来源**: https://example.com/1

**摘要：** 50-150字的摘要内容...

**来源：** 来源媒体 | **日期：** YYYY-MM-DD

---

### 2. 标题2
...
```

---

#### 完整性验证（必须执行）

生成摘要后，**必须验证**以下内容：

1. ✅ 文件存在且非空（>500字节）
2. ✅ 包含 `# 搜索主题` 标题
3. ✅ 包含 `## 📋 核心数据表`
4. ✅ 包含 `## ⏱️ 发展时间线`
5. ✅ 包含 `## 🔍 详细客观事件分析`
6. ✅ 包含 `## 📄 各条结果摘要`
7. ✅ 每个搜索结果都有摘要

**验证失败处理：**

如果验证不通过，**必须重新生成**摘要：
1. 重新读取原md文件（原始搜索结果）
2. 按模板重新生成完整摘要
3. 再次验证
4. 如果仍失败，生成**最小可用摘要**（至少包含核心要点、数据表、时间线）

**最小可用摘要模板（最终Fallback）：**

```markdown
# 搜索主题

> 核心要点一句话概括

---

## 📋 核心数据表

| 项目 | 内容 |
|------|------|
| 搜索主题 | ... |
| 结果数量 | N条 |

---

## ⏱️ 发展时间线

| 时间 | 事件 |
|------|------|
| ... | ... |

---

## 🔍 详细客观事件分析

### 一、概述
基于N条搜索结果的综合分析。

### 二、主要发现
- 发现1
- 发现2
- 发现3

### 三、关键数据
（从搜索结果中提取的关键数据）

---

## 📄 各条结果摘要

### 1. [标题]
**来源**: [URL]
**摘要：** [50-150字摘要]
**来源：** [媒体] | **日期：** [日期]
```

---

#### 最终文件结构（必须包含）：

```markdown
# 搜索主题

> 概括核心要点

---

## 📋 核心数据表
...

## ⏱️ 发展时间线
...

## 🔍 详细客观事件分析
...

## 📊 关键对比
...

## 🎯 后续展望与挑战
...

---

## 📄 各条结果摘要

### 1. 标题1
...

### 2. 标题2
...
```

**注意：**
- 第一轮摘要要求详尽，应涵盖主题背景、关键数据、时间线、多维分析等
- 摘要中引用的具体数据需标注来源
- 第二轮各条摘要要求简洁（50-150字）
- 两轮操作之间**重新读取md文件**
- **不得跳过任何步骤，必须完整生成**
- 如果模型输出过长被截断，重新读取并补全

---

## 浏览器启动模式

| 模式 | 设置 | 说明 |
|------|------|------|
| 最小化（默认） | `headless=False, args=['--start-minimized']` | 有窗口但启动时最小化 |
| 无头模式 | `headless=True` | 完全隐藏 |
| 普通模式 | `headless=False` | 正常显示窗口 |

---

## 常用API

### 元素定位
```python
page.locator('#id')
page.locator('.class')
page.locator('xpath=//div[@id="main"]')
```

### 获取内容
```python
text = await page.evaluate('() => document.body.innerText')
title = await page.title()
url = page.url
```

### 等待策略
```python
await page.goto(url, wait_until="domcontentloaded")
await asyncio.sleep(3)
```

---

## 常见问题

### 百度搜索
```python
# 错误
await page.fill('#kw', '关键词')

# 正确 - URL直接搜索
await page.goto(f"https://www.baidu.com/s?wd={urllib.parse.quote(keyword)}")

# 只搜索网页+资讯
await page.goto(f"https://www.baidu.com/s?wd={encoded}&tn=news")
```

### 选择器参考
| 场景 | 选择器 |
|------|--------|
| 百度搜索结果 | `.c-container h3 a` |
| B站视频卡片 | `.bili-video-card` |
| YouTube视频缩略图 | `a#thumbnail[href*='/watch?v=']` |
| YouTube视频容器 | `ytd-rich-item-renderer`, `ytd-grid-video-renderer` |
| YouTube频道名称 | `ytd-video-owner-renderer #channel-name a` |
| YouTube视频标题 | `#title, #video-title, .title` |

### 知乎反爬
```python
# 检测知乎链接
is_zhihu = "zhihu" in href.lower() or "知乎" in title

# 关闭登录弹窗
await asyncio.sleep(random.uniform(1, 3))
await page.keyboard.press("Escape")
```

### YouTube视频爬取
```python
from urllib.parse import urlparse
import argparse

# 1. URL类型自动检测
def detect_url_type(url):
    path = urlparse(url.lower()).path
    if '/watch' in path:
        return 'video'
    elif '/@' in path or '/c/' in path or '/channel/' in path or '/user/' in path:
        return 'channel'
    return 'unknown'

# 2. 从视频页面获取频道信息
async def get_channel_from_video(page):
    channel_info = {}
    selectors = ["ytd-video-owner-renderer #channel-name a", "ytd-channel-name a", "#owner-name a"]
    for sel in selectors:
        el = page.locator(sel)
        if await el.count() > 0:
            channel_info['name'] = await el.first.inner_text()
            channel_info['url'] = await el.first.get_attribute('href')
            break
    return channel_info

# 3. 从频道页面获取频道名称
async def get_channel_name(page):
    name = await page.evaluate('''() => {
        const titles = document.querySelectorAll('h1');
        for (let el of titles) {
            const text = el.textContent?.trim();
            if (text && text.length > 0 && text.length < 100) return text;
        }
        return '';
    }''')
    return name if name else "Unknown"

# 4. 获取视频列表（推荐用evaluate更稳定）
async def fetch_videos(page, num):
    videos = []
    seen = set()
    thumbnails = page.locator("a#thumbnail[href*='/watch?v=']")
    count = await thumbnails.count()

    for i in range(min(count, 20)):
        try:
            href = await thumbnails.nth(i).get_attribute('href')
            if href and '/watch?v=' in href:
                clean = href.split('&')[0]
                if clean in seen:
                    continue
                seen.add(clean)

                title = await page.evaluate(f'''() => {{
                    const el = document.querySelectorAll('a#thumbnail[href*="/watch?v="]')[{i}];
                    const parent = el?.closest('ytd-rich-item-renderer') || el?.closest('ytd-grid-video-renderer');
                    const titleEl = parent?.querySelector('#title, #video-title') || parent?.querySelector('.title');
                    return titleEl?.textContent?.trim() || 'Untitled';
                }}''')

                videos.append({'title': title, 'url': clean})
        except:
            pass
        if len(videos) >= num:
            break
    return videos
```

### YouTube 完整脚本模板
```python
import asyncio
import random
import sys
import io
import re
import argparse
from urllib.parse import urlparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from playwright.async_api import async_playwright

def random_sleep(min_sec=1, max_sec=4):
    return asyncio.sleep(random.uniform(min_sec, max_sec))

async def human_scroll(page, times=3):
    for _ in range(times):
        await page.evaluate(f'window.scrollBy(0, {random.randint(400, 800)})')
        await random_sleep(1, 2)

async def close_popup(page):
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.5)

def detect_url_type(url):
    path = urlparse(url.lower()).path
    if '/watch' in path: return 'video'
    elif '/@' in path or '/c/' in path or '/channel/' in path: return 'channel'
    return 'unknown'

async def get_channel_from_video(page):
    channel_info = {}
    for sel in ["ytd-video-owner-renderer #channel-name a", "ytd-channel-name a", "#owner-name a"]:
        el = page.locator(sel)
        if await el.count() > 0:
            channel_info['name'] = await el.first.inner_text()
            channel_info['url'] = await el.first.get_attribute('href')
            break
    return channel_info

async def get_channel_name(page):
    name = await page.evaluate('''() => {
        const titles = document.querySelectorAll('h1');
        for (let el of titles) {
            const text = el.textContent?.trim();
            if (text && text.length > 0 && text.length < 100) return text;
        }
        return '';
    }''')
    return name if name else "Unknown"

async def fetch_videos(page, num):
    videos, seen = [], set()
    thumbnails = page.locator("a#thumbnail[href*='/watch?v=']")
    count = await thumbnails.count()
    for i in range(min(count, 20)):
        try:
            href = await thumbnails.nth(i).get_attribute('href')
            if href and '/watch?v=' in href:
                clean = href.split('&')[0]
                if clean in seen: continue
                seen.add(clean)
                title = await page.evaluate(f'''() => {{
                    const el = document.querySelectorAll('a#thumbnail[href*="/watch?v="]')[{i}];
                    const parent = el?.closest('ytd-rich-item-renderer') || el?.closest('ytd-grid-video-renderer');
                    const titleEl = parent?.querySelector('#title, #video-title') || parent?.querySelector('.title');
                    return titleEl?.textContent?.trim() || 'Untitled';
                }}''')
                videos.append({'title': title, 'url': clean})
        except: pass
        if len(videos) >= num: break
    return videos

async def get_youtube_videos(url, num=10):
    url_type = detect_url_type(url)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=['--start-minimized'])
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
            window.chrome = { app: { getIsInstalled: () => {} }, runtime: { id: 'abcdefghijklmnop' } };
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return getParameter.apply(this, arguments);
            };
            Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
            Object.defineProperty(screen, 'availHeight', { get: () => 1080 });
            Object.defineProperty(navigator, 'connection', { get: () => ({ downlink: 10, effectiveType: '4g' }) });
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            window.navigator.chrome = { runtime: {} };
        """)

        if url_type == 'video':
            await page.goto(url, wait_until="domcontentloaded")
            await random_sleep(3, 5)
            await close_popup(page)
            channel_info = await get_channel_from_video(page)
            channel_name = channel_info.get('name', 'Unknown')
            channel_url = channel_info['url']
            if not channel_url.startswith('http'):
                channel_url = 'https://www.youtube.com' + channel_url
        else:
            if not url.startswith('http'): url = 'https://www.youtube.com' + url
            await page.goto(url, wait_until="domcontentloaded")
            await random_sleep(3, 5)
            await close_popup(page)
            channel_name = await get_channel_name(page)
            channel_url = url

        videos = await fetch_videos(page, num)
        await browser.close()
        return channel_name, videos

async def main():
    parser = argparse.ArgumentParser(description='YouTube视频爬取')
    parser.add_argument('url', nargs='?', default='https://www.youtube.com/watch?v=xxx')
    parser.add_argument('-n', '--num', type=int, default=10)
    args = parser.parse_args()

    channel_name, videos = await get_youtube_videos(args.url, num=args.num)
    if videos:
        print(f"\n=== {channel_name} 最新{len(videos)}期视频 ===")
        for i, v in enumerate(videos, 1):
            print(f"{i}. {v['title']}")
            print(f"   https://www.youtube.com{v['url']}")

if __name__ == "__main__":
    asyncio.run(main())
```

### YouTube 反爬
```python
# 1. 基础反爬脚本
await page.add_init_script("""
    window.navigator.chrome = { runtime: {} };
    delete navigator.webdriver;
""")

# 2. 模拟鼠标平滑移动 + 随机悬停
async def human_mouse_move(page, elements=None):
    """模拟人类鼠标移动路径，可选悬停到指定元素"""
    viewport = page.viewport_size
    if not viewport:
        return

    points = [
        (random.randint(200, viewport['width']-200), random.randint(200, viewport['height']-200)),
        (random.randint(200, viewport['width']-200), random.randint(200, viewport['height']-200)),
        (random.randint(200, viewport['width']-200), random.randint(200, viewport['height']-200)),
    ]

    for x, y in points:
        steps = random.randint(8, 15)
        current_x, current_y = 0, 0
        for i in range(steps):
            target_x = x * i / steps + current_x * (steps - i) / steps
            target_y = y * i / steps + current_y * (steps - i) / steps
            await page.mouse.move(target_x, target_y)
            await asyncio.sleep(random.uniform(0.05, 0.15))

    if elements:
        for el in elements:
            if random.random() > 0.5:
                await el.hover()
                await random_sleep(0.5, 1.5)

async def human_scroll(page, times=3):
    """模拟人类滚动"""
    for _ in range(times):
        await page.evaluate(f'window.scrollBy(0, {random.randint(300, 700)})')
        await random_sleep(0.8, 2)
        if random.random() > 0.5:
            await page.evaluate(f'window.scrollBy(0, {random.randint(-200, -100)})')
            await random_sleep(0.5, 1)

# 使用示例
await human_scroll(page)
await human_mouse_move(page)
```

### YouTube 使用说明
```bash
# 视频链接
python youtube_latest.py https://www.youtube.com/watch?v=xxx -n 10

# 频道链接
python youtube_latest.py https://www.youtube.com/@channel -n 10
```

---

## 注意事项

1. Windows执行用PowerShell命令
2. 使用`&tn=news`排除图片/视频/百科
3. 操作间添加random_sleep模拟人类
4. 知乎链接需同时检测href和标题
5. 遇到验证暂停等待用户
6. 脚本超时10分钟
7. **摘要生成必须由模型完成，不要在代码中处理**
8. 文件编码统一为UTF-8

---
