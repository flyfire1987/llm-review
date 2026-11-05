#!/bin/bash
# 大模型综述 —— 一键重启脚本
# 独立目录 /root/.openclaw/workspace/llm-review, 独立端口 8090
cd /root/.openclaw/workspace/llm-review

# 编辑令牌（写入 API 需要校验）
# 首次部署请修改此值，或通过环境变量 LLM_REVIEW_ADMIN_TOKEN 覆盖
export LLM_REVIEW_ADMIN_TOKEN="${LLM_REVIEW_ADMIN_TOKEN:-llm-review-edit-2026}"

# 杀掉旧进程：先按端口拿 PID，再用 pkill 兜底
OLD_PID=$(ss -tlnp 2>/dev/null | grep ":8090 " | head -1 | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
if [ -n "$OLD_PID" ]; then
  kill -9 "$OLD_PID" 2>/dev/null
  sleep 1
fi
pkill -9 -f "llm-review/.*app\.py" 2>/dev/null
sleep 1

# 重启
nohup python3 app.py > /tmp/llm_review.log 2>&1 &
sleep 2

# 验证
if ss -tlnp 2>/dev/null | grep -q ":8090 "; then
  echo "✅ 启动成功，端口 8090 运行中"
else
  echo "❌ 启动失败，请检查日志: tail -20 /tmp/llm_review.log"
fi
