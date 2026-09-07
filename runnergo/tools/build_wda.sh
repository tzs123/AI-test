#!/bin/bash
# 构建 WebDriverAgent 并修复 Xcode 26.6 SDK 与 iOS 26.5 模拟器运行时不匹配的缺库问题，
# 然后安装到指定模拟器并启动（WDA HTTP 服务监听 Mac 的 127.0.0.1:8100）。
#
# 用法: tools/build_wda.sh <simulator-udid>
# 可用环境变量: WDA_SRC（源码目录，默认 /Users/tanzsongsen/RunnerGo/WebDriverAgent）
#               WDA_DERIVED（构建产物目录，默认 /Users/tanzsongsen/RunnerGo/wda-derived）
set -e

UDID="${1:?用法: build_wda.sh <simulator-udid>}"
SRC="${WDA_SRC:-/Users/tanzsongsen/RunnerGo/WebDriverAgent}"
OUT="${WDA_DERIVED:-/Users/tanzsongsen/RunnerGo/wda-derived}"
DEV_XCODE="$(xcode-select -p)/Platforms/iPhoneSimulator.platform/Developer"

if [ ! -d "$SRC/WebDriverAgent.xcodeproj" ]; then
  git clone --depth 1 https://github.com/appium/WebDriverAgent.git "$SRC"
fi

xcodebuild build-for-testing \
  -project "$SRC/WebDriverAgent.xcodeproj" \
  -scheme WebDriverAgentRunner \
  -destination "platform=iOS Simulator,id=$UDID" \
  -derivedDataPath "$OUT" \
  CODE_SIGNING_ALLOWED=NO

APP="$OUT/Build/Products/Debug-iphonesimulator/WebDriverAgentRunner-Runner.app"

# SDK(26.6) 构建产物依赖 Developer 目录里的 Testing 组件，运行时(26.5)没有这些库；
# simctl launch 不注入 Developer 路径，所以直接补进 app 包内（@rpath 会搜索 app/Frameworks）。
cp "$DEV_XCODE/usr/lib/lib_TestingInterop.dylib" "$APP/Frameworks/"
mkdir -p "$APP/PlugIns/WebDriverAgentRunner.xctest/Frameworks"
for fw in _Testing_Foundation _Testing_CoreGraphics _Testing_CoreImage _Testing_UIKit; do
  cp -R "$DEV_XCODE/Library/Frameworks/$fw.framework" "$APP/Frameworks/"
  cp -R "$DEV_XCODE/Library/Frameworks/$fw.framework" "$APP/PlugIns/WebDriverAgentRunner.xctest/Frameworks/"
done

xcrun simctl install "$UDID" "$APP"
xcrun simctl launch "$UDID" com.facebook.WebDriverAgentRunner.xctrunner

for _ in $(seq 1 10); do
  sleep 2
  if curl -s -m 3 http://127.0.0.1:8100/status >/dev/null; then
    echo "WDA 已就绪: http://127.0.0.1:8100"
    exit 0
  fi
done
echo "WDA 未就绪，请查看模拟器日志" >&2
exit 1
