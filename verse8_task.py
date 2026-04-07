"""
Verse8 每日签到任务
═══════════════════════════════════════════════════
流程：打开游戏 → 点 START → 挂机 5 分钟 → 签到领积分

由 verse8_runner.py 通过 base_module.run_batch() 调用。
"""

__version__ = "2026.04.07.5"

import asyncio
import datetime
import hashlib
import json
import os
from typing import Optional

from playwright.async_api import Page, BrowserContext, Frame

from base_module import (
    log, WalletPopupHandler, STOP_FLAG,
)


# ════════════════════════════════════════════════════════════
#  业务配置
# ════════════════════════════════════════════════════════════

GAME_URL = "https://verse8.io/F9x8Q3e"
POINT_URL = "https://verse8.io/point"
IDLE_SECONDS = 310
DAILY_RESET_HOUR = 8  # UTC 0 点 = 北京时间 8 点


def _business_date() -> str:
    return (datetime.datetime.now() - datetime.timedelta(hours=DAILY_RESET_HOUR)).strftime("%Y-%m-%d")


# ════════════════════════════════════════════════════════════
#  实时状态追踪（供 Web 前端展示，由 runner 通过 SocketIO 推送）
# ════════════════════════════════════════════════════════════

TASK_STATUS: dict = {}
_TASK_STATUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_status.json")


def _load_task_status():
    global TASK_STATUS
    if os.path.exists(_TASK_STATUS_FILE):
        try:
            with open(_TASK_STATUS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("_date", "") != _business_date():
                os.remove(_TASK_STATUS_FILE)
                TASK_STATUS = {}
                return
            data.pop("_date", None)
            TASK_STATUS = data
        except Exception:
            TASK_STATUS = {}


def _save_task_status():
    try:
        data = dict(TASK_STATUS)
        data["_date"] = _business_date()
        with open(_TASK_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _update_status(account_id: str, status: str = "", detail: str = "", error: str = ""):
    """更新账号的实时运行状态（前端可见）"""
    if account_id not in TASK_STATUS:
        TASK_STATUS[account_id] = {
            "name": account_id, "status": "waiting",
            "detail": "", "error": "", "updated_at": "",
        }
    entry = TASK_STATUS[account_id]
    if status:
        entry["status"] = status
    if detail:
        entry["detail"] = detail
    if error:
        entry["error"] = error
    entry["updated_at"] = datetime.datetime.now().strftime("%H:%M:%S")
    _save_task_status()


# ════════════════════════════════════════════════════════════
#  进度持久化
# ════════════════════════════════════════════════════════════

_PROGRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_progress.json")
_PROGRESS: dict = {}


def _load_progress():
    global _PROGRESS
    if os.path.exists(_PROGRESS_FILE):
        try:
            with open(_PROGRESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("_date", "") != _business_date():
                os.remove(_PROGRESS_FILE)
                _PROGRESS = {}
                return
            data.pop("_date", None)
            _PROGRESS = data
        except Exception:
            _PROGRESS = {}
    else:
        _PROGRESS = {}


def _save_progress():
    data = dict(_PROGRESS)
    data["_date"] = _business_date()
    try:
        with open(_PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def reset_daily_data():
    _load_progress()


# ════════════════════════════════════════════════════════════
#  截图工具
# ════════════════════════════════════════════════════════════

def _screenshot_dir(account_id: str) -> str:
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots", account_id)
    os.makedirs(base, exist_ok=True)
    return base


async def take_screenshot(page: Page, account_id: str, label: str = "debug"):
    try:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{label}_{ts}.png"
        path = os.path.join(_screenshot_dir(account_id), filename)
        await page.screenshot(path=path)
        log(account_id, f"截图: {account_id}/{filename}")
    except Exception as e:
        log(account_id, f"截图失败: {e}")


# ════════════════════════════════════════════════════════════
#  工具函数
# ════════════════════════════════════════════════════════════

async def click_canvas(frame: Frame, rx: float, ry: float):
    """在 iframe 内 canvas 的相对位置发送点击事件"""
    await frame.evaluate(f"""(() => {{
        let c = document.querySelector('canvas');
        if (!c) return;
        let rect = c.getBoundingClientRect();
        let x = c.width * {rx};
        let y = c.height * {ry};
        let opts = {{
            bubbles: true, cancelable: true,
            clientX: rect.left + x, clientY: rect.top + y,
            button: 0
        }};
        c.dispatchEvent(new PointerEvent('pointerdown', opts));
        c.dispatchEvent(new MouseEvent('mousedown', opts));
        c.dispatchEvent(new MouseEvent('mouseup', opts));
        c.dispatchEvent(new MouseEvent('click', opts));
        c.dispatchEvent(new PointerEvent('pointerup', opts));
    }})()""")


async def get_canvas_hash(frame: Frame) -> str:
    try:
        canvas = frame.locator("canvas").first
        screenshot = await canvas.screenshot()
        return hashlib.md5(screenshot).hexdigest()
    except Exception:
        return ""


def find_game_frame(page: Page) -> Optional[Frame]:
    for f in page.frames:
        if "verse8.games" in f.url:
            return f
    return None


async def handle_cloudflare(page: Page, aid: str, timeout: int = 30) -> bool:
    """处理 Cloudflare Turnstile：等自动通过 → 尝试点击 → 超时跳过"""
    for i in range(timeout):
        cf_iframe = page.locator("iframe[src*='challenges.cloudflare.com']")
        if await cf_iframe.count() == 0:
            return True

        if i == 5:
            log(aid, "Cloudflare 未自动通过，尝试点击...")
            try:
                cf_frame = page.frame_locator("iframe[src*='challenges.cloudflare.com']")
                await cf_frame.locator("[type='checkbox'], .cb-lb, body").first.click()
            except Exception:
                pass

        await asyncio.sleep(1)

    log(aid, "Cloudflare 验证超时")
    return False


async def check_login_state(page: Page) -> bool:
    """已登录 = 页面上有用户名按钮，没有 Sign in"""
    try:
        sign_in = page.locator("button:has-text('Sign in'), span:has-text('Sign in')")
        if await sign_in.count() > 0:
            return False
        user_btn = page.locator("button.flex.items-center.px-3.py-2.gap-2")
        return await user_btn.count() > 0
    except Exception:
        return False


# ════════════════════════════════════════════════════════════
#  核心流程
# ════════════════════════════════════════════════════════════

async def run_task(page: Page, context: BrowserContext, account_id: str,
                   popup_handler: WalletPopupHandler, **kwargs) -> bool:
    """
    Verse8 每日签到完整流程（框架标准入口）
    返回 True = 签到成功, False = 失败
    """
    _load_progress()
    aid = account_id

    # 检查今日是否已完成
    if _PROGRESS.get(aid):
        log(aid, "今日已签到，跳过")
        _update_status(aid, "completed", "已签到")
        return True

    # ── 步骤1：打开游戏页面 ──
    _update_status(aid, "loading", "打开游戏页面")
    log(aid, f"打开游戏页面: {GAME_URL}")
    for attempt in range(3):
        try:
            await page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
            break
        except Exception as e:
            log(aid, f"页面加载失败（{attempt+1}/3）: {e}")
            if attempt < 2:
                await asyncio.sleep(5)
    else:
        log(aid, "页面加载彻底失败")
        _update_status(aid, "failed", "页面加载失败")
        return False

    await asyncio.sleep(5)

    # ── 步骤2：检查登录状态 ──
    log(aid, "检查登录状态...")
    logged_in = await check_login_state(page)
    if not logged_in:
        log(aid, "未登录，跳过此账号")
        await take_screenshot(page, aid, "not_logged_in")
        _update_status(aid, "failed", "未登录")
        return False
    log(aid, "已登录")

    # ── 步骤3：等待游戏 iframe 加载 ──
    _update_status(aid, "loading", "等待游戏加载")
    log(aid, "等待游戏加载...")
    try:
        await page.wait_for_selector("iframe[title='Yapybara Defense']", timeout=120000)
    except Exception:
        log(aid, "游戏 iframe 加载超时")
        await take_screenshot(page, aid, "iframe_timeout")
        _update_status(aid, "failed", "游戏加载超时")
        return False

    log(aid, "等待加载遮罩消失...")
    try:
        await page.wait_for_selector("div.absolute.inset-0.z-50", state="hidden", timeout=120000)
    except Exception:
        log(aid, "加载遮罩超时，继续尝试...")

    await asyncio.sleep(5)

    # ── 步骤4：点击 START ──
    game_frame = find_game_frame(page)
    if not game_frame:
        log(aid, "未找到游戏 iframe")
        _update_status(aid, "failed", "找不到游戏")
        return False

    canvas_count = await game_frame.evaluate("document.querySelectorAll('canvas').length")
    if canvas_count == 0:
        log(aid, "游戏内没有 canvas")
        _update_status(aid, "failed", "无 canvas")
        return False

    _update_status(aid, "playing", "点击 START")
    log(aid, "点击 START 按钮...")
    hash_before = await get_canvas_hash(game_frame)
    await click_canvas(game_frame, 0.50, 0.68)
    await asyncio.sleep(3)
    hash_after = await get_canvas_hash(game_frame)

    if hash_before == hash_after:
        log(aid, "第一次点击未生效，重试...")
        for rx, ry in [(0.50, 0.65), (0.50, 0.72), (0.50, 0.60)]:
            await click_canvas(game_frame, rx, ry)
            await asyncio.sleep(2)
            h = await get_canvas_hash(game_frame)
            if h != hash_before:
                log(aid, f"点击生效 ({rx}, {ry})")
                break
        else:
            log(aid, "多次尝试均未生效，继续挂机（可能已在游戏中）")

    # ── 步骤5：挂机 ──
    _update_status(aid, "playing", f"挂机 {IDLE_SECONDS}s")
    log(aid, f"开始挂机 {IDLE_SECONDS} 秒...")
    interval = 30
    elapsed = 0
    while elapsed < IDLE_SECONDS:
        if STOP_FLAG:
            log(aid, "收到停止信号")
            return False
        wait = min(interval, IDLE_SECONDS - elapsed)
        await asyncio.sleep(wait)
        elapsed += wait
        remaining = IDLE_SECONDS - elapsed
        if remaining > 0:
            log(aid, f"挂机中... 剩余 {remaining}s")
            _update_status(aid, "playing", f"剩余 {remaining}s")

    log(aid, "挂机完成")

    # ── 步骤6：跳转积分页面（先释放游戏资源防止 Page crashed） ──
    _update_status(aid, "checking_in", "跳转积分页")

    # 尝试通过 JS 移除游戏 iframe 释放内存
    try:
        await page.evaluate("document.querySelectorAll('iframe').forEach(f => f.remove())")
        log(aid, "已移除游戏 iframe，释放资源")
        await asyncio.sleep(2)
    except Exception:
        pass

    # 先导航到空白页，让渲染进程回收游戏的 GPU/内存
    try:
        await page.goto("about:blank", timeout=10000)
        await asyncio.sleep(1)
    except Exception:
        pass

    log(aid, f"跳转积分页面: {POINT_URL}")
    page_ok = False
    for attempt in range(3):
        try:
            await page.goto(POINT_URL, wait_until="domcontentloaded", timeout=60000)
            page_ok = True
            break
        except Exception as e:
            log(aid, f"积分页面加载失败（{attempt+1}/3）: {e}")
            if "crashed" in str(e).lower():
                # 页面渲染进程崩溃，尝试新建标签页
                log(aid, "页面崩溃，尝试新建标签页...")
                try:
                    page = await context.new_page()
                    await page.goto(POINT_URL, wait_until="domcontentloaded", timeout=60000)
                    page_ok = True
                    log(aid, "新标签页加载成功")
                    break
                except Exception as e2:
                    log(aid, f"新标签页也失败: {e2}")
            if attempt < 2:
                await asyncio.sleep(5)

    if not page_ok:
        _update_status(aid, "failed", "积分页加载失败")
        return False

    await asyncio.sleep(5)

    # ── 步骤7：处理 Cloudflare（页面级） ──
    if await page.locator("iframe[src*='challenges.cloudflare.com']").count() > 0:
        log(aid, "检测到 Cloudflare 验证...")
        if not await handle_cloudflare(page, aid):
            log(aid, "Cloudflare 未通过，刷新重试...")
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)

    # ── 步骤8：点击签到 ──
    _update_status(aid, "checking_in", "点击签到")
    log(aid, "查找签到按钮...")

    try:
        await page.wait_for_selector("button:has-text('Check-in Now')", timeout=30000)
    except Exception:
        log(aid, "未找到 Check-in Now，尝试先点 Go Earn Points...")
        go_btn = page.locator("button:has-text('Go Earn Points!')")
        if await go_btn.count() > 0:
            await go_btn.click()
            await asyncio.sleep(3)

    checkin_btn = page.locator("button:has-text('Check-in Now')")
    if await checkin_btn.count() == 0:
        log(aid, "签到按钮不存在，可能今日已签或未满足条件")
        await take_screenshot(page, aid, "no_checkin_btn")
        _update_status(aid, "failed", "无签到按钮")
        return False

    disabled = await checkin_btn.first.get_attribute("disabled")
    if disabled is not None:
        log(aid, "签到按钮禁用（可能游戏时间未满 5 分钟）")
        await take_screenshot(page, aid, "btn_disabled")
        _update_status(aid, "failed", "按钮禁用")
        return False

    log(aid, "点击 Check-in Now...")
    await checkin_btn.first.click()
    await asyncio.sleep(3)

    # 处理签到后的 Cloudflare
    if await page.locator("iframe[src*='challenges.cloudflare.com']").count() > 0:
        log(aid, "签到后出现 Cloudflare...")
        await handle_cloudflare(page, aid, timeout=30)
        await asyncio.sleep(3)

    # ── 步骤9：检查签到结果 ──
    _update_status(aid, "checking_in", "等待结果")
    log(aid, "检查签到结果...")

    for _ in range(15):
        # 成功
        claimed = page.locator("text=You've claimed")
        if await claimed.count() > 0:
            text = await claimed.first.inner_text()
            log(aid, f"签到成功: {text}")
            confirm = page.locator("button:has-text('Confirm')")
            if await confirm.count() > 0:
                await confirm.first.click()
                log(aid, "已确认")
            _PROGRESS[aid] = True
            _save_progress()
            _update_status(aid, "completed", text)
            return True

        # 失败
        unable = page.locator("text=Unable to Claim")
        if await unable.count() > 0:
            log(aid, "签到失败: Unable to Claim（账号受限）")
            close_btn = page.locator("button:has-text('Close')")
            if await close_btn.count() > 0:
                await close_btn.first.click()
            await take_screenshot(page, aid, "unable_to_claim")
            _update_status(aid, "failed", "账号受限")
            return False

        await asyncio.sleep(1)

    # 超时，刷新重试一次
    log(aid, "结果超时，刷新重试...")
    try:
        await page.reload(wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)
        checkin_btn = page.locator("button:has-text('Check-in Now')")
        if await checkin_btn.count() > 0:
            disabled = await checkin_btn.first.get_attribute("disabled")
            if disabled is not None:
                log(aid, "刷新后按钮禁用，推测签到已成功")
                _PROGRESS[aid] = True
                _save_progress()
                _update_status(aid, "completed", "推测成功")
                return True
            log(aid, "按钮仍可点击，重新签到...")
            await checkin_btn.first.click()
            await asyncio.sleep(5)
            claimed = page.locator("text=You've claimed")
            if await claimed.count() > 0:
                text = await claimed.first.inner_text()
                log(aid, f"重试成功: {text}")
                confirm = page.locator("button:has-text('Confirm')")
                if await confirm.count() > 0:
                    await confirm.first.click()
                _PROGRESS[aid] = True
                _save_progress()
                _update_status(aid, "completed", text)
                return True
    except Exception as e:
        log(aid, f"重试异常: {e}")

    await take_screenshot(page, aid, "result_unknown")
    log(aid, "签到结果不确定")
    _update_status(aid, "failed", "结果不确定")
    return False
