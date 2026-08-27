在 Vercel Serverless 环境中，由于磁盘空间（`/tmp` 最大 512MB）和内存（默认 1024MB）的严格限制，**“流式上传（Streaming Upload）”** 是处理媒体文件的唯一正确姿势。

其核心原理是：利用 `yt-dlp` 获取 YouTube 原生媒体的直链（Direct URL），然后发起 HTTP GET 请求但不一次性读取响应体，而是建立一个**数据管道**。一边从 YouTube CDN 接收数据块（Chunk），一边使用 AWS S3 SDK（兼容 R2）将数据分片实时推送到 Cloudflare R2。整个过程内存占用通常在十几兆以内。

以下是完整的最佳实践方案。

### 1. 更新依赖 `requirements.txt`

你需要增加 `boto3`（AWS 官方 SDK，完美兼容 R2）和 `requests`（处理流式下载最稳定的库）：

```text
fastapi>=0.110.0
yt-dlp>=2025.1.1
quickjs>=1.19.4
boto3>=1.34.0
requests>=2.31.0

```

### 2. 核心代码实现

在你的 `api/index.py` 中，编写如下流式上传逻辑：

```python
import os
import boto3
import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from boto3.s3.transfer import TransferConfig

app = FastAPI()

# 初始化 R2 客户端 (S3 兼容)
# 请在 Vercel Settings -> Environment Variables 中配置这些变量
s3_client = boto3.client(
    's3',
    endpoint_url=f"https://{os.environ.get('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY'),
    aws_secret_access_key=os.environ.get('R2_SECRET_KEY'),
    region_name='auto' # R2 默认 region 为 auto
)

R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME')

@app.post("/api/upload-audio")
async def extract_and_upload(url: str = Query(..., description="YouTube 视频链接")):
    # 1. 配置 yt-dlp 仅提取元数据和直链
    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        # 'proxy': os.environ.get("YTDL_PROXY"), # 生产环境强烈建议开启代理
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            audio_url = info.get("url")
            ext = info.get("ext", "m4a")
            video_id = info.get("id")
            
            if not audio_url:
                raise HTTPException(status_code=400, detail="无法获取媒体直链")
                
            object_key = f"audio/{video_id}.{ext}"

            # 2. 建立流式数据管道
            # 使用 stream=True，请求不会将整个文件下载到内存
            with requests.get(audio_url, stream=True, timeout=15) as r:
                r.raise_for_status()
                
                # 配置文件流的分片上传策略（关键）
                # 这里设置以 8MB 为一个块 (Chunk) 边下边传
                config = TransferConfig(
                    multipart_threshold=8 * 1024 * 1024,
                    max_concurrency=2, # 限制并发线程防止 Vercel CPU 爆满
                    multipart_chunksize=8 * 1024 * 1024,
                    use_threads=True
                )
                
                # r.raw 提供了类似底层 socket 文件的接口，boto3 会自动分块读取并上传
                s3_client.upload_fileobj(
                    Fileobj=r.raw,
                    Bucket=R2_BUCKET_NAME,
                    Key=object_key,
                    Config=config,
                    ExtraArgs={'ContentType': 'audio/mp4' if ext == 'm4a' else 'audio/webm'}
                )

        return {
            "status": "success",
            "video_id": video_id,
            "r2_path": object_key,
            "message": "音频已成功流式传输至 R2"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

```

---

## 3. 部署时必须注意的“生死线”：超时时间

这个方案在技术上完全跑得通，但你必须面对 Serverless 架构最大的限制——**执行时长限制 (Max Duration)**。

流式传输的速度取决于 Vercel 节点到 YouTube 的拉取速度，以及推送到 R2 的上传速度。如果你提取的是一首 3 分钟的歌（约 3-5 MB），整个过程几秒钟就能跑完；但如果是 1 小时的播客（约 50-60 MB），传输可能需要 15-30 秒。

**Vercel 函数默认超时时间非常短：**

* **Hobby (免费版):** 默认 10 秒，最多可调至 **60 秒**。
* **Pro (付费版):** 默认 15 秒，最多可调至 **300 秒** (甚至 900 秒)。

**你必须在项目根目录新建 `vercel.json` 延长超时时间，否则大概率会在中途被强制掐断：**

```json
{
  "functions": {
    "api/index.py": {
      "maxDuration": 60 
    }
  }
}

```

> **架构建议：** 如果你的目标是下载几十小时的有声书或超长播客，Vercel Serverless 会因为超时限制直接失败。这种情况就必须退回到我们之前提到的方案三：让 Vercel 仅获取 `audio_url` 的直链，并把直链扔给外部的独立 Worker（如运行在 Fly.io 上的持久化 Docker 容器）去慢慢下载并上传到 R2。
