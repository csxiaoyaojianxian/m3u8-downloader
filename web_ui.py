#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_ui.py — m3u8_dl 的本地 Web 界面 (v2 重构版)

用法:
  python web_ui.py            # 默认 http://127.0.0.1:8765
  python web_ui.py --port 9000
"""

import argparse
import json
import os
import re
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import m3u8_dl

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(TOOL_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# 任务表: id -> {status, done, total, logs[], out_path, error, url, created}
TASKS = {}

# 线程本地日志汇聚: 让 m3u8_dl.log/err 在工作线程里的输出进入对应任务
_tls = threading.local()
_orig_log, _orig_err = m3u8_dl.log, m3u8_dl.err


def _hook_log(msg):
    sink = getattr(_tls, "logs", None)
    if sink is not None:
        sink.append(msg)
    _orig_log(msg)


def _hook_err(msg):
    sink = getattr(_tls, "logs", None)
    if sink is not None:
        sink.append("[!] " + msg)
    _orig_err(msg)


m3u8_dl.log = _hook_log
m3u8_dl.err = _hook_err


def run_detect(url, referer):
    session = m3u8_dl.make_session(referer=referer or None)
    return m3u8_dl.detect_m3u8_detail(url, session)


def title_from_url(url, referer=None):
    """抓取网页 <title> 作为文件名; 失败返回 None"""
    try:
        session = m3u8_dl.make_session(referer=referer or None)
        resp = session.get(url, timeout=10, stream=True)
        resp.raise_for_status()
        text = b""
        for chunk in resp.iter_content(32 * 1024):
            text += chunk
            if len(text) >= 256 * 1024:
                break
        resp.close()
        html = text.decode("utf-8", errors="ignore")
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()
            return title or None
    except Exception:
        pass
    return None


def sanitize_filename(name, ext=".ts"):
    """清洗用户输入的文件名: 去路径分隔符/非法字符, 统一扩展名, 限长"""
    name = (name or "").strip()
    name = os.path.basename(name)  # 防路径穿越
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(". ")
    if len(name) > 120:
        name = name[:120]
    if not name:
        return None
    # 统一扩展名: 用户写了别的后缀会被替换
    stem = os.path.splitext(name)[0] or name
    return stem + ext


def unique_path(dirpath, filename):
    """重名时自动加序号: xxx.ts -> xxx(1).ts"""
    p = os.path.join(dirpath, filename)
    if not os.path.exists(p):
        return p
    stem, ext = os.path.splitext(filename)
    for i in range(1, 999):
        p = os.path.join(dirpath, f"{stem}({i}){ext}")
        if not os.path.exists(p):
            return p
    return p


def run_download(task_id, url, referer, workers, to_mp4, retries, retry_rounds,
                 filename=None):
    task = TASKS[task_id]
    _tls.logs = task["logs"]
    try:
        # 文件名输入为网址时, 抓网页标题作为文件名
        if filename and filename.strip().lower().startswith(("http://", "https://")):
            task["logs"].append("文件名为网址, 抓取网页标题...")
            title = title_from_url(filename, referer)
            if title:
                task["logs"].append(f"标题: {title}")
                filename = title
            else:
                task["logs"].append("标题获取失败, 使用默认文件名")
                filename = None

        if not urlparse(url).path.lower().endswith(".m3u8"):
            task["logs"].append("输入为网页地址, 先自动探测 m3u8...")
            session = m3u8_dl.make_session(referer=referer or None)
            candidates = m3u8_dl.detect_m3u8(url, session)
            if not candidates:
                raise RuntimeError("未探测到 m3u8 地址")
            if len(candidates) > 1:
                task["logs"].append(f"探测到 {len(candidates)} 个地址, 取第 1 个")
            url = candidates[0]

        def progress(done, total):
            task["done"], task["total"] = done, total

        # 文件名 = 最终合并生成的文件名:
        # 始终先合并为 .ts 临时文件, 需要时再无损转封装为目标名(.mp4)
        # 临时目录按任务 ID 隔离, 避免多任务并行时分片互相覆盖
        ext = ".mp4" if to_mp4 else ".ts"
        name = sanitize_filename(filename, ext) or f"{task_id}{ext}"
        stem = os.path.splitext(name)[0]
        merge_path = unique_path(DOWNLOAD_DIR, stem + ".ts")
        task_tmp = os.path.join(DOWNLOAD_DIR, f".tmp_{task_id}")

        m3u8_dl.download_m3u8(url, out_path=merge_path, workers=workers,
                              tmp_dir=task_tmp,
                              to_mp4=False, referer=referer or None,
                              progress=progress, retries=retries,
                              retry_rounds=retry_rounds)

        if to_mp4:
            target = unique_path(DOWNLOAD_DIR, stem + ".mp4")
            try:
                m3u8_dl.ts_to_mp4(merge_path, target, auto_install=False)
                os.remove(merge_path)
                final = target
            except Exception as e:
                task["logs"].append(f"MP4 转封装失败, 保留 TS 文件: {e}")
                final = merge_path
        else:
            final = merge_path
        task["out_path"] = final
        task["status"] = "done"
    except Exception as e:
        task["status"] = "error"
        task["error"] = str(e)


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>M3U8 下载器</title>
<style>
:root {
  --bg:#f4f5f7; --bg2:#e9ebef; --card:#ffffff; --line:#e4e7ec; --line2:#d8dce3;
  --txt:#1a1d21; --txt2:#5c6470; --txt3:#8b93a1;
  --pri:#3b6cf6; --pri2:#2c55d4; --pri-soft:#eaf0ff;
  --ok:#16a34a; --ok-soft:#e8f7ee; --bad:#dc2626; --bad-soft:#fdecec;
  --warn:#d97706; --warn-soft:#fdf3e3;
  --radius:12px; --shadow:0 1px 3px rgba(16,24,40,.06), 0 1px 2px rgba(16,24,40,.04);
  --mono:ui-monospace,Menlo,Consolas,monospace;
}
[data-theme="dark"] {
  --bg:#14161a; --bg2:#1b1e24; --card:#1e2228; --line:#2c313a; --line2:#3a4150;
  --txt:#e8eaed; --txt2:#a0a8b4; --txt3:#6b7480;
  --pri:#5b8bff; --pri2:#7aa0ff; --pri-soft:#233046;
  --ok:#34d399; --ok-soft:#12321f; --bad:#f87171; --bad-soft:#3a1a1a;
  --warn:#fbbf24; --warn-soft:#332a12;
  --shadow:0 1px 3px rgba(0,0,0,.3);
}
* { box-sizing:border-box; }
html { scrollbar-gutter:stable; }
body { margin:0; font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg); color:var(--txt); font-size:14px; line-height:1.5;
  transition:background .25s,color .25s; }
.wrap { max-width:880px; margin:0 auto; padding:0 20px 60px; }

/* ---------- 头部 ---------- */
header { position:sticky; top:0; z-index:50; backdrop-filter:blur(12px);
  background:color-mix(in srgb, var(--bg) 82%, transparent);
  border-bottom:1px solid var(--line); }
.hbar { max-width:880px; margin:0 auto; padding:14px 20px; display:flex;
  align-items:center; gap:12px; }
.logo { width:34px; height:34px; border-radius:9px; flex:none;
  background:linear-gradient(135deg, var(--pri), #7c5cf0);
  display:flex; align-items:center; justify-content:center;
  color:#fff; font-weight:700; font-size:15px; }
.htitle { font-size:17px; font-weight:700; letter-spacing:.2px; }
.hsub { font-size:12px; color:var(--txt3); }
.hspace { flex:1; }
.iconbtn { width:34px; height:34px; border-radius:9px; border:1px solid var(--line);
  background:var(--card); color:var(--txt2); cursor:pointer; font-size:15px;
  display:flex; align-items:center; justify-content:center; transition:all .15s; }
.iconbtn:hover { border-color:var(--pri); color:var(--pri); }

/* ---------- 布局组件 ---------- */
.card { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
  box-shadow:var(--shadow); padding:18px 20px; margin-top:18px; }
.card-title { display:flex; align-items:center; gap:8px; font-size:14px;
  font-weight:700; margin-bottom:14px; }
.card-title .dot { width:8px; height:8px; border-radius:50%; background:var(--pri); }
.card-title .cnt { font-size:12px; color:var(--txt3); font-weight:400; }

.row { display:flex; gap:10px; align-items:flex-end; flex-wrap:wrap; }
.field { flex:1; min-width:150px; }
.field label { display:block; font-size:12px; color:var(--txt2); margin-bottom:5px; }
input[type=text], select { width:100%; padding:9px 12px; border:1px solid var(--line2);
  border-radius:9px; font-size:13.5px; outline:none; background:var(--card);
  color:var(--txt); transition:border-color .15s, box-shadow .15s; }
input[type=text]:focus { border-color:var(--pri);
  box-shadow:0 0 0 3px var(--pri-soft); }
input[type=text]::placeholder { color:var(--txt3); }

.btn { padding:9px 20px; border:none; border-radius:9px; font-size:13.5px;
  font-weight:600; cursor:pointer; background:var(--pri); color:#fff;
  transition:all .15s; white-space:nowrap; }
.btn:hover { background:var(--pri2); }
.btn:disabled { opacity:.45; cursor:not-allowed; }
.btn.ghost { background:transparent; color:var(--txt2); border:1px solid var(--line2); }
.btn.ghost:hover { color:var(--pri); border-color:var(--pri); background:var(--pri-soft); }
.btn.sm { padding:5px 12px; font-size:12.5px; border-radius:7px; }
.btn.danger { background:transparent; color:var(--bad); border:1px solid transparent; }
.btn.danger:hover { background:var(--bad-soft); border-color:var(--bad); }

.badge { display:inline-flex; align-items:center; gap:4px; font-size:11.5px;
  padding:2px 9px; border-radius:99px; background:var(--bg2); color:var(--txt2);
  font-weight:500; white-space:nowrap; }
.badge.ok { background:var(--ok-soft); color:var(--ok); }
.badge.bad { background:var(--bad-soft); color:var(--bad); }
.badge.pri { background:var(--pri-soft); color:var(--pri); }
.badge.warn { background:var(--warn-soft); color:var(--warn); }

/* ---------- 探测结果 ---------- */
.item { display:flex; align-items:center; gap:10px; padding:10px 12px;
  border:1px solid var(--line); border-radius:10px; margin-top:8px;
  transition:border-color .15s, background .15s; }
.item:hover { border-color:var(--pri); background:var(--pri-soft); }
.item .u { flex:1; word-break:break-all; font-size:12.5px; color:var(--txt);
  font-family:var(--mono); }
.item .tags { display:flex; gap:6px; flex:none; }
.empty { text-align:center; color:var(--txt3); font-size:13px; padding:22px 0 14px; }
.spin { display:inline-block; width:13px; height:13px; border:2px solid var(--line2);
  border-top-color:var(--pri); border-radius:50%; animation:sp .7s linear infinite;
  vertical-align:-2px; }
@keyframes sp { to { transform:rotate(360deg); } }

/* ---------- 任务 ---------- */
.task { border:1px solid var(--line); border-radius:10px; margin-top:10px;
  overflow:hidden; }
.task-h { display:flex; align-items:center; gap:10px; padding:10px 14px;
  cursor:pointer; user-select:none; }
.task-h:hover { background:var(--bg2); }
.task-h .u { flex:1; word-break:break-all; font-size:12.5px; color:var(--txt2);
  font-family:var(--mono); }
.task-h .meta { font-size:12px; color:var(--txt3); white-space:nowrap; }
.bar { height:6px; background:var(--bg2); border-radius:99px; overflow:hidden;
  margin:0 14px 10px; }
.bar i { display:block; height:100%; width:0; border-radius:99px;
  background:linear-gradient(90deg, var(--pri), #7c5cf0);
  transition:width .4s ease; }
.task.done .bar i { background:var(--ok); }
.task.error .bar i { background:var(--bad); }
.task-b { display:none; border-top:1px solid var(--line); }
.task.open .task-b { display:block; }
.task .logs { font-family:var(--mono); font-size:11px; color:var(--txt2);
  background:var(--bg2); padding:10px 14px; max-height:170px; overflow-y:auto;
  white-space:pre-wrap; word-break:break-all; }
.task-out { padding:8px 14px; font-size:12px; color:var(--txt2); word-break:break-all; }

/* ---------- 文件库 ---------- */
.file { display:flex; align-items:center; gap:12px; padding:10px 4px;
  border-bottom:1px solid var(--line); }
.file:last-child { border-bottom:none; }
.file .ic { width:34px; height:34px; border-radius:8px; flex:none;
  display:flex; align-items:center; justify-content:center; font-size:12px;
  font-weight:700; }
.file .ic.ts { background:var(--warn-soft); color:var(--warn); }
.file .ic.mp4 { background:var(--ok-soft); color:var(--ok); }
.file .info { flex:1; min-width:0; }
.file .name { font-size:13px; font-weight:600; word-break:break-all; }
.file .sub { font-size:11.5px; color:var(--txt3); }
.file .acts { display:flex; gap:6px; flex:none; }

/* ---------- 拖放区 ---------- */
.dropzone { border:2px dashed var(--line2); border-radius:10px; padding:18px;
  text-align:center; color:var(--txt3); font-size:13px; margin-bottom:6px;
  transition:all .15s; }
.dropzone.drag { border-color:var(--pri); background:var(--pri-soft);
  color:var(--pri); }

/* ---------- Toast ---------- */
#toasts { position:fixed; right:20px; bottom:20px; z-index:100;
  display:flex; flex-direction:column; gap:8px; }
.toast { padding:10px 16px; border-radius:10px; font-size:13px; color:#fff;
  background:#333; box-shadow:0 4px 14px rgba(0,0,0,.22); opacity:0;
  transform:translateY(8px); transition:all .25s; max-width:420px;
  word-break:break-all; }
.toast.show { opacity:1; transform:none; }
.toast.ok { background:var(--ok); } .toast.bad { background:var(--bad); }

.foot { text-align:center; color:var(--txt3); font-size:11.5px; margin-top:28px; }
.foot code { font-family:var(--mono); background:var(--bg2); padding:1px 6px;
  border-radius:5px; }
</style>
</head>
<body>
<header>
  <div class="hbar">
    <div class="logo">M8</div>
    <div>
      <div class="htitle">M3U8 下载器</div>
      <div class="hsub">自动嗅探 · 断点续传 · 无损转封装</div>
    </div>
    <div class="hspace"></div>
    <button class="iconbtn" id="themeBtn" title="切换主题">🌙</button>
  </div>
</header>

<div class="wrap">

  <!-- 探测 & 下载 -->
  <div class="card">
    <div class="card-title"><span class="dot"></span>探测与下载</div>
    <div class="row">
      <div class="field" style="flex:3">
        <label>网页地址 或 m3u8 链接 <span style="color:var(--txt3)">· 粘贴后自动探测</span></label>
        <input type="text" id="url"
          placeholder="https://example.com/video-page 或 .../index.m3u8"
          onkeydown="if(event.key==='Enter')detect()">
      </div>
      <div style="flex:0"><button class="btn" id="btnDetect" onclick="detect()">🔍 探测</button></div>
    </div>
    <div class="row" style="margin-top:12px">
      <div class="field">
        <label>Referer（防盗链）</label>
        <input type="text" id="referer" placeholder="https://example.com/">
      </div>
      <div class="field" style="max-width:80px;flex:0">
        <label>并发</label>
        <input type="text" id="workers" value="15">
      </div>
      <div class="field" style="max-width:80px;flex:0">
        <label>分片重试</label>
        <input type="text" id="retries" value="3">
      </div>
      <div class="field" style="max-width:80px;flex:0">
        <label>重试轮数</label>
        <input type="text" id="retryRounds" value="3">
      </div>
      <div class="field" style="max-width:130px;flex:0">
        <label>&nbsp;</label>
        <label style="font-size:13px;color:var(--txt);display:flex;gap:6px;align-items:center;margin:0;padding:9px 0">
          <input type="checkbox" id="mp4" style="width:15px;height:15px" checked> 转封装 MP4
        </label>
      </div>
    </div>
    <div class="row" style="margin-top:12px">
      <div class="field">
        <label>文件名（可选，输入网址自动取网页标题）</label>
        <input type="text" id="filename" placeholder="默认: 任务ID.ts/mp4；也可直接粘贴网址"
          onblur="resolveTitle()" onkeydown="if(event.key==='Enter')resolveTitle()">
      </div>
      <div style="flex:0"><button class="btn ghost" id="btnTitle" onclick="resolveTitle(true)">取标题</button></div>
    </div>
    <div id="results" style="margin-top:6px"></div>
  </div>

  <!-- 下载任务 -->
  <div class="card" id="taskCard">
    <div class="card-title"><span class="dot"></span>下载任务
      <span class="cnt" id="taskCnt"></span></div>
    <div id="tasks"><div class="empty">暂无任务，探测后点击「下载」开始</div></div>
  </div>

  <!-- 文件库 -->
  <div class="card" id="filesCard">
    <div class="card-title"><span class="dot"></span>文件库
      <span class="cnt" id="fileCnt"></span>
      <div style="flex:1"></div>
      <button class="btn ghost sm" onclick="loadFiles()">刷新</button>
    </div>
    <div class="dropzone" id="dropzone">
      📥 把 .ts 文件拖到这里，自动批量转为 MP4
    </div>
    <div id="files"><div class="empty">downloads 目录为空</div></div>
    <div class="row" style="margin-top:12px">
      <div class="field" style="flex:3">
        <label>转换任意本地 .ts 文件</label>
        <input type="text" id="tsPath" placeholder="/Users/xxx/Movies/video.ts">
      </div>
      <div style="flex:0"><button class="btn ghost" id="btnConvert" onclick="convertCustom()">转 MP4</button></div>
    </div>
  </div>

  <div class="foot">本地服务运行于 <code id="footOrigin"></code> · 文件保存于 <code>downloads/</code></div>
</div>

<div id="toasts"></div>

<script>
'use strict';
const $ = id => document.getElementById(id);
const state = { tasks: {} };   // id -> {lastDone, lastTs, speed}

/* ---------- 主题 ---------- */
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  localStorage.setItem('m3u8-theme', t);
  $('themeBtn').textContent = t === 'dark' ? '☀️' : '🌙';
}
$('themeBtn').onclick = () =>
  setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
setTheme(localStorage.getItem('m3u8-theme') ||
  (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));

/* ---------- Toast ---------- */
function toast(msg, type = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  $('toasts').appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 300); },
    type === 'bad' ? 5000 : 3000);
}

/* ---------- 工具 ---------- */
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtMB(n) { return n >= 1e9 ? (n/1e9).toFixed(2)+' GB' : (n/1e6).toFixed(1)+' MB'; }
function fmtDur(s) {
  if (!s) return '';
  const m = Math.floor(s/60), sec = Math.round(s%60);
  return m >= 1 ? `约${m}分${sec ? sec+'秒' : ''}` : `约${sec}秒`;
}
async function api(path, body) {
  const opt = body
    ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}
    : {};
  const r = await fetch(path, opt);
  return r.json();
}

/* ---------- 探测 ---------- */
async function resolveTitle(force) {
  const input = $('filename');
  const v = input.value.trim();
  if (!v || !v.toLowerCase().startsWith('http')) return;  // 非网址不处理
  const btn = $('btnTitle');
  btn.disabled = true; btn.textContent = '取标题中…';
  try {
    const data = await api('/api/title', {url: v, referer: $('referer').value.trim()});
    if (data.title) {
      input.value = data.title;
      toast('已取标题: ' + data.title, 'ok');
    } else if (force) {
      toast('标题获取失败，可手动输入', 'bad');
    }
  } catch (e) { if (force) toast('标题获取失败: ' + e, 'bad'); }
  finally { btn.disabled = false; btn.textContent = '取标题'; }
}

/* ---------- 地址输入: 规范化 + 粘贴自动探测 ---------- */
const URL_SCHEME_RE = /^[a-z][a-z0-9+.-]*:\/\//i;
const DOMAIN_RE = /^[\w-]+(\.[\w-]+)+(:\d+)?(\/|$)/;
let detectTimer = null;

function normalizeUrl() {
  const input = $('url');
  let v = input.value.trim();
  if (!v) return v;
  if (!URL_SCHEME_RE.test(v) && DOMAIN_RE.test(v)) v = 'https://' + v;
  input.value = v;
  return v;
}
function markInvalid() {
  $('url').style.borderColor = 'var(--bad)';
  setTimeout(() => { $('url').style.borderColor = ''; }, 1600);
}
$('url').addEventListener('paste', () => setTimeout(() => {
  const v = normalizeUrl();
  if (v) {
    clearTimeout(detectTimer);
    detectTimer = setTimeout(detect, 400);   // 防抖: 粘贴后 0.4s 自动探测
  }
}, 60));

/* ---------- 探测 ---------- */
async function detect() {
  const url = normalizeUrl();
  if (!url) { toast('请输入地址', 'bad'); markInvalid(); return; }
  if (!URL_SCHEME_RE.test(url)) { toast('地址格式不对，需要 http(s):// 开头', 'bad'); markInvalid(); return; }
  const referer = $('referer').value.trim();
  const btn = $('btnDetect');
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> 探测中';
  $('results').innerHTML =
    '<div class="empty"><span class="spin"></span> 正在抓取页面并验证候选接口…</div>';
  try {
    const data = await api('/api/detect', {url, referer});
    renderResults(data.results || []);
    if (!data.results || !data.results.length)
      toast('未探测到 m3u8 地址，可尝试补 Referer', 'bad');
    else toast(`探测到 ${data.results.length} 个 m3u8 地址`, 'ok');
  } catch (e) { toast('探测失败: ' + e, 'bad'); }
  finally { btn.disabled = false; btn.textContent = '🔍 探测'; }
}

function renderResults(list) {
  const box = $('results');
  box.innerHTML = '';
  if (!list.length) {
    box.innerHTML = '<div class="empty">未探测到 m3u8 地址</div>';
    return;
  }
  list.forEach((d, i) => {
    const tags = [`<span class="badge ${d.kind==='media'?'ok':d.kind==='master'?'pri':''}">${esc(d.kind)}</span>`];
    if (d.duration) tags.push(`<span class="badge">${fmtDur(d.duration)}</span>`);
    if (d.streams) tags.push(`<span class="badge">${d.streams} 路码率</span>`);
    const div = document.createElement('div');
    div.className = 'item';
    div.innerHTML = `<span class="tags" style="flex:none">${tags.join('')}</span>
      <span class="u">${esc(d.url)}</span>
      <button class="btn sm" onclick="download('${encodeURIComponent(d.url)}')">下载</button>`;
    box.appendChild(div);
  });
}

/* ---------- 下载 ---------- */
async function download(encodedUrl) {
  const url = decodeURIComponent(encodedUrl);
  const toMp4 = $('mp4').checked;
  const filename = $('filename').value.trim();
  const body = {
    url,
    referer: $('referer').value.trim(),
    workers: parseInt($('workers').value) || 8,
    retries: parseInt($('retries').value) || 3,
    retry_rounds: parseInt($('retryRounds').value) || 3,
    to_mp4: toMp4,
    filename,
  };
  try {
    const data = await api('/api/download', body);
    state.tasks[data.task_id] = {lastDone: 0, lastTs: Date.now(), speed: 0};
    toast(`任务已创建: ${data.out_name}`, 'ok');
    renderTaskShell(data.task_id, url, data.out_name);
    poll(data.task_id);
  } catch (e) { toast('创建任务失败: ' + e, 'bad'); }
}

function renderTaskShell(id, url, outName) {
  if ($('task-' + id)) return;
  const empty = $('tasks').querySelector('.empty');
  if (empty) empty.remove();
  const el = document.createElement('div');
  el.className = 'task';
  el.id = 'task-' + id;
  el.innerHTML = `
    <div class="task-h" onclick="this.parentNode.classList.toggle('open')">
      <span class="badge pri" id="st-${id}">排队中</span>
      <span class="u" title="${esc(url)}">${esc(outName || url)}</span>
      <span class="meta" id="meta-${id}"></span>
    </div>
    <div class="bar"><i id="bar-${id}"></i></div>
    <div class="task-b">
      <div class="task-out" id="out-${id}">输出: downloads/${esc(outName || id)}</div>
      <div class="logs" id="log-${id}"></div>
    </div>`;
  $('tasks').prepend(el);
  updateTaskCount();
}

function updateTaskCount() {
  const running = Object.values(state.tasks).length;
  $('taskCnt').textContent = running ? `(${running})` : '';
}

async function poll(id) {
  let lastDone = 0, lastTs = Date.now();
  const timer = setInterval(async () => {
    let t;
    try { t = await api('/api/task/' + id); } catch { return; }
    const st = $('st-' + id), bar = $('bar-' + id), meta = $('meta-' + id);
    if (!st) { clearInterval(timer); return; }
    const pct = t.total ? Math.round(t.done / t.total * 100) : 0;
    bar.style.width = pct + '%';
    const now = Date.now();
    if (t.done > lastDone) {
      state.tasks[id].speed = (t.done - lastDone) / ((now - lastTs) / 1000);
      lastDone = t.done; lastTs = now;
    }
    const taskEl = $('task-' + id);
    if (t.status === 'done') {
      taskEl.classList.add('done');
      st.className = 'badge ok'; st.textContent = '✔ 完成';
      meta.textContent = pct + '%';
      $('out-' + id).innerHTML = '输出: ' + esc(t.out_path) +
        ' <button class="btn ghost sm" onclick="renameTask(\'' + id + '\')">✏️ 重命名</button>';
      clearInterval(timer);
      delete state.tasks[id];
      updateTaskCount(); loadFiles();
      toast('下载完成: ' + (t.out_path||'').split('/').pop(), 'ok');
    } else if (t.status === 'error') {
      taskEl.classList.add('error');
      st.className = 'badge bad'; st.textContent = '✘ 失败';
      meta.textContent = pct + '%';
      $('out-' + id).textContent = '错误: ' + t.error;
      clearInterval(timer);
      delete state.tasks[id];
      updateTaskCount();
      toast('下载失败: ' + (t.error || '').slice(0, 80), 'bad');
    } else {
      st.className = 'badge pri'; st.textContent = '下载中 ' + pct + '%';
      meta.textContent = `${t.done || 0}/${t.total || '?'}` +
        (state.tasks[id].speed ? ` · ${state.tasks[id].speed.toFixed(1)} 片/秒` : '');
    }
    const lg = $('log-' + id);
    if (lg && t.logs) {
      const atBottom = lg.scrollTop + lg.clientHeight >= lg.scrollHeight - 4;
      lg.textContent = t.logs.join('\n');
      if (atBottom) lg.scrollTop = lg.scrollHeight;
    }
  }, 700);
}

/* ---------- 文件库 ---------- */
async function loadFiles() {
  try {
    const data = await api('/api/files');
    const box = $('files');
    box.innerHTML = '';
    const list = data.files || [];
    $('fileCnt').textContent = list.length ? `(${list.length})` : '';
    if (!list.length) {
      box.innerHTML = '<div class="empty">downloads 目录为空</div>';
      return;
    }
    list.forEach(f => {
      const ext = f.name.toLowerCase().endsWith('.mp4') ? 'mp4' : 'ts';
      const div = document.createElement('div');
      div.className = 'file';
      div.innerHTML = `
        <div class="ic ${ext}">${ext.toUpperCase()}</div>
        <div class="info">
          <div class="name">${esc(f.name)}</div>
          <div class="sub">${fmtMB(f.bytes)} · ${new Date(f.mtime * 1000).toLocaleString()}</div>
        </div>
        <div class="acts">
          ${ext === 'ts' ? `<button class="btn ghost sm" onclick="convert('${encodeURIComponent(f.path)}')">转 MP4</button>` : ''}
          <button class="btn ghost sm" onclick="playFile('${encodeURIComponent(f.path)}')">打开</button>
          <button class="btn ghost sm" onclick="renameFile('${encodeURIComponent(f.path)}')">改名</button>
          <button class="btn danger sm" onclick="delFile('${encodeURIComponent(f.path)}')">删除</button>
        </div>`;
      box.appendChild(div);
    });
  } catch (e) { console.error(e); }
}

async function convertCustom() {
  const path = $('tsPath').value.trim();
  if (!path) { toast('请输入 .ts 文件路径', 'bad'); return; }
  await doConvert(path);
}

async function doConvert(path) {
  const btn = $('btnConvert');
  if (btn) { btn.disabled = true; btn.textContent = '转换中…'; }
  toast('转换中，大文件约需十几秒…');
  try {
    const data = await api('/api/convert', {path});
    if (data.out) { toast('转换完成: ' + data.out.split('/').pop(), 'ok'); loadFiles(); }
    else toast('转换失败: ' + (data.error || ''), 'bad');
  } catch (e) { toast('转换失败: ' + e, 'bad'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = '转 MP4'; } }
}

function convert(encodedPath) { doConvert(decodeURIComponent(encodedPath)); }

function playFile(encodedPath) {
  const p = decodeURIComponent(encodedPath);
  const a = document.createElement('a');
  a.href = '/files/' + encodeURIComponent(p.split('/').pop());
  a.target = '_blank'; a.click();
}

async function delFile(encodedPath) {
  const p = decodeURIComponent(encodedPath);
  if (!confirm('确定删除 ' + p.split('/').pop() + ' ?')) return;
  try {
    const data = await api('/api/delete', {path: p});
    if (data.ok) { toast('已删除', 'ok'); loadFiles(); }
    else toast('删除失败: ' + (data.error || ''), 'bad');
  } catch (e) { toast('删除失败: ' + e, 'bad'); }
}

async function renameTask(id) {
  const t = await api('/api/task/' + id);
  const cur = (t.out_path || '').split('/').pop();
  const newName = prompt('新文件名（不含扩展名）:', cur.replace(/\.[^.]+$/, ''));
  if (!newName) return;
  const data = await api('/api/rename', {path: t.out_path, name: newName});
  if (data.ok) {
    toast('已重命名: ' + data.path.split('/').pop(), 'ok');
    $('out-' + id).innerHTML = '输出: ' + esc(data.path) +
      ' <button class="btn ghost sm" onclick="renameTask(\'' + id + '\')">✏️ 重命名</button>';
    loadFiles();
  } else toast('重命名失败: ' + (data.error || ''), 'bad');
}

async function renameFile(encodedPath) {
  const p = decodeURIComponent(encodedPath);
  const newName = prompt('新文件名（不含扩展名）:', p.split('/').pop().replace(/\.[^.]+$/, ''));
  if (!newName) return;
  const data = await api('/api/rename', {path: p, name: newName});
  if (data.ok) { toast('已重命名', 'ok'); loadFiles(); }
  else toast('重命名失败: ' + (data.error || ''), 'bad');
}

/* ---------- 批量拖入转换 ---------- */
const dz = $('dropzone');
['dragenter', 'dragover'].forEach(ev =>
  dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add('drag'); }));
['dragleave', 'drop'].forEach(ev =>
  dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove('drag'); }));
dz.addEventListener('drop', e => handleDropped([...e.dataTransfer.files]));

async function handleDropped(files) {
  const tsFiles = files.filter(f => /\.ts$/i.test(f.name));
  if (!tsFiles.length) { toast('请拖入 .ts 文件', 'bad'); return; }
  toast(`开始批量转换 ${tsFiles.length} 个文件…`);
  let ok = 0, fail = 0;
  for (let i = 0; i < tsFiles.length; i++) {
    const f = tsFiles[i];
    toast(`转换中 (${i + 1}/${tsFiles.length}): ${f.name}`);
    try {
      const r = await fetch('/api/convert-upload', {
        method: 'POST', body: f,
        headers: {'X-Filename': encodeURIComponent(f.name)}
      });
      const d = await r.json();
      if (d.out) ok++;
      else { fail++; toast(`失败: ${f.name} — ${d.error || ''}`, 'bad'); }
    } catch (e) { fail++; toast(`失败: ${f.name} — ${e}`, 'bad'); }
  }
  toast(`批量转换完成: 成功 ${ok}, 失败 ${fail}`, ok ? 'ok' : 'bad');
  loadFiles();
}

/* ---------- 启动 ---------- */
$('footOrigin').textContent = location.origin;
loadFiles();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _in_download_dir(self, path):
        real = os.path.realpath(path)
        return real.startswith(os.path.realpath(DOWNLOAD_DIR) + os.sep)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path.startswith("/api/task/"):
            tid = self.path.rsplit("/", 1)[-1]
            t = TASKS.get(tid, {"status": "unknown"})
            self._send(200, json.dumps(t, ensure_ascii=False))
        elif self.path == "/api/files":
            files = []
            try:
                for name in sorted(os.listdir(DOWNLOAD_DIR)):
                    p = os.path.join(DOWNLOAD_DIR, name)
                    if os.path.isfile(p) and name.lower().endswith((".ts", ".mp4")):
                        st = os.stat(p)
                        files.append({"name": name, "path": p,
                                      "bytes": st.st_size, "mtime": st.st_mtime})
                files.sort(key=lambda f: -f["mtime"])
            except OSError:
                pass
            self._send(200, json.dumps({"files": files}, ensure_ascii=False))
        elif self.path.startswith("/files/"):
            name = os.path.basename(self.path[len("/files/"):])
            p = os.path.join(DOWNLOAD_DIR, name)
            if os.path.isfile(p):
                ctype = "video/mp4" if name.lower().endswith(".mp4") else "video/mp2t"
                with open(p, "rb") as f:
                    self._send(200, f.read(), ctype)
            else:
                self._send(404, '{"error":"not found"}')
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if self.path == "/api/detect":
            b = self._body()
            try:
                results = run_detect(b.get("url", ""), b.get("referer"))
                self._send(200, json.dumps({"results": results}, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"results": [], "error": str(e)},
                                           ensure_ascii=False))
        elif self.path == "/api/download":
            b = self._body()
            tid = uuid.uuid4().hex[:8]
            to_mp4 = bool(b.get("to_mp4"))
            ext = ".mp4" if to_mp4 else ".ts"
            filename = b.get("filename")
            # 文件名是网址: 创建任务时就地取标题, UI 立即显示真实文件名
            if filename and filename.strip().lower().startswith(("http://", "https://")):
                filename = title_from_url(filename, b.get("referer"))
            name = sanitize_filename(filename, ext) or f"{tid}{ext}"
            TASKS[tid] = {"status": "running", "done": 0, "total": 0,
                          "logs": [], "url": b.get("url"), "out_path": None,
                          "error": None, "created": time.time(),
                          "out_name": name}
            threading.Thread(
                target=run_download,
                args=(tid, b.get("url"), b.get("referer"),
                      int(b.get("workers") or 8), to_mp4,
                      int(b.get("retries") or 3), int(b.get("retry_rounds") or 3),
                      filename),
                daemon=True).start()
            self._send(200, json.dumps({"task_id": tid, "out_name": name}))
        elif self.path == "/api/convert":
            b = self._body()
            try:
                out = m3u8_dl.ts_to_mp4(b.get("path", ""), auto_install=False)
                self._send(200, json.dumps({"out": out}, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False))
        elif self.path == "/api/title":
            b = self._body()
            title = title_from_url(b.get("url", ""), b.get("referer"))
            self._send(200, json.dumps({"title": title}, ensure_ascii=False))
        elif self.path == "/api/rename":
            b = self._body()
            old = b.get("path", "")
            if not self._in_download_dir(old):
                self._send(200, json.dumps({"error": "只允许重命名 downloads 目录内的文件"}))
                return
            new_name = sanitize_filename(b.get("name"),
                                         os.path.splitext(old)[1])
            if not new_name:
                self._send(200, json.dumps({"error": "文件名无效"}))
                return
            target = unique_path(DOWNLOAD_DIR, new_name)
            try:
                os.rename(old, target)
                self._send(200, json.dumps({"ok": True, "path": target},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False))
        elif self.path == "/api/convert-upload":
            # 批量拖入: 浏览器拿不到本地路径, 由前端上传文件字节再转换
            raw_name = urllib.parse.unquote(
                self.headers.get("X-Filename", "upload.ts"))
            safe = sanitize_filename(os.path.basename(raw_name), ".ts")
            if not safe:
                self._send(200, json.dumps({"error": "文件名无效"}))
                return
            save_path = unique_path(DOWNLOAD_DIR, safe)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                with open(save_path, "wb") as f:
                    remain = n
                    while remain > 0:
                        chunk = self.rfile.read(min(1024 * 1024, remain))
                        if not chunk:
                            break
                        f.write(chunk)
                        remain -= len(chunk)
                out = m3u8_dl.ts_to_mp4(save_path, auto_install=False)
                os.remove(save_path)
                self._send(200, json.dumps({"out": out}, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"error": str(e), "ts": save_path},
                                           ensure_ascii=False))
        elif self.path == "/api/delete":
            b = self._body()
            path = b.get("path", "")
            if not self._in_download_dir(path):
                self._send(200, json.dumps({"error": "只允许删除 downloads 目录内的文件"}))
                return
            try:
                os.remove(path)
                self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                self._send(200, json.dumps({"error": str(e)}, ensure_ascii=False))
        else:
            self._send(404, '{"error":"not found"}')

    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="M3U8 下载器 Web 界面")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"M3U8 下载器界面已启动: http://{args.host}:{args.port}")
    print(f"下载文件保存目录: {DOWNLOAD_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
