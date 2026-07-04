# Hugging Face Spaces 部署指南

## 一、创建 HF Space

1. 打开 https://huggingface.co/login 登录（没账号就注册一个，免费）
2. 点击右上角头像 → **New Space**
3. 填写信息：
   - **Name**: `ai-guide`（或你喜欢的名字）
   - **License**: MIT
   - **SDK**: 选择 **Docker**
   - **Visibility**: Public（免费版必须 Public）
4. 点击 **Create Space**

## 二、上传代码（两种方式，任选一种）

### 方式 A：用 Git 命令行（推荐）

```bash
# 1. 进入项目目录
cd C:\Users\ROOT\WorkBuddy\20260702123126

# 2. 添加 HF 远程仓库（把 你的用户名 换成你的 HF 用户名）
git remote add hf https://huggingface.co/spaces/你的用户名/ai-guide

# 3. 推送到 HF（会要求输入 HF 账号密码）
git push hf main
```

> HF 密码就是你的 Access Token，在 https://huggingface.co/settings/tokens 创建，选 **Write** 权限。

### 方式 B：网页上传

1. 打开你创建的 Space 页面
2. 点击 **Files** → **Add file** → 逐个上传以下文件：
   - `Dockerfile`
   - `README.md`
   - `server.py`
   - `index.html`
   - `navigation.html`
   - `requirements.txt`
   - `.dockerignore`
   - `models/faster-whisper-tiny/` 目录下全部 4 个文件（config.json, model.bin, tokenizer.json, vocabulary.txt）

## 三、等待构建

- 推送/上传完成后，HF 会自动构建 Docker 镜像
- 构建过程：拉取 Python 镜像 → 安装依赖 → 复制文件 → 启动
- 首次构建约 3-5 分钟（主要时间是安装 faster-whisper）
- 构建成功后会自动启动，页面右上角显示 **Running**

## 四、访问地址

构建成功后，你的应用地址是：

```
https://你的用户名-ai-guide.hf.space
```

- 主页：`https://你的用户名-ai-guide.hf.space/index.html`
- 导航页：`https://你的用户名-ai-guide.hf.space/navigation.html`

> HF Space 自带 HTTPS，手机端麦克风权限可以直接使用。

## 五、注意事项

1. **免费版限制**：16GB 内存 / 2 vCPU（对 whisper tiny 足够了）
2. **休眠**：48 小时无访问会自动休眠，再次访问会自动唤醒（约 30 秒）
3. **Coze 积分**：如果之前有 4028 错误（积分不足），仍需要到 coze.cn 充值
4. **模型已内置**：whisper tiny 模型（73MB）已打包在 Docker 镜像中，不需要在线下载
5. **Edge TTS**：websocket-client 已加入 requirements.txt，Edge TTS 可正常使用
