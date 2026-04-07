"""
Verse8 签到 — 启动器 + Web 控制台
═══════════════════════════════════════════════════
功能：
  1. 启动时从 GitHub 自动检查 / 下载最新 verse8_task.py & base_module.py
  2. 动态加载外部脚本（热更新：替换 .py 即可，无需重新打包 exe）
  3. Flask + SocketIO Web 面板控制任务启停、查看日志
  4. 打包为 exe 后，业务逻辑全部通过外部 .py 文件加载
  5. verse8_runner.py 自身热更新后自动重启

启动方式：
  python verse8_runner.py
  浏览器打开 http://127.0.0.1:5001
"""

__version__ = "2026.04.07.4"

from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO, emit
import subprocess
import threading
import asyncio
import time
import datetime
import os
import sys
import importlib.util
import re
import json
import urllib.request
import urllib.error

# ═══════════════════════════════════════════════
#  路径工具
# ═══════════════════════════════════════════════

def get_base_dir():
    """获取程序根目录（兼容 exe 打包和直接运行）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_resource_path(relative_path):
    """获取资源文件路径（兼容 PyInstaller）"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


# ═══════════════════════════════════════════════
#  Flask 应用初始化
# ═══════════════════════════════════════════════

template_dir = os.path.join(get_base_dir(), "templates")
if not os.path.exists(template_dir):
    template_dir = get_resource_path("templates")
if not os.path.exists(template_dir):
    template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

app = Flask(__name__, template_folder=template_dir)
app.config['SECRET_KEY'] = 'verse8-runner-secret'
socketio = SocketIO(app, async_mode='threading')

# ═══════════════════════════════════════════════
#  项目配置
# ═══════════════════════════════════════════════

PROJECT_NAME = "Verse8 签到"
TASK_MODULE_NAME = "verse8_task"
TASK_FUNC_NAME = "run_task"          # task 模块中的入口函数名
RUNNER_PORT = 5002
DEFAULT_WORKERS = 5


def _load_local_config() -> dict:
    """从 exe 同目录的 config.json 读取本地配置（端口、并发数等），不存在则返回空"""
    cfg_path = os.path.join(get_base_dir(), "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


_LOCAL_CFG = _load_local_config()
RUNNER_PORT = int(_LOCAL_CFG.get("port", RUNNER_PORT))
DEFAULT_WORKERS = int(_LOCAL_CFG.get("workers", DEFAULT_WORKERS))

# ═══════════════════════════════════════════════
#  自动更新配置
#  ── 首次使用需要：
#     1. 在 GitHub 创建仓库
#     2. 运行 publish.py 推送代码
#     3. 把下方 GITHUB_REPO 改为你的仓库地址
# ═══════════════════════════════════════════════

CHECK_UPDATE_ON_START = True

# TODO: 替换为你的 GitHub 仓库地址（格式: 用户名/仓库名）
GITHUB_REPO = "danchelam/verse8-checkin"
GITHUB_BRANCH = "main"

_GH_RAW = f"https://raw.githubusercontent.com/{GITHUB_REPO}/refs/heads/{GITHUB_BRANCH}"
_CDN_RAW = f"https://cdn.jsdelivr.net/gh/{GITHUB_REPO}@{GITHUB_BRANCH}"

UPDATE_META_URL = f"{_GH_RAW}/version.json"
UPDATE_TASK_URL = f"{_GH_RAW}/verse8_task.py"
UPDATE_BASE_URL = f"{_GH_RAW}/base_module.py"
UPDATE_RUNNER_URL = f"{_GH_RAW}/verse8_runner.py"
_CDN_META_URL = f"{_CDN_RAW}/version.json"
_CDN_TASK_URL = f"{_CDN_RAW}/verse8_task.py"
_CDN_BASE_URL = f"{_CDN_RAW}/base_module.py"
_CDN_RUNNER_URL = f"{_CDN_RAW}/verse8_runner.py"

# ═══════════════════════════════════════════════
#  状态上报（推送到远程管理服务器，暂时关闭）
# ═══════════════════════════════════════════════

REPORT_ENABLED = False
REPORT_URL = ""                      # 例: "http://100.103.90.123:8888/api/verse8_report"
RUNNER_NAME = os.environ.get("RUNNER_NAME", "")

# ═══════════════════════════════════════════════
#  版本跟踪
# ═══════════════════════════════════════════════

LAST_TASK_VERSION = "0"
LAST_BASE_VERSION = "0"
LAST_RUNNER_VERSION = __version__
LAST_REMOTE_TASK_VERSION = ""
LAST_REMOTE_BASE_VERSION = ""
LAST_REMOTE_RUNNER_VERSION = ""
LAST_UPDATE_STATUS = "unknown"

# ═══════════════════════════════════════════════
#  版本读取 / 比较 / 更新
# ═══════════════════════════════════════════════

def read_local_version(script_path: str) -> str:
    """从 .py 文件中读取 __version__ 值"""
    if not os.path.exists(script_path):
        return "0"
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read(4096)
        m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", content)
        return m.group(1) if m else "0"
    except Exception:
        return "0"


def parse_version(v: str):
    """版本号字符串 → 元组，用于数值比较"""
    nums = re.findall(r"\d+", v)
    return tuple(int(x) for x in nums) if nums else (0,)


def _url_fetch(url: str, timeout: int = 15) -> str:
    """下载 URL 内容，加时间戳防缓存"""
    ts = int(time.time())
    full = f"{url}{'&' if '?' in url else '?'}t={ts}"
    with urllib.request.urlopen(full, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def fetch_remote_versions() -> dict:
    """从 GitHub / CDN 获取远程 version.json"""
    if not UPDATE_META_URL:
        return {}
    for label, meta_url in [("GitHub", UPDATE_META_URL), ("CDN", _CDN_META_URL)]:
        try:
            print(f"【更新】检查更新: {meta_url}")
            data = _url_fetch(meta_url, timeout=10).strip().lstrip("\ufeff")
            if data.startswith("{"):
                return json.loads(data)
        except Exception as e:
            print(f"【更新】{label} 获取失败: {e}，尝试备用源...")
    print("【更新】所有源均失败，无法获取远程版本。")
    return {}


def download_script(url: str) -> str:
    """下载脚本内容，GitHub 失败自动切换 CDN"""
    if not url:
        return ""
    cdn_url = url.replace(_GH_RAW, _CDN_RAW) if _GH_RAW in url else ""
    for label, dl_url in [("GitHub", url), ("CDN", cdn_url)]:
        if not dl_url:
            continue
        try:
            return _url_fetch(dl_url, timeout=30)
        except Exception as e:
            print(f"【更新】{label} 下载失败: {e}，尝试备用源...")
    print("【更新】所有源均下载失败。")
    return ""


def update_single_script(name: str, local_path: str, remote_version: str, download_url: str) -> bool:
    """比较版本号，有新版则下载覆盖（自动备份旧文件）"""
    local_version = read_local_version(local_path)
    if not remote_version:
        return False
    if parse_version(remote_version) <= parse_version(local_version):
        print(f"【更新】{name} 已是最新: {local_version}")
        return False

    print(f"【更新】{name} 发现新版本: {remote_version}（本地: {local_version}），下载中...")
    new_code = download_script(download_url)
    if not new_code:
        print(f"【更新】{name} 下载失败")
        return False

    try:
        if os.path.exists(local_path):
            with open(local_path, "r", encoding="utf-8") as f:
                old = f.read()
            with open(local_path + ".bak", "w", encoding="utf-8") as f:
                f.write(old)
        with open(local_path, "w", encoding="utf-8") as f:
            f.write(new_code)
        print(f"【更新】{name} 更新成功 → {remote_version}")
        return True
    except Exception as e:
        print(f"【更新】{name} 写入失败: {e}")
        return False


def _restart_self():
    """Runner 自身更新后重启进程"""
    print("【更新】verse8_runner 已更新，正在自动重启...")
    time.sleep(1)
    python = sys.executable
    script = os.path.abspath(__file__)
    if getattr(sys, 'frozen', False):
        os.execv(sys.executable, [sys.executable] + sys.argv)
    else:
        os.execv(python, [python, script] + sys.argv[1:])


def try_auto_update():
    """启动时自动检查并下载更新"""
    global LAST_TASK_VERSION, LAST_BASE_VERSION, LAST_RUNNER_VERSION
    global LAST_REMOTE_TASK_VERSION, LAST_REMOTE_BASE_VERSION, LAST_REMOTE_RUNNER_VERSION
    global LAST_UPDATE_STATUS

    if not CHECK_UPDATE_ON_START:
        print("【更新】自动更新已关闭。")
        LAST_UPDATE_STATUS = "disabled"
        return
    if not UPDATE_META_URL:
        print("【更新】未配置 UPDATE_META_URL，跳过自动更新。")
        LAST_UPDATE_STATUS = "no_config"
        return

    remote = fetch_remote_versions()
    if not remote:
        LAST_UPDATE_STATUS = "remote_unavailable"
        return
    print(f"【更新】远程版本: {remote}")

    base_dir = get_base_dir()
    task_path = os.path.join(base_dir, "verse8_task.py")
    base_path = os.path.join(base_dir, "base_module.py")

    LAST_REMOTE_TASK_VERSION = remote.get("task_version", "")
    LAST_REMOTE_BASE_VERSION = remote.get("base_version", "")
    LAST_REMOTE_RUNNER_VERSION = remote.get("runner_version", "")

    updated = False
    if UPDATE_TASK_URL and LAST_REMOTE_TASK_VERSION:
        if update_single_script("verse8_task", task_path, LAST_REMOTE_TASK_VERSION, UPDATE_TASK_URL):
            updated = True
    if UPDATE_BASE_URL and LAST_REMOTE_BASE_VERSION:
        if update_single_script("base_module", base_path, LAST_REMOTE_BASE_VERSION, UPDATE_BASE_URL):
            updated = True

    LAST_TASK_VERSION = read_local_version(task_path)
    LAST_BASE_VERSION = read_local_version(base_path)
    LAST_UPDATE_STATUS = "updated" if updated else "up_to_date"

    # Runner 自更新（仅 .py 模式，exe 包内无法热替换自身）
    if getattr(sys, 'frozen', False):
        print(f"【更新】verse8_runner (exe 模式) 跳过自更新: {__version__}")
    elif UPDATE_RUNNER_URL and LAST_REMOTE_RUNNER_VERSION:
        runner_path = os.path.abspath(__file__)
        local_runner_ver = __version__
        if parse_version(LAST_REMOTE_RUNNER_VERSION) > parse_version(local_runner_ver):
            print(f"【更新】verse8_runner 发现新版本: {LAST_REMOTE_RUNNER_VERSION}（本地: {local_runner_ver}），下载中...")
            new_code = download_script(UPDATE_RUNNER_URL)
            if new_code:
                try:
                    with open(runner_path, "r", encoding="utf-8") as f:
                        old = f.read()
                    with open(runner_path + ".bak", "w", encoding="utf-8") as f:
                        f.write(old)
                    with open(runner_path, "w", encoding="utf-8") as f:
                        f.write(new_code)
                    print(f"【更新】verse8_runner 更新成功 → {LAST_REMOTE_RUNNER_VERSION}")
                    _restart_self()
                except Exception as e:
                    print(f"【更新】verse8_runner 写入失败: {e}")
            else:
                print("【更新】verse8_runner 下载失败")
        else:
            print(f"【更新】verse8_runner 已是最新: {local_runner_ver}")
    LAST_RUNNER_VERSION = __version__


# ═══════════════════════════════════════════════
#  动态加载核心模块（支持热更新）
# ═══════════════════════════════════════════════

def _load_module_from_file(name: str, path: str):
    """从文件路径动态加载 Python 模块"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_core_modules():
    """
    加载 base_module.py 和 verse8_task.py。
    优先从 exe 同级目录加载（热更新）；失败则回退到内置版本。
    """
    base_dir = get_base_dir()
    base_path = os.path.join(base_dir, "base_module.py")
    task_path = os.path.join(base_dir, f"{TASK_MODULE_NAME}.py")

    base_mod = None
    task_mod = None

    if os.path.exists(base_path):
        print(f"【热更新】加载外部 base_module: {base_path}")
        try:
            base_mod = _load_module_from_file("base_module", base_path)
        except Exception as e:
            print(f"【热更新】加载 base_module 失败: {e}")
    if base_mod is None:
        print("【系统】回退到内置 base_module")
        try:
            import base_module as base_mod
        except ImportError as e:
            print(f"【错误】无法加载 base_module: {e}")
            return None, None

    if os.path.exists(task_path):
        print(f"【热更新】加载外部 {TASK_MODULE_NAME}: {task_path}")
        try:
            task_mod = _load_module_from_file(TASK_MODULE_NAME, task_path)
        except Exception as e:
            print(f"【热更新】加载 {TASK_MODULE_NAME} 失败: {e}")
    if task_mod is None:
        print(f"【系统】回退到内置 {TASK_MODULE_NAME}")
        try:
            task_mod = __import__(TASK_MODULE_NAME)
        except ImportError as e:
            print(f"【错误】无法加载 {TASK_MODULE_NAME}: {e}")
            return base_mod, None

    return base_mod, task_mod


# ═══════════════════════════════════════════════
#  启动时初始化
# ═══════════════════════════════════════════════

try_auto_update()
base_module, task_module = load_core_modules()

_task_ver = getattr(task_module, '__version__', '?') if task_module else '未加载'
_base_ver = getattr(base_module, '__version__', '?') if base_module else '未加载'
print(f"【版本】{TASK_MODULE_NAME}: {_task_ver} | base_module: {_base_ver} | runner: {__version__}")

task_thread = None
is_task_running = False


def log_emitter(msg):
    """将日志推送到前端 WebSocket"""
    socketio.emit('new_log', msg)


if base_module:
    base_module.set_logger_callback(log_emitter)

# ═══════════════════════════════════════════════
#  每日清除进度文件
# ═══════════════════════════════════════════════

_last_clear_date = ""


def _business_date():
    """业务日期：每日任务在北京时间 8:00 重置"""
    return (datetime.datetime.now() - datetime.timedelta(hours=8)).strftime("%Y-%m-%d")


def _clear_daily_files():
    """检查并清除过期的进度文件"""
    global _last_clear_date
    today_str = _business_date()
    if _last_clear_date == today_str:
        return
    _last_clear_date = today_str

    base_dir = get_base_dir()
    for fname in ("task_progress.json", "task_status.json", "completed_tasks.json"):
        fpath = os.path.join(base_dir, fname)
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved_date = data.get("_date", "")
            if saved_date == today_str:
                continue
            os.remove(fpath)
            log_emitter(f"【清除】{fname} 已过期（{saved_date or '无日期'}），已删除")
        except Exception:
            try:
                os.remove(fpath)
                log_emitter(f"【清除】{fname} 格式异常，已删除")
            except Exception:
                pass

    if task_module and hasattr(task_module, 'TASK_STATUS'):
        task_module.TASK_STATUS.clear()
    if task_module and hasattr(task_module, 'reset_daily_data'):
        task_module.reset_daily_data()


# ═══════════════════════════════════════════════
#  任务执行逻辑
# ═══════════════════════════════════════════════

def run_batch_logic(thread_count):
    """在后台线程中运行批量任务"""
    global is_task_running, base_module, task_module

    base_module, task_module = load_core_modules()
    if not base_module or not task_module:
        log_emitter("【错误】无法加载核心模块！")
        is_task_running = False
        socketio.emit('status_update', {'running': False})
        return

    base_module.set_logger_callback(log_emitter)
    base_module.STOP_FLAG = False

    _clear_daily_files()

    tv = getattr(task_module, '__version__', '?')
    bv = getattr(base_module, '__version__', '?')
    log_emitter(f"【版本】{TASK_MODULE_NAME}: {tv} | base_module: {bv}")

    excel_path = os.path.join(get_base_dir(), "hubshuju.xlsx")
    log_emitter(f"正在加载账号: {excel_path}")
    accounts = base_module.load_accounts(excel_path)

    if not accounts:
        log_emitter("【错误】未找到账号，请检查 hubshuju.xlsx")
        is_task_running = False
        socketio.emit('status_update', {'running': False})
        return

    log_emitter(f"共加载 {len(accounts)} 个账号，并发数: {thread_count}")

    task_func = getattr(task_module, TASK_FUNC_NAME, None)
    if not task_func:
        log_emitter(f"【错误】{TASK_MODULE_NAME} 中未找到 {TASK_FUNC_NAME} 函数！")
        is_task_running = False
        socketio.emit('status_update', {'running': False})
        return

    try:
        asyncio.run(base_module.run_batch(
            accounts,
            task_func,
            max_workers=thread_count,
        ))
    except Exception as e:
        log_emitter(f"任务执行异常: {e}")
    finally:
        is_task_running = False
        socketio.emit('status_update', {'running': False})
        log_emitter("所有任务已结束或被停止。")


# ═══════════════════════════════════════════════
#  Flask 路由 + SocketIO 事件
# ═══════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')


@socketio.on('start_task')
def handle_start_task(data):
    global task_thread, is_task_running
    if is_task_running:
        emit('new_log', "任务已经在运行中...")
        return

    try:
        threads = int(data.get('threads', DEFAULT_WORKERS))
    except (ValueError, TypeError, AttributeError):
        threads = DEFAULT_WORKERS

    is_task_running = True
    emit('status_update', {'running': True})

    task_thread = threading.Thread(
        target=run_batch_logic, args=(threads,), daemon=True,
    )
    task_thread.start()


@socketio.on('connect')
def handle_connect():
    emit('version_info', {
        'project': PROJECT_NAME,
        'task_local': LAST_TASK_VERSION,
        'base_local': LAST_BASE_VERSION,
        'runner_local': LAST_RUNNER_VERSION,
        'task_remote': LAST_REMOTE_TASK_VERSION,
        'base_remote': LAST_REMOTE_BASE_VERSION,
        'runner_remote': LAST_REMOTE_RUNNER_VERSION,
        'status': LAST_UPDATE_STATUS,
    })


@socketio.on('stop_task')
def handle_stop_task():
    global is_task_running
    if not is_task_running:
        return
    emit('new_log', "正在发送停止信号...")
    if base_module:
        base_module.stop_all_tasks()


@socketio.on('shutdown_server')
def handle_shutdown_server():
    emit('new_log', "正在关闭程序...")
    def kill():
        time.sleep(1)
        os._exit(0)
    threading.Thread(target=kill, daemon=True).start()


# ═══════════════════════════════════════════════
#  任务状态 API + 实时推送
# ═══════════════════════════════════════════════

@app.route('/api/tasks')
def api_tasks():
    if task_module and hasattr(task_module, 'TASK_STATUS'):
        return jsonify(list(task_module.TASK_STATUS.values()))
    return jsonify([])


def _get_runner_name():
    if RUNNER_NAME:
        return RUNNER_NAME
    import socket
    return socket.gethostname()


def _task_status_pusher():
    """后台线程：每 2 秒向前端推送任务状态 + 可选上报到管理服务器"""
    while True:
        socketio.sleep(2)
        if task_module and hasattr(task_module, 'TASK_STATUS') and task_module.TASK_STATUS:
            data = list(task_module.TASK_STATUS.values())
            socketio.emit('task_status_update', data)

            if REPORT_ENABLED and REPORT_URL:
                try:
                    payload = json.dumps({
                        'runner': _get_runner_name(),
                        'tasks': data,
                    }).encode('utf-8')
                    req = urllib.request.Request(
                        REPORT_URL, data=payload,
                        headers={'Content-Type': 'application/json'},
                        method='POST',
                    )
                    urllib.request.urlopen(req, timeout=3)
                except Exception:
                    pass


socketio.start_background_task(_task_status_pusher)


# ═══════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════

if __name__ == '__main__':
    port = RUNNER_PORT
    print("=" * 50)
    print(f"  {PROJECT_NAME} 控制台")
    print(f"  请在浏览器访问: http://127.0.0.1:{port}")
    print("=" * 50)

    def open_browser():
        time.sleep(1.5)
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}")
    threading.Thread(target=open_browser, daemon=True).start()

    socketio.run(app, host="0.0.0.0", debug=False, port=port, allow_unsafe_werkzeug=True)
