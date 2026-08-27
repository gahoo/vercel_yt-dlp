# yt-dlp Vercel API

YouTube 媒体提取与语音转录 API，基于 [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [FastAPI](https://fastapi.tiangolo.com/)，部署在 [Vercel](https://vercel.com) Serverless Functions 上。

支持流式上传至 **Cloudflare R2**（绕过 Vercel 4.5MB 响应体限制）与通过 **Groq Whisper** 快速进行语音文本转录。

---

## 功能端点

| 端点 | 说明 |
|------|------|
| `GET /api/info` | 提取视频元信息（标题、时长、可用格式、字幕列表等） |
| `GET /api/subtitles` | 获取视频字幕内容（支持 vtt/srt/json3 格式） |
| `GET /api/stream` | 提取音视频直链 URL（供客户端自行播放/下载） |
| `GET /api/download` | 服务端下载音频并流式存入 Cloudflare R2，返回 1 小时有效的预签名下载链接 |
| `GET /api/transcribe` | 调用 Groq Whisper 模型进行语音转录，支持传入 R2 文件 `key` 或音频 `url` |

---

## 环境变量配置

在 Vercel 项目设置（**Settings -> Environment Variables**）或本地 `.env` 文件中配置以下变量：

| 变量名 | 必填 | 说明 |
|---|---|---|
| `API_KEY` | 否 | API 访问认证密钥。不设置时无需认证直接公开访问 |
| `R2_ACCOUNT_ID` | 是（用于 download/transcribe） | Cloudflare Account ID |
| `R2_ACCESS_KEY` | 是（用于 download/transcribe） | Cloudflare R2 API Token Access Key |
| `R2_SECRET_KEY` | 是（用于 download/transcribe） | Cloudflare R2 API Token Secret Key |
| `R2_BUCKET_NAME` | 是（用于 download/transcribe） | 存储音频的 Cloudflare R2 存储桶名称 |
| `GROQ_API_KEY` | 是（用于 transcribe） | Groq API Key（用于 Whisper 语音转录） |

---

## 快速开始

### 本地开发

```bash
# 1. 创建虚拟环境并安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env # 填入你的 R2、Groq 和 API Key

# 3. 启动本地开发服务
uvicorn main:app --reload --port 8000
```

本地服务启动后，在浏览器访问 `http://127.0.0.1:8000/docs` 查看 Swagger 交互式文档。

---

## 认证方式

若设置了 `API_KEY`，请求接口时支持以下三种传递方式：

```bash
# 1. Header
curl -H "X-API-Key: YOUR_KEY" "https://your-app.vercel.app/api/info?url=..."

# 2. Bearer Token
curl -H "Authorization: Bearer YOUR_KEY" "https://your-app.vercel.app/api/info?url=..."

# 3. Query Parameter
curl "https://your-app.vercel.app/api/info?url=...&api_key=YOUR_KEY"
```

---

## API 调用示例

### 1. 获取视频信息 (`/api/info`)

```bash
curl "https://your-app.vercel.app/api/info?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ&api_key=YOUR_KEY"
```

---

### 2. 获取英文字幕 (`/api/subtitles`)

```bash
curl "https://your-app.vercel.app/api/subtitles?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ&lang=en&format=srt&api_key=YOUR_KEY"
```

---

### 3. 获取音频直链 (`/api/stream`)

```bash
curl "https://your-app.vercel.app/api/stream?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ&type=audio&quality=worst&api_key=YOUR_KEY"
```

---

### 4. 下载音频并存储至 R2 (`/api/download`)

服务端获取音频流并转存至 Cloudflare R2。如果该视频已存在于 R2，将命中缓存直接返回链接：

```bash
curl "https://your-app.vercel.app/api/download?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ&api_key=YOUR_KEY"
```

**响应示例：**
```json
{
  "status": "uploaded",
  "key": "audio/dQw4w9WgXcQ.m4a",
  "url": "https://your-bucket.r2.cloudflarestorage.com/audio/dQw4w9WgXcQ.m4a?X-Amz-Algorithm=..."
}
```

---

### 5. 语音转录 (`/api/transcribe`)

支持两种转录模式：

#### 模式 A：根据 R2 的对象 `key` 转录（推荐，配合 `/api/download` 使用）

将 `/api/download` 返回的 `key` 传入进行转录：

```bash
curl "https://your-app.vercel.app/api/transcribe?key=audio/dQw4w9WgXcQ.m4a&api_key=YOUR_KEY"
```

#### 模式 B：根据公网音频 `url` 转录

直接传入任意可公开访问的音频直链：

```bash
curl "https://your-app.vercel.app/api/transcribe?url=https://example.com/audio.mp3&api_key=YOUR_KEY"
```

#### 可选参数：
- `model`：Groq Whisper 模型名称（默认：`whisper-large-v3`，可选 `whisper-large-v3-turbo` 等）

**响应示例：**
```json
{
  "text": "We're no strangers to love You know the rules and so do I...",
  "x_groq": {
    "id": "req_01m11799bge9eshq5k9tg1fq2t"
  }
}
```

---

## 典型工作流：从 YouTube 视频到文本转录

```text
1. 客户端 -> GET /api/download?url=https://youtube.com/watch?v=xxx
   └─> 服务端抓取音频 -> 存入 R2 -> 返回 key: "audio/xxx.m4a"

2. 客户端 -> GET /api/transcribe?key=audio/xxx.m4a
   └─> 服务端从 R2 流式管道推送至 Groq Whisper API -> 返回识别后的纯文本
```

---

## 技术限制与说明

| 项目 | 说明 |
|---|---|
| **执行超时** | Vercel Hobby 计划配置了 `maxDuration: 300`（最长 5 分钟） |
| **内存与响应大小** | 音频流式转存 R2 规避了 Vercel 的 4.5MB 响应体限制；转录过程采用分块流式转发，内存占用极低 |
| **R2 生命周期** | 建议在 Cloudflare R2 控制台为存储桶设置 1 天生命周期规则（Lifecycle Rule），自动清理历史音频 |
| **依赖环境** | 使用 `quickjs` 引擎处理 YouTube 签名解析，无需额外安装 Node.js/Deno 或 ffmpeg |
