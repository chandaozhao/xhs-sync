#!/bin/bash
# xhs-sync 一键安装：在新电脑上运行一次即可。
# 用法：在「终端」里进入项目文件夹，运行 bash setup.sh
set -e
cd "$(dirname "$0")"

if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" 2>/dev/null; then
  echo "⛔ 没找到可用的 Python 3。"
  echo "   如果弹出「安装命令行开发者工具」的窗口，请点「安装」，装完后重新运行：bash setup.sh"
  exit 1
fi

echo "1/4 创建 Python 环境…"
python3 -m venv .venv
echo "2/4 安装依赖…"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "3/4 下载浏览器（约 150 MB，需要几分钟）…"
.venv/bin/python -m playwright install chromium

echo "4/4 安装视频转文字工具…"
MODEL="$HOME/.xhs-sync/models/ggml-large-v3-turbo.bin"
if ! command -v brew >/dev/null 2>&1; then
  echo "⚠️ 没找到 Homebrew，跳过（同步不受影响，只是不能把视频转成文字）。"
  echo "   以后想用：先按 https://brew.sh 的说明安装 Homebrew，再重新运行 bash setup.sh"
else
  brew install ffmpeg whisper.cpp || echo "⚠️ 转文字工具没装好（同步不受影响），可以稍后重新运行 bash setup.sh"
  if [ ! -f "$MODEL" ]; then
    echo "   下载语音识别模型（约 1.6 GB，需要几分钟）…"
    mkdir -p "$(dirname "$MODEL")"
    curl -fL --retry 3 -C - -o "$MODEL.part" \
      "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin" \
      && mv "$MODEL.part" "$MODEL" \
      || echo "⚠️ 模型没下载完（网络问题），重新运行 bash setup.sh 会接着下载。"
  fi
fi

if [ ! -f config.toml ]; then
  cp config.example.toml config.toml
  echo
  echo "已创建配置文件 config.toml。请用文本编辑打开它，填写 vault_path（你的 Obsidian Vault 路径）。"
fi

echo
echo "✅ 安装完成。填好 config.toml 后，运行下面这行，在弹出的浏览器里扫码登录小红书："
echo "   .venv/bin/python sync.py --show --limit 2"
