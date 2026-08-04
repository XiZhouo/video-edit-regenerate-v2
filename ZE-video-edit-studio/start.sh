#!/usr/bin/env bash
# start.sh — ZE Skill Build 的生产部署启动脚本（本机双击请用 start.command/start.bat）。
#   生产态(默认): 构建前端 dist → 后端 uvicorn 单端口同源托管 (server.py StaticFiles)，端口取自 deploy.json(PORT env 可覆盖)
#   开发态(ZE_DEV=1): 后端 API + Vite dev :5173 (HMR，跨端口)
# 环境变量: PORT (端口), ZE_DATA_DIR (数据目录, 默认 <skill>/.data), ZE_DEV (=1 走开发态)
set -euo pipefail

# 权限硬门禁:新建文件/目录一律 owner-only(0700/0600),与祖链或历史 775 解耦——
# 部署机上 .data/ 的凭据目录绝不落成组可读。core(project_topology)已在自建目录显式
# mkdir(0o700)+chmod;此处 umask 是进程级兜底,覆盖 venv/构建/日志等所有旁路写入。
umask 077

# 服务模式硬门禁:部署态(多租户)强制每个网页用户绑定自己的 LLM 账号,未绑者的浮层 AI
# fail-closed(requires_credential),绝不悄悄落到服务器共享登录。_copilot_engine_for 读此 env。
# 注:存量 ~130 下游 start.sh 由 W7-2 批改脚本统一补此行;本行是新 skill 脚手架的模板默认。
export ZE_SERVER_MODE=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
# 存量兜底:若 .data 是历史 umask 下建出的组可读目录,起服前一次性收回组/他人权限。
# umask 077 只管新建;已存在的旧目录靠这行剥权。目录不存在则静默跳过。
chmod -R go-rwx .data 2>/dev/null || true
# 端口真源 = deploy.json 的 port(Deploy.PrepareTarget reserve 后写入);PORT env 可覆盖(本地预览)。
# 绝不把任何真实端口写死成默认——历史事故(2026-06-17):start.sh 占位默认端口撞上别 skill 的
# 生产端口,部署时误顶并杀掉对方服务。故取不到端口就**显式报错退出**,绝不猜一个端口。
PORT="${PORT:-$(python3 -c "import json; print(json.load(open('deploy.json')).get('port') or '')" 2>/dev/null || true)}"
if [ -z "${PORT:-}" ]; then
  echo "[ZE][FATAL] 无法确定端口:PORT 未设且 deploy.json 无 port 字段(未经 Deploy.PrepareTarget reserve)。" >&2
  echo "  本机启动请运行 scripts/local_launcher.py;生产端口经 ZE-skill-deploy 的 Deploy.PrepareTarget 写入 deploy.json。" >&2
  exit 1
fi
export PORT   # server.py copilot 拼 loopback /mcp URL 需从 env 读 PORT(否则从请求端口兜底)

# 视频生成网关地址(Omni / Seedance 等共用)。代码侧不写死内网 URL,由部署注入;
# 与本地 v2/run-v2.sh 默认一致,未显式配置时落到内网 videogen 服务。
export ZE_VIDEOGEN_BASE_URL="${ZE_VIDEOGEN_BASE_URL:-http://videogen.tools.hupogames.com}"

# Scoped Deploy 先用固定目标适配器按 ZE_DEPENDENCIES.json 安装并验完锁；这里必须只消费结果，
# 不能再次读取自由 install 命令、联网 pip/npm 或在目标机重建前端。直接运行本脚本的存量/本机路径
# 保留旧行为，直到该工具显式迁移依赖清单。
TARGET_PREPARED="${ZE_TARGET_DEPENDENCIES_PREPARED:-0}"
if [ "$TARGET_PREPARED" = "1" ]; then
  if [ "${ZE_DEV:-0}" = "1" ]; then
    echo "[ZE][FATAL] 目标适配器准备的生产产物不允许进入 ZE_DEV 模式。" >&2
    exit 1
  fi
  if [ ! -x ".venv/bin/python" ]; then
    echo "[ZE][FATAL] 目标依赖适配器未提供已验证的 .venv。" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  if [ -d webapp ] && [ ! -f webapp/dist/index.html ]; then
    echo "[ZE][FATAL] 签名 Base Artifact 缺少预构建 webapp/dist；拒绝在服务器现场构建。" >&2
    exit 1
  fi
else
  # --- 兼容路径:尚未迁移的本机/旧部署 ---
  if [ ! -d ".venv" ]; then
    python3 -m venv .venv
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python -m pip install -q -r requirements.txt
  if [ -d webapp ] && [ ! -d "webapp/node_modules" ]; then
    if [ -f webapp/package-lock.json ]; then
      ( cd webapp && npm ci )
    else
      ( cd webapp && npm install )
    fi
  fi
fi

if [ "${ZE_DEV:-0}" = "1" ] && [ -d webapp ]; then
  # --- 开发态: 后端(后台) + Vite dev(前台, HMR) ---
  echo "[ZE] DEV backend  → http://localhost:${PORT}"
  uvicorn server:app --host 0.0.0.0 --port "${PORT}" &
  BACKEND_PID=$!
  trap 'kill "$BACKEND_PID" 2>/dev/null || true' EXIT
  echo "[ZE] DEV frontend → http://localhost:5173 (VITE_API_BASE=http://localhost:${PORT})"
  ( cd webapp && VITE_API_BASE="http://localhost:${PORT}" npm run dev )
else
  # --- 生产态: 构建 dist(VITE_API_BASE 留空=同源) → uvicorn 后台单端口托管 UI+API ---
  # 部署契约(ssh_deploy.sh):**后台启动 + 写 .pid + 返回**(不能前台 block,否则 ssh 挂死、
  # 部署不收尾、端口不登记)。停旧:先 kill .pid,再按 cwd 归属清理本端口残留(见下,不无脑杀端口占用者)。
  # 每次都构建:旧逻辑"dist 存在就跳过"会让改了源码后重启仍跑旧包(前端改动不生效);
  # 这些 vite 应用 build 约秒级,代价可忽略,换"重启必反映当前源码"。
  if [ -d webapp ] && [ "$TARGET_PREPARED" != "1" ]; then
    echo "[ZE] building frontend → webapp/dist"
    ( cd webapp && npm run build )
  elif [ -d webapp ]; then
    echo "[ZE] using signed prebuilt frontend → webapp/dist"
  fi

  # 停旧进程:① kill 记录的 .pid;② 端口兜底——仅杀「占着本端口且 cwd 属于本部署目录」的进程。
  # 历史事故(2026-06-17):旧逻辑对 ${PORT} 无脑 fuser -k / lsof|kill -9,一旦 PORT 配错就误杀
  # 别人的生产服务(screen-guide 误顶 8765 杀掉 seek-find)。故按 /proc/<pid>/cwd 校验进程归属再杀;
  # 占用者不属于本目录就**不杀**——让后续 bind 失败、health 超时、部署显式失败,绝不附带破坏。
  # 无 /proc(macOS 本地开发)则只信 .pid,不按端口杀。
  if [ -f .pid ]; then kill "$(cat .pid)" 2>/dev/null || true; fi
  if command -v lsof >/dev/null 2>&1; then
    for _pid in $(lsof -ti "tcp:${PORT}" 2>/dev/null || true); do
      [ -e "/proc/$_pid/cwd" ] || continue
      _cwd="$(readlink -f "/proc/$_pid/cwd" 2>/dev/null || true)"
      case "$_cwd" in
        "$ROOT"|"$ROOT"/*) kill "$_pid" 2>/dev/null || true; sleep 0.3; kill -9 "$_pid" 2>/dev/null || true ;;
        *) echo "[ZE] 跳过 pid=$_pid(占 ${PORT} 但 cwd=$_cwd 不属于 $ROOT,不杀)" >&2 ;;
      esac
    done
  fi

  # 等端口真正空出再起(最多 ~10s):上面只杀了「属于本目录」的旧进程,这里纯等待其释放。
  # 若端口被「不属于本目录」的进程占着,这里**不再强杀**——等不到就让下面 bind/health 失败而非误杀。
  for _ in $(seq 1 20); do
    if python3 -c "import socket,sys;s=socket.socket();s.settimeout(0.5);r=s.connect_ex(('127.0.0.1',${PORT}));s.close();sys.exit(0 if r else 1)"; then
      break
    fi
    sleep 0.5
  done

  echo "[ZE] serving (UI+API) → http://0.0.0.0:${PORT}"
  nohup uvicorn server:app --host 0.0.0.0 --port "${PORT}" > app.log 2>&1 &
  echo $! > .pid

  # 健康校验:轮询 /health 确认**新**进程真的起来了;否则部署**显式失败**(退非零),
  # 让部署流水线看见失败,而不是静默留着旧进程继续服务旧版。
  for _ in $(seq 1 40); do
    sleep 0.5
    if python3 -c "import urllib.request,sys;urllib.request.urlopen('http://127.0.0.1:${PORT}/health',timeout=2)" 2>/dev/null; then
      echo "[ZE] started pid $(cat .pid), health OK, log: app.log"
      exit 0
    fi
    if ! kill -0 "$(cat .pid)" 2>/dev/null; then
      echo "[ZE][FATAL] uvicorn 启动即退出(端口被占 / 构建坏?)。app.log 末尾:" >&2
      tail -n 30 app.log >&2 || true
      exit 1
    fi
  done
  echo "[ZE][FATAL] /health 超时,新进程未就绪 → 部署判失败(不静默留旧版)。app.log 末尾:" >&2
  tail -n 30 app.log >&2 || true
  exit 1
fi
