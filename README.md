# M3U8 自动嗅探下载器

一个本地运行的 M3U8 视频探测与下载工具：粘贴视频页地址自动嗅探 m3u8，支持 AES-128 解密、断点续传式分片重试、无损转封装 MP4，带本地 Web 界面。

> 仅供个人学习研究，请勿用于下载受版权保护的内容。

## 特性

- **内容验证式嗅探**：静态 HTML 正则 + 候选接口逐个验证（响应体 `#EXTM3U` 开头即命中），支持无后缀动态接口、JSON 响应内路径两层递归
- **断点续传式重试**：分片流式写入 `.part`，断连后带 `Range` 头从断点续传；每次重试换全新连接；无进展自动丢弃重下
- **多轮重试**：单分片重试次数 + 失败分片整体重扫轮数均可配置
- **fMP4 支持**：自动识别 `EXT-X-MAP` init 段并拼接文件头
- **AES-128 解密**：自动获取密钥与 IV
- **无损转封装 MP4**：`ffmpeg -c copy` 秒级完成，无需重编码；ffmpeg 可自动获取（优先系统 PATH → 本地 bin → Homebrew → pip 静态包）
- **本地 Web 界面**：亮/暗主题、多任务并行、实时进度与速度、文件库管理（转 MP4 / 重命名 / 删除）、批量拖入 `.ts` 转 MP4、粘贴网址自动探测、网址自动取网页标题

## 安装

```bash
git clone https://github.com/csxiaoyaojianxian/m3u8-downloader.git
cd m3u8-downloader
pip3 install -r requirements.txt
```

## 使用

### Web 界面（推荐）

```bash
python3 web_ui.py            # 打开 http://127.0.0.1:8765
```

粘贴视频页或 m3u8 地址 → 自动探测 → 选择结果下载。典型流程全程只需两次点击。

### 命令行

```bash
# 探测（显示类型与时长）
python3 m3u8_dl.py detect "https://example.com/video-page"

# 下载（网页地址自动探测）
python3 m3u8_dl.py download "https://example.com/video-page" -o 视频.mp4 --mp4

# 直接下载 m3u8，15 并发，带 Referer 防盗链
python3 m3u8_dl.py download "https://cdn.example.com/index.m3u8" -w 15 --referer "https://example.com/"

# 已有 ts 转 mp4（无损转封装）
python3 m3u8_dl.py convert video.ts -o video.mp4
```

### 常用参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `-w` | 并发下载数 | 8 |
| `--retries` | 单分片单轮重试次数 | 3 |
| `--retry-rounds` | 失败分片整体重试轮数 | 3 |
| `--mp4` | 完成后转封装为 mp4 | 关 |
| `--referer` / `-H` | 防盗链 / 自定义请求头 | 无 |
| `--keep-tmp` | 保留分片（用于续传） | 关 |

## 测试

`tests/` 内置三个模拟服务器，用于回归下载内核的健壮性：

```bash
python3 tests/flaky_server.py        # 固定位置掐断连接 + 支持 Range 续传 (127.0.0.1:18766)
python3 tests/norange_server.py      # 不支持 Range + 固定断连 (127.0.0.1:18769)
python3 tests/dynamic_site_server.py # 无后缀动态接口视频站 (127.0.0.1:18768)
```

## 目录结构

```
m3u8_dl.py            # 核心嗅探/下载/转换引擎（CLI 入口）
web_ui.py             # Web 界面（纯标准库，无前端框架）
tests/                # 模拟服务器（回归测试）
bin/ffmpeg            # 运行时自动获取的静态 ffmpeg（不入库）
downloads/            # 默认下载目录（不入库）
```

## License

MIT
