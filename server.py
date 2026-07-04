# -*- coding: utf-8 -*-
"""
Coze API 代理服务器
- 提供静态文件服务（HTML/JS/CSS）
- 代理 /coze-api/* 请求到 https://api.coze.cn/*，解决浏览器 CORS 限制
- 支持 SSE 流式响应
- 支持文件上传代理（multipart/form-data）
- 支持 Edge TTS 代理（POST /tts 文本 → 音频流）
"""

# 云端环境（Railway / Hugging Face Spaces）不设 HF 镜像，直接用 huggingface.co
# HF Spaces 设置 SPACE_ID 环境变量
import os
_is_cloud = bool(
    os.environ.get('RAILWAY_ENVIRONMENT') or
    os.environ.get('SPACE_ID') or
    os.environ.get('RENDER')
)
if not _is_cloud:
    os.environ.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')
    os.environ.setdefault('HF_HUB_DISABLE_XET', '1')
os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')

import http.server
import http.client
import socketserver
import ssl
import os
import re
import sys
import socket
import json
import time
import hmac
import hashlib
import base64
import uuid
import datetime
import urllib.parse
from http.server import SimpleHTTPRequestHandler
from socketserver import TCPServer, ThreadingMixIn

# 多线程 HTTPServer
class ThreadingHTTPServer(ThreadingMixIn, TCPServer):
    daemon_threads = True
    allow_reuse_address = True
from http.server import SimpleHTTPRequestHandler
from socketserver import TCPServer, ThreadingMixIn

# 同步 WebSocket 客户端（Edge TTS 必需）
try:
    import websocket
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False
    print('[WARN] websocket-client 库未安装，Edge TTS 不可用。请运行: pip install websocket-client', file=sys.stderr, flush=True)

PORT = int(os.environ.get('PORT', 9876))
COZE_HOST = 'api.coze.cn'
AMAP_HOST = 'restapi.amap.com'  # 高德 Web 服务 API

class CozeProxyHandler(http.server.SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.path.dirname(os.path.abspath(__file__)), **kwargs)

    # ===== 静态文件响应添加 CORS 头 =====
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    # ===== 代理 Coze API =====
    def do_POST(self):
        if self.path.startswith('/coze-api/'):
            self._proxy('POST', COZE_HOST)
        elif self.path.startswith('/amap-api/'):
            self._proxy('POST', AMAP_HOST)
        elif self.path == '/tts' or self.path.startswith('/tts?'):
            self._handle_tts()
        elif self.path == '/stt' or self.path.startswith('/stt?'):
            self._handle_stt()
        else:
            self.send_error(404)

    def do_GET(self):
        if self.path == '/health':
            # 健康检查端点 — 返回 ok
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            payload = b'{"ok":true,"server":"coze-amap-proxy","port":' + str(PORT).encode() + b'}'
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith('/coze-api/'):
            self._proxy('GET', COZE_HOST)
        elif self.path.startswith('/amap-api/'):
            self._proxy('GET', AMAP_HOST)
        else:
            super().do_GET()
    # ===== Edge TTS 代理 =====
    # POST /tts  body: {"text": "...", "voice": "zh-CN-XiaoxiaoNeural"}
    # 返回音频流（audio/mpeg）
    def _handle_tts(self):
        if not HAS_WEBSOCKETS:
            self.send_error(501, 'Edge TTS not available: install websocket-client')
            return
        # 读取 body
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length) if length > 0 else b'{}'
            data = json.loads(raw.decode('utf-8'))
        except Exception as e:
            self.send_response(400)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'JSON parse error')
            return
        text = (data.get('text') or '').strip()
        voice = data.get('voice') or 'zh-CN-XiaoxiaoNeural'
        rate = data.get('rate') or '+0%'
        volume = data.get('volume') or '+0%'
        pitch = data.get('pitch') or '+0Hz'
        if not text:
            self.send_response(400)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'text is empty')
            return
        if len(text) > 5000:
            text = text[:5000]
        try:
            audio_bytes = _edge_tts_sync(text, voice, rate, volume, pitch)
        except Exception as e:
            print('[TTS] Error: ' + repr(e), file=sys.stderr, flush=True)
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            try:
                self.wfile.write(('TTS error: ' + repr(e)).encode('utf-8'))
            except Exception:
                pass
            return
        if not audio_bytes:
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'TTS returned empty audio')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Content-Length', str(len(audio_bytes)))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        try:
            self.wfile.write(audio_bytes)
        except (BrokenPipeError, ConnectionResetError):
            pass
        print('[TTS] OK ' + str(len(audio_bytes)) + ' bytes, voice=' + voice + ', text=' + text[:30], file=sys.stderr, flush=True)

    def _proxy(self, method, target_host):
        if target_host == COZE_HOST:
            target_path = self.path[len('/coze-api'):]
        elif target_host == AMAP_HOST:
            target_path = self.path[len('/amap-api'):]
        else:
            target_path = self.path

        # 关键修复：Python 3 的 http.client 用 ASCII 编码 URL 路径，
        # 当中文关键字出现在 query string 中时会报 'ascii' codec 错误
        # 解决：把 path 转成 latin-1（HTTP header 兼容）后传给底层 socket
        # 最稳妥的做法：手动解析 query 并用 urllib.parse.quote 重新组装
        from urllib.parse import urlsplit, urlunsplit, quote
        try:
            parts = urlsplit(target_path)
            # 把 netloc/path 部分用 latin-1 编码（HTTP header 安全）
            safe_path = quote(parts.path, safe='/?&=:%')
            # query 中可能含中文，重新 quote（保留必要的 & =）
            safe_query = quote(parts.query, safe='&=%')
            target_path = safe_path + ('?' + safe_query if safe_query else '')
        except Exception:
            pass

        # 读取请求体
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        # 转发请求头（排除 hop-by-hop 头，但保留 Content-Type 和 Content-Length）
        headers = {}
        for key, val in self.headers.items():
            lk = key.lower()
            if lk in ('host', 'transfer-encoding', 'connection', 'accept-encoding', 'origin', 'referer'):
                continue
            headers[key] = val

        # 设置正确的 Host 头
        headers['Host'] = target_host

        # 特殊处理：高德 IP 定位 API — 自动注入客户端真实公网 IP
        # 高德的 /v3/ip 默认用调用方的 IP，但这里调用方是本机 (127.0.0.1)，需要替换
        if target_host == AMAP_HOST and target_path.startswith('/v3/ip'):
            # 提取真实客户端 IP（按优先级）
            client_ip = (
                self.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
                self.headers.get('X-Real-IP', '').strip() or
                self.headers.get('CF-Connecting-IP', '').strip() or
                self.client_address[0]
            )
            if client_ip:
                # 在 query string 中加入 ip 参数（覆盖高德默认使用调用方IP的逻辑）
                if 'ip=' in target_path:
                    target_path = re.sub(r'ip=[^&]*', 'ip=' + client_ip, target_path)
                else:
                    sep = '&' if '?' in target_path else '?'
                    target_path = target_path + sep + 'ip=' + client_ip
                # 输出到 stderr 方便调试
                import sys
                print(f'[高德IP] client={client_ip}', file=sys.stderr, flush=True)

        # 文件上传需要更长的超时
        timeout = 120 if not target_path.startswith('/v1/files') else 300

        try:
            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(target_host, context=ctx, timeout=timeout)
            conn.request(method, target_path, body=body, headers=headers)
            resp = conn.getresponse()

            # 发送响应状态和头
            self.send_response(resp.status)
            for key, val in resp.getheaders():
                lk = key.lower()
                if lk in ('transfer-encoding', 'connection', 'content-encoding', 'server'):
                    continue
                self.send_header(key, val)
            self.end_headers()

            # 流式转发响应体
            try:
                self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass

            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break

            conn.close()

        except http.client.HTTPException as e:
            try:
                self.send_error(502, 'Bad Gateway: ' + str(e))
            except Exception:
                pass
        except Exception as e:
            try:
                self.send_error(500, 'Proxy Error: ' + str(e))
            except Exception:
                pass


    # ===== STT 代理（语音转文字，使用 faster-whisper 本地识别） =====
    # POST /stt   body: multipart/form-data, field "audio" (webm/wav/mp3/...)
    # 返回 JSON {"text": "..."}
    def _handle_stt(self):
        import tempfile
        try:
            length = int(self.headers.get('Content-Length', 0))
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self.send_response(400)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(b'Content-Type must be multipart/form-data')
                return
            # 解析 multipart（Python 3.13 已移除 cgi 模块，手动解析）
            raw_body = self.rfile.read(length) if length > 0 else b''
            # 提取 boundary
            boundary = None
            for part in content_type.split(';'):
                part = part.strip()
                if part.startswith('boundary='):
                    boundary = part[len('boundary='):]
                    if boundary.startswith('"') and boundary.endswith('"'):
                        boundary = boundary[1:-1]
                    break
            if not boundary:
                self.send_response(400)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(b'missing boundary')
                return
            # 按 boundary 分割
            delim = b'--' + boundary.encode()
            segments = raw_body.split(delim)
            audio_data = None
            audio_filename = 'voice.webm'
            for seg in segments:
                seg = seg.strip()
                if not seg or seg == b'--' or seg == b'':
                    continue
                # 分离 header 和 body
                if b'\r\n\r\n' in seg:
                    header_block, file_data = seg.split(b'\r\n\r\n', 1)
                elif b'\n\n' in seg:
                    header_block, file_data = seg.split(b'\n\n', 1)
                else:
                    continue
                # 解析 Content-Disposition
                cd = ''
                for line in header_block.split(b'\r\n'):
                    if line.lower().startswith(b'content-disposition:'):
                        cd = line.decode('utf-8', errors='replace')
                        break
                if 'name="audio"' not in cd:
                    continue
                # 提取 filename
                for item in cd.split(';'):
                    item = item.strip()
                    if item.startswith('filename='):
                        fn = item[9:].strip('"')
                        if fn:
                            audio_filename = fn
                # 去掉尾部 \r\n
                audio_data = file_data.rstrip(b'\r\n')
                break
            if not audio_data:
                self.send_response(400)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(b'missing field "audio"')
                return
            # 写入临时文件
            ext = audio_filename.rsplit('.', 1)[-1] if '.' in audio_filename else 'webm'
            tmpf = tempfile.NamedTemporaryFile(delete=False, suffix='.' + ext)
            try:
                tmpf.write(audio_data)
                tmpf.close()
                tmp_path = tmpf.name
            except Exception as e:
                try: os.unlink(tmpf.name)
                except Exception: pass
                raise e
        except Exception as e:
            self.send_response(400)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(('STT parse error: ' + repr(e)).encode('utf-8'))
            return

        # 调 whisper
        try:
            text = _stt_transcribe(tmp_path)
        except ImportError as e:
            self.send_response(501)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'whisper_not_installed', 'detail': str(e)}).encode('utf-8'))
            try: os.unlink(tmp_path)
            except Exception: pass
            return
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'stt_failed', 'detail': repr(e)}).encode('utf-8'))
            try: os.unlink(tmp_path)
            except Exception: pass
            return
        try: os.unlink(tmp_path)
        except Exception: pass

        # 成功
        out = json.dumps({'text': text or ''}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(out)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(out)
        print('[STT] OK "' + (text or '')[:50] + '"', file=sys.stderr, flush=True)


if __name__ == '__main__':

    # ===== Edge TTS 核心（用官方 edge-tts 库） =====
    import edge_tts

    def _edge_tts_sync(text, voice='zh-CN-XiaoxiaoNeural', rate='+0%', volume='+0%', pitch='+0Hz'):
        """同步调用 Edge TTS，返回 MP3 字节数组（用 edge-tts 库 + asyncio.run）。"""
        import asyncio
        import io

        # 1) 查缓存
        cache_key = (text, voice, rate, volume, pitch)
        cached = _tts_cache.get(cache_key)
        if cached and cached[1] > time.time():
            return cached[0]

        # 2) 调微软
        async def _do():
            comm = edge_tts.Communicate(
                text=text,
                voice=voice,
                rate=rate,
                volume=volume,
                pitch=pitch,
            )
            buf = io.BytesIO()
            async for chunk in comm.stream():
                if chunk['type'] == 'audio':
                    buf.write(chunk['data'])
            return buf.getvalue()

        # 失败重试 1 次（处理 ConnectionReset 微软限流）
        last_err = None
        for attempt in range(2):
            try:
                result = asyncio.run(_do())
                if result:
                    # 写缓存
                    _tts_cache[cache_key] = (result, time.time() + _TTS_CACHE_TTL)
                    # 限制缓存大小（防内存爆炸）
                    if len(_tts_cache) > 200:
                        # 清掉最早的 50 个
                        for k in list(_tts_cache.keys())[:50]:
                            del _tts_cache[k]
                return result
            except Exception as e:
                last_err = e
                time.sleep(0.5)
        raise last_err

    # ===== STT 核心（用 faster-whisper 本地识别） =====
    _whisper_model = None
    def _stt_transcribe(audio_path, lang='zh'):
        """用 faster-whisper 把音频转成文字（首次加载模型较慢）。"""
        global _whisper_model
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise ImportError('请运行: pip install faster-whisper')
        if _whisper_model is None:
            # 优先从本地目录加载模型（避免 Railway 下载失败）
            _local_model = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models', 'faster-whisper-tiny')
            if os.path.isdir(_local_model) and os.path.isfile(os.path.join(_local_model, 'model.bin')):
                print(f'[STT] 从本地加载 whisper tiny 模型: {_local_model}', flush=True)
                _whisper_model = WhisperModel(_local_model, device='cpu', compute_type='int8')
            else:
                print('[STT] 本地模型未找到，尝试在线下载 tiny 模型…', flush=True)
                _whisper_model = WhisperModel('tiny', device='cpu', compute_type='int8')
        segments, info = _whisper_model.transcribe(audio_path, language=lang, beam_size=5)
        text = ''.join(seg.text for seg in segments).strip()
        return text

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    with ThreadingHTTPServer(('0.0.0.0', PORT), CozeProxyHandler) as httpd:
        # 设置超时，防止连接卡死
        httpd.timeout = 120
        print('========================================')
        print('  Server: http://localhost:' + str(PORT))
        print('  Coze API proxy: /coze-api/* -> api.coze.cn')
        print('  AMap API proxy: /amap-api/* -> restapi.amap.com')
        print('  Edge TTS proxy: POST /tts  -> edge-tts')
        print('  Press Ctrl+C to stop')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nServer stopped')
