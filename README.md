# yt-dlp Vercel API

YouTube 媒体提取 API，基于 [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [FastAPI](https://fastapi.tiangolo.com/)，部署在 [Vercel](https://vercel.com) Serverless Functions 上。

## 功能

| 端点 | 说明 |
|------|------|
| `GET /api/info` | 提取视频元信息（标题、时长、可用格式、字幕列表等） |
| `GET /api/subtitles` | 获取字幕内容（支持 vtt/srt/json3 格式） |
| `GET /api/stream` | 获取音频/视频直链 URL（客户端自行下载） |
| `GET /api/download` | 服务端代理下载（默认 worstaudio，无转码） |

## 快速开始

### 部署到 Vercel

1. Fork 或克隆此仓库
2. 在 Vercel 上导入项目
3. 设置环境变量：
   - `API_KEY`：你的 API 密钥（不设置则为公开访问）
4. 部署

### 本地开发

```bash
# 安装依赖
pip install -r requirements.txt

# 使用 FastAPI 开发模式
fastapi dev api/index.py

# 或使用 Vercel CLI 模拟生产环境
vercel dev
```

## API 文档

部署后访问 `/docs` 查看自动生成的 Swagger UI 文档。

### 认证

所有端点需要 API Key，支持三种传递方式：

```bash
# Header
curl -H "X-API-Key: YOUR_KEY" "https://your-app.vercel.app/api/info?url=..."

# Bearer Token
curl -H "Authorization: Bearer YOUR_KEY" "https://your-app.vercel.app/api/info?url=..."

# Query Parameter
curl "https://your-app.vercel.app/api/info?url=...&api_key=YOUR_KEY"
```

### 示例

#### 获取视频信息

```bash
curl "https://your-app.vercel.app/api/info?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ&api_key=YOUR_KEY"
```

#### 获取英文字幕

```bash
curl "https://your-app.vercel.app/api/subtitles?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ&lang=en&format=srt&api_key=YOUR_KEY"
```

#### 获取音频直链

```bash
curl "https://your-app.vercel.app/api/stream?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ&type=audio&quality=worst&api_key=YOUR_KEY"
```

#### 下载音频

```bash
curl -o audio.webm "https://your-app.vercel.app/api/download?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ&format=worstaudio&api_key=YOUR_KEY"
```

## 技术限制

| 限制 | 说明 |
|------|------|
| 响应体大小 | Vercel 限制 4.5MB，大文件请用 `/api/stream` |
| 执行超时 | Hobby 计划最长 5 分钟 |
| 无转码 | 未集成 ffmpeg，返回原始格式（.webm/.m4a/.opus 等） |
| IP 限制 | YouTube 高质量格式可能有 IP 绑定，建议使用 worst 质量 |

## 技术栈

- **Python 3.12** + **FastAPI** + **yt-dlp**
- 部署平台：**Vercel Serverless Functions**
- 无 ffmpeg 依赖
