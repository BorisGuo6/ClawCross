---
name: hyperframes
description: "HyperFrames — Write HTML. Render video. 把 HTML/CSS/JS 渲染成 MP4。本 SKILL 整合了官方 README 与实战调试经验：入口三属性 / __hf / __timelines 契约、9 个常见报错的修复路径、CDN 本地化建议、渲染命令。Author: HeyGen (heygen-com/hyperframes)"
---

# HyperFrames Skill（视频工坊增强版）

HyperFrames 让你用 HTML + CSS + JavaScript（GSAP timeline）写视频，输出 MP4。本团队的 Engineer / Reviewer persona 已内嵌本 SKILL 的核心内容，可直接交付项目。

## 1. 安装与渲染命令

```bash
# 安装（任选其一）
npm install -g hyperframes
npm install hyperframes

# 渲染（默认 Puppeteer 模式）
cd <project-dir>
npx hyperframes render .

# Docker 模式（系统需 Docker）
npx hyperframes render . --docker

# 指定输出
npx hyperframes render . --output ./output/video.mp4

# 渲染产物：<project-dir>/renders/<composition-id>_<时间戳>.mp4
```

## 2. HTML 入口契约（缺一不可）

入口 div 必须带三个 data 属性：

```html
<div class="container"
     data-composition-id="my-video"
     data-width="1080"
     data-height="1920">
  <!-- scenes here -->
</div>
```

可选但推荐：`data-start="0"` `data-duration="<秒>"`，便于 hyperframes 读总时长。

### 画幅尺寸对照

| 平台 | 宽×高 | data-width × data-height |
|------|-------|--------------------------|
| 抖音 / 小红书 / TikTok 竖屏 | 9:16 | 1080 × 1920 |
| YouTube / B站横屏 | 16:9 | 1920 × 1080 |
| Instagram 方形 | 1:1 | 1080 × 1080 |

## 3. JS 契约（`window.__hf` + `window.__timelines`）

```html
<script src="gsap.min.js"></script>  <!-- 本地，不要 CDN -->
<script>
  // 1) 必须是对象，不是数组
  window.__timelines = window.__timelines || {};

  // 2) 创建 paused timeline
  const tl = gsap.timeline({ paused: true });

  // 3) key 必须等于 data-composition-id
  window.__timelines["my-video"] = tl;

  // 4) tl.set 初始化场景，避免 CSS 闪现
  tl.set(".s1", { opacity: 0 })
    .set(".s2", { opacity: 0 });

  // 5) 单一时间线分段控制
  tl.to(".s1", { opacity: 1, duration: 0.5 }, 0)
    .from(".s1 .title", { opacity: 0, y: 30, duration: 1 }, 0)
    .to(".s1", { opacity: 0, duration: 0.5 }, 4.5);
  tl.to(".s2", { opacity: 1, duration: 0.5 }, 5)
    .to(".s2", { opacity: 0, duration: 0.5 }, 9.5);

  // 6) 暴露 __hf —— 没这行直接报 "window.__hf not ready"
  window.__hf = {
    duration: 10,
    seek: (t) => tl.seek(t)
  };
</script>
```

## 4. CSS 基础框架

```css
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body {
  width: 100%; height: 100%;
  overflow: hidden;
  background: #000;
  font-family: system-ui, -apple-system, sans-serif;
}
.container {
  width: 100vw; height: 100vh;
  position: relative; overflow: hidden;
}
.scene {
  position: absolute; top: 0; left: 0;
  width: 100%; height: 100%;
}
/* 场景初始隐藏交给 GSAP，CSS 不要写 opacity:0 */
```

## 5. 9 个常见报错与修复

| # | 报错 | 根因 | 修法 |
|---|------|------|------|
| 1 | `window.__hf not ready after 45000ms` | 没暴露 `window.__hf` 或 CDN GSAP 加载超时 | script 末尾加 `window.__hf = { duration, seek }`；本地化 GSAP |
| 2 | `root_missing_composition_id` | 入口 div 没 `data-composition-id` | 加上 |
| 3 | `root_missing_dimensions` | 入口 div 没 `data-width` / `data-height` | 加上 |
| 4 | `timeline_id_mismatch` | `__timelines["X"]` 的 key 和 `data-composition-id` 不一致 | 两边统一 |
| 5 | CDN 超时 → 表现为 #1 | jsdelivr / cdnjs 在渲染容器里不稳 | 下载到本地 `gsap.min.js` |
| 6 | `__timelines` 是数组不是对象 | 写成 `window.__timelines = [ ... ]` | 改成 `{}` 然后 `window.__timelines["X"] = tl` |
| 7 | 页面开头闪一下 | CSS 写了 opacity:0，GSAP 还没接管 | 用 `tl.set(".sX", { opacity: 0 })` |
| 8 | `npm config prefix cannot be changed` | `.npmrc` 冲突 | 直接 `cd` 到项目目录跑 `npx hyperframes render .` |
| 9 | `Docker not available` | 没装 Docker | 别加 `--docker` 参数 |

## 6. 本地化 GSAP（最常见的超时修复）

```bash
curl -sL https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js -o gsap.min.js
# HTML 改成 <script src="gsap.min.js"></script>
```

## 7. `hyperframes.json`（项目配置）

```json
{
  "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
  "registry": "https://raw.githubusercontent.com/heygen-com/hyperframes/main/registry",
  "paths": {
    "blocks": "compositions",
    "components": "compositions/components",
    "assets": "assets"
  }
}
```

## 8. 嵌套 composition（单 timeline 解决不了时）

```html
<div id="root" data-composition-id="main" data-start="0" data-duration="15"
     data-width="1920" data-height="1080">
  <div id="graphics" data-composition-id="main-graphics"
       data-composition-src="compositions/main-graphics.html"
       data-start="0" data-duration="15" data-track-index="1"
       data-width="1920" data-height="1080"></div>
</div>
```

每个嵌套 composition 都要在自己源文件里注册 `__timelines["main-graphics"]`。

## 9. 渲染前自检脚本

```bash
cd <project>

# 三属性
grep -E 'data-composition-id|data-width|data-height' index.html
# __hf
grep -n 'window.__hf' index.html
# __timelines 对象（不是数组）
grep -n 'window.__timelines' index.html
# timeline key 与 composition-id 一致
grep -oP 'data-composition-id="\K[^"]+' index.html
grep -oP '__timelines\["\K[^"]+' index.html
# GSAP 是否本地化
grep -E 'cdnjs|jsdelivr|cloudflare' index.html && echo "WARN: still using CDN"
ls -lh gsap.min.js
```

## 10. 项目结构

```
my-video/
├── index.html          # HTML + CSS + GSAP timeline
├── gsap.min.js         # 本地 GSAP（强烈推荐）
├── hyperframes.json    # 项目配置（可选）
├── meta.json           # { "id", "name", "createdAt" }（可选）
├── compositions/       # 嵌套 composition（可选）
├── assets/             # 字体/图片/视频素材（可选）
└── renders/            # 渲染输出目录（hyperframes 自动生成）
    └── my-video_<timestamp>.mp4
```

## 11. 单位与时长建议

- 单镜建议 2–6s；总时长 ≤ 30s 渲染最稳
- `__hf.duration`（秒） 必须 ≥ timeline 上最后一段 tween 的结束时间
- 颜色用 hex（`#0D1B2A`）；字体优先 system-ui 或本地化的 Google Font

## 12. 链接

- 官方仓库：https://github.com/heygen-com/hyperframes
- schema：https://hyperframes.heygen.com/schema/hyperframes.json
