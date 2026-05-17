"""微信一键 @所有人 - 图形界面版（纯键盘模拟 + UI 扫描）。

发送原理：
  pyautogui 模拟键盘：输入 @ → 粘贴成员名 → 回车选中 → 重复 → 粘贴消息 → 发送

成员名单获取（两种方式）：
  A. 手动粘贴 / 从 txt 导入
  B. 自动扫描：你先打开群成员面板，点【扫描群成员】，
     程序用 uiautomation 库直接读 WeChat 窗口的 UI 树

依赖：pyautogui  pyperclip  uiautomation
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Iterable


APP_TITLE = "微信一键 @所有人"
DEFAULT_BATCH_SIZE = 9
DEFAULT_INTERVAL = 1.5
DEFAULT_CHAR_DELAY = 0.15
DEFAULT_COUNTDOWN = 5


class _QueueWriter:
    def __init__(self, q: "queue.Queue[str]", prefix: str = "") -> None:
        self._q = q
        self._prefix = prefix
        self._buf = ""

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._q.put(self._prefix + line)
        return len(s)

    def flush(self) -> None:
        if self._buf.strip():
            self._q.put(self._prefix + self._buf)
            self._buf = ""


def _load_input_deps():
    try:
        import pyautogui
        import pyperclip
        return pyautogui, pyperclip, None
    except ImportError as e:
        return None, None, str(e)


def _load_uia():
    try:
        import uiautomation as auto  # type: ignore
        return auto, None
    except ImportError as e:
        return None, str(e)


def chunks(seq: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# ---------- UI 树扫描：抓群成员 ----------

# 已知微信主窗口的类名（覆盖 WeChat 3.x / 微信 4.x / Weixin）
_WECHAT_CLASSES = (
    "WeChatMainWndForPC",
    "WeixinMainWndForPC",
    "Qt51514QWindowIcon",
    "Qt5152QWindowIcon",
    "Qt5QWindowIcon",
    "Chrome_WidgetWin_1",
    "Chrome_WidgetWin_0",
)
_WECHAT_NAMES = ("微信", "Weixin", "WeChat")
# 噪声项：名单里如果含这些字样大概率不是真成员
_NOISE = ("搜索", "添加", "邀请", "群聊成员", "查看更多", "@所有人", "全选")


def find_wechat_window(auto) -> object | None:
    for cls in _WECHAT_CLASSES:
        try:
            w = auto.WindowControl(searchDepth=2, ClassName=cls)
            if w.Exists(0.3):
                return w
        except Exception:
            continue
    for nm in _WECHAT_NAMES:
        try:
            w = auto.WindowControl(searchDepth=2, Name=nm)
            if w.Exists(0.3):
                return w
        except Exception:
            continue
    return None


_CONTAINER_TYPES = ("ListControl", "GroupControl", "PaneControl", "DocumentControl", "TableControl")
_ITEM_TYPES = ("ListItemControl", "TreeItemControl", "MenuItemControl", "TextControl", "ButtonControl")


def extract_members(win, max_depth: int = 30) -> tuple[list[str], list[tuple[str, int]]]:
    """走 UI 树，找含最多"看起来像姓名的子项"的容器作为成员列表。
    对 WeChat 3.x: 找 ListControl + ListItemControl
    对 微信 4.x (Qt): 找任意容器下含较多有名字的子项的那个
    返回 (成员名单, 候选诊断信息)。"""
    candidates: list[tuple[object, list[str]]] = []

    def looks_like_member_name(name: str) -> bool:
        if not name or len(name) > 30:
            return False
        if any(n in name for n in _NOISE):
            return False
        return True

    def walk(ctrl, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            ctype = ctrl.ControlTypeName
        except Exception:
            ctype = ""
        try:
            children = ctrl.GetChildren()
        except Exception:
            children = []

        if ctype in _CONTAINER_TYPES and children:
            names = []
            for c in children:
                try:
                    cctype = c.ControlTypeName
                    nm = (c.Name or "").strip()
                except Exception:
                    continue
                if cctype in _ITEM_TYPES and looks_like_member_name(nm):
                    names.append(nm)
            if len(names) >= 3:  # 至少 3 个才算候选
                candidates.append((ctrl, names))

        for c in children:
            walk(c, depth + 1)

    walk(win, 0)

    diag = []
    for ctrl, names in candidates:
        try:
            label = f"{ctrl.ControlTypeName}/{(ctrl.Name or ctrl.ClassName or '?')[:20]}"
        except Exception:
            label = "?"
        diag.append((label, len(names)))

    if not candidates:
        return [], diag

    candidates.sort(key=lambda x: -len(x[1]))
    _, best_names = candidates[0]
    seen = set()
    deduped = []
    for n in best_names:
        if n not in seen:
            seen.add(n)
            deduped.append(n)
    return deduped, diag


def extract_via_at_popup(auto, pyautogui, pyperclip, char_delay: float, log_fn) -> list[str]:
    """@ 弹窗扫描法（适用于 微信 4.x）：
    在聊天输入框输入 @ 触发成员选择弹窗，按 ↓ 翻遍所有成员收集 Name。
    要求用户已点击聊天输入框，光标在里面。
    """
    log_fn("  按下 @ 触发成员弹窗…")
    pyautogui.write("@", interval=0)
    time.sleep(0.7)

    members: list[str] = []
    seen: set[str] = set()
    desktop = auto.GetRootControl()

    def collect_visible() -> int:
        before = len(seen)
        def walk(ctrl, depth):
            if depth > 18:
                return
            try:
                ctype = ctrl.ControlTypeName
                nm = (ctrl.Name or "").strip()
            except Exception:
                return
            if ctype in _ITEM_TYPES and nm and len(nm) <= 30:
                if not any(n in nm for n in _NOISE):
                    if nm not in seen:
                        seen.add(nm)
                        members.append(nm)
            try:
                for c in ctrl.GetChildren():
                    walk(c, depth + 1)
            except Exception:
                pass
        walk(desktop, 0)
        return len(seen) - before

    added = collect_visible()
    log_fn(f"  初始弹窗可见 {added} 项")

    # 翻页：每次按 ↓ 一两次，扫描，直到连续多次没有新成员
    stalled = 0
    max_iter = 500  # 防死循环
    for i in range(max_iter):
        pyautogui.press("down")
        time.sleep(char_delay * 0.5)
        added = collect_visible()
        if added == 0:
            stalled += 1
            if stalled >= 6:
                break
        else:
            stalled = 0

    log_fn(f"  共收集 {len(members)} 项，关闭弹窗…")
    # 关闭弹窗并清掉输入框里残留的 @ 字符
    pyautogui.press("escape")
    time.sleep(char_delay)
    pyautogui.press("backspace")
    time.sleep(char_delay)
    return members


def dump_tree(win, max_depth: int = 30, max_lines: int = 2000) -> list[str]:
    """诊断用：dump WeChat 窗口完整 UI 树。"""
    lines = []
    def walk(ctrl, depth):
        if len(lines) >= max_lines:
            return
        if depth > max_depth:
            return
        try:
            ctype = ctrl.ControlTypeName
            nm = ctrl.Name or ""
            cls = getattr(ctrl, "ClassName", "") or ""
        except Exception:
            return
        if nm.strip() or ctype in _CONTAINER_TYPES + _ITEM_TYPES:
            lines.append(f"{'  '*depth}{ctype} '{nm[:40]}' [{cls}]")
        try:
            for c in ctrl.GetChildren():
                walk(c, depth + 1)
        except Exception:
            pass
    walk(win, 0)
    return lines


# ---------- GUI ----------

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title(APP_TITLE)
        root.geometry("760x820")
        root.minsize(640, 720)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()

        sys.stdout = _QueueWriter(self.log_queue, prefix="[out] ")
        sys.stderr = _QueueWriter(self.log_queue, prefix="[err] ")

        self._build_ui()
        self._drain_log()

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 5}
        frm = ttk.Frame(self.root)
        frm.pack(fill="both", expand=True)

        hint = (
            "工作流程：①打开微信群 → 点群名旁'…'或'群聊'图标展开右侧成员面板  "
            "②回本程序点【扫描群成员】  ③倒计时切回微信，等待扫描结果填入下方名单  "
            "④写消息 → 点【开始发送】 → 倒计时切回微信群聊点击输入框 → 自动 @ 发送"
        )
        ttk.Label(frm, text=hint, foreground="#666", wraplength=720, justify="left").grid(
            row=0, column=0, columnspan=4, sticky="ew", **pad
        )

        # 扫描按钮区
        scan_frm = ttk.Frame(frm)
        scan_frm.grid(row=1, column=0, columnspan=4, sticky="ew", **pad)
        ttk.Label(scan_frm, text="① 群成员名单:").pack(side="left")
        self.scan_btn = ttk.Button(scan_frm, text="🔍 扫描(面板)", command=self._start_scan)
        self.scan_btn.pack(side="left", padx=4)
        self.scan_at_btn = ttk.Button(scan_frm, text="🔍 扫描(@弹窗,推荐4.x)", command=self._start_scan_at)
        self.scan_at_btn.pack(side="left", padx=4)
        self.diag_btn = ttk.Button(scan_frm, text="🩺 全树诊断", command=self._start_dump)
        self.diag_btn.pack(side="left", padx=4)
        ttk.Button(scan_frm, text="从 txt 导入…", command=self._pick_members_file).pack(side="right", padx=4)

        # 成员名单 textarea
        self.members_text = scrolledtext.ScrolledText(frm, height=10, wrap="none")
        self.members_text.grid(row=2, column=0, columnspan=4, sticky="nsew", **pad)

        # 消息
        ttk.Label(frm, text="② 消息内容:").grid(row=3, column=0, sticky="nw", **pad)
        self.msg_text = scrolledtext.ScrolledText(frm, height=4, wrap="word")
        self.msg_text.grid(row=3, column=1, columnspan=3, sticky="ew", **pad)

        # 排除
        ttk.Label(frm, text="排除成员（逗号或空格）:").grid(row=4, column=0, sticky="w", **pad)
        self.exclude_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.exclude_var).grid(row=4, column=1, columnspan=3, sticky="ew", **pad)

        # 参数
        param_frm = ttk.Frame(frm)
        param_frm.grid(row=5, column=0, columnspan=4, sticky="ew", **pad)
        ttk.Label(param_frm, text="每批 @ 人数:").pack(side="left", padx=4)
        self.batch_var = tk.StringVar(value=str(DEFAULT_BATCH_SIZE))
        ttk.Entry(param_frm, textvariable=self.batch_var, width=6).pack(side="left", padx=4)
        ttk.Label(param_frm, text="按键间隔(s):").pack(side="left", padx=4)
        self.char_delay_var = tk.StringVar(value=str(DEFAULT_CHAR_DELAY))
        ttk.Entry(param_frm, textvariable=self.char_delay_var, width=6).pack(side="left", padx=4)
        ttk.Label(param_frm, text="批次间隔(s):").pack(side="left", padx=4)
        self.interval_var = tk.StringVar(value=str(DEFAULT_INTERVAL))
        ttk.Entry(param_frm, textvariable=self.interval_var, width=6).pack(side="left", padx=4)
        ttk.Label(param_frm, text="倒计时(s):").pack(side="left", padx=4)
        self.countdown_var = tk.StringVar(value=str(DEFAULT_COUNTDOWN))
        ttk.Entry(param_frm, textvariable=self.countdown_var, width=6).pack(side="left", padx=4)

        # 按钮
        btn_frm = ttk.Frame(frm)
        btn_frm.grid(row=6, column=0, columnspan=4, sticky="ew", **pad)
        ttk.Button(btn_frm, text="预览分批", command=self._preview).pack(side="left", padx=5)
        self.send_btn = ttk.Button(btn_frm, text="③ 开始发送", command=self._start_send)
        self.send_btn.pack(side="left", padx=5)
        self.stop_btn = ttk.Button(btn_frm, text="紧急停止", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=5)
        ttk.Button(btn_frm, text="清空日志", command=self._clear_log).pack(side="right", padx=5)

        # 日志
        ttk.Label(frm, text="日志:").grid(row=7, column=0, sticky="w", **pad)
        self.log = scrolledtext.ScrolledText(frm, height=12, wrap="word", state="disabled")
        self.log.grid(row=8, column=0, columnspan=4, sticky="nsew", **pad)

        self.status_var = tk.StringVar(value="就绪 - 紧急停止：把鼠标快速甩到屏幕左上角")
        ttk.Label(frm, textvariable=self.status_var, relief="sunken", anchor="w").grid(
            row=9, column=0, columnspan=4, sticky="ew"
        )

        for c in range(4):
            frm.columnconfigure(c, weight=1)
        frm.rowconfigure(2, weight=2)
        frm.rowconfigure(8, weight=2)

    # ---- 通用 ----
    def _pick_members_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择成员名单文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(path, encoding="gbk") as f:
                content = f.read()
        self.members_text.delete("1.0", "end")
        self.members_text.insert("1.0", content)

    def _log(self, msg: str) -> None:
        self.log_queue.put(msg)

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", msg + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log)

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _parse_members(self) -> list[str]:
        raw = self.members_text.get("1.0", "end")
        return [ln.strip() for ln in raw.splitlines() if ln.strip()]

    def _parse_exclude(self) -> set[str]:
        raw = self.exclude_var.get()
        if not raw.strip():
            return set()
        for sep in ("，", ","):
            raw = raw.replace(sep, " ")
        return {p.strip() for p in raw.split() if p.strip()}

    def _read_params(self):
        try:
            return (
                max(1, int(self.batch_var.get())),
                max(0.0, float(self.char_delay_var.get())),
                max(0.0, float(self.interval_var.get())),
                max(0, int(float(self.countdown_var.get()))),
            )
        except ValueError:
            messagebox.showerror(APP_TITLE, "参数必须是数字。")
            return None

    # ---- 扫描成员 ----
    def _start_scan(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(APP_TITLE, "上一个任务还在运行。")
            return
        params = self._read_params()
        if params is None:
            return
        _, _, _, countdown = params
        if not messagebox.askyesno(
            APP_TITLE,
            f"准备扫描微信群成员。\n\n请确保：\n"
            "1. 微信群聊已打开\n"
            "2. 右侧成员面板已展开（点群名旁的图标）\n\n"
            f"点确定后 {countdown} 秒倒计时，请切到微信窗口让它在最前面。",
        ):
            return
        self.cancel_event.clear()
        self.scan_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.worker = threading.Thread(
            target=self._do_scan, args=(countdown,), daemon=True
        )
        self.worker.start()

    def _do_scan(self, countdown: int) -> None:
        try:
            auto, err = _load_uia()
            if auto is None:
                self._log(f"[错误] 缺少 uiautomation: {err}")
                self._log("请执行: pip install uiautomation")
                return
            for s in range(countdown, 0, -1):
                if self.cancel_event.is_set():
                    self._log("已取消。"); return
                self._log(f"  扫描倒计时 {s}…切到微信窗口，让成员面板可见！")
                self.root.after(0, lambda v=s: self.status_var.set(f"扫描倒计时 {v}…"))
                time.sleep(1)

            self._log("查找微信窗口…")
            win = find_wechat_window(auto)
            if win is None:
                self._log("[错误] 找不到微信窗口。请确认微信 PC 客户端正在运行。")
                return
            self._log(f"  找到窗口: {getattr(win, 'ClassName', '?')} / {getattr(win, 'Name', '?')}")

            self._log("扫描 UI 树寻找成员列表…")
            members, diag = extract_members(win)
            self._log(f"  发现 {len(diag)} 个候选 ListControl:")
            for name, count in diag[:8]:
                self._log(f"    - {name!r}: {count} 项")
            if not members:
                self._log("[错误] 未提取到成员。可能原因：")
                self._log("  1. 成员面板没展开 → 在微信里点群名旁的图标展开右侧面板再重试")
                self._log("  2. 微信版本 UI 结构不同 → 把上面候选诊断信息发我，或手动准备 txt 名单")
                return
            self._log(f"✓ 提取到 {len(members)} 个成员，已填入名单框")
            text = "\n".join(members)
            self.root.after(0, lambda: self._fill_members(text))
        except Exception as e:
            self._log(f"[错误] {type(e).__name__}: {e}")
            self._log(traceback.format_exc())
        finally:
            self.root.after(0, lambda: self.scan_btn.configure(state="normal"))
            self.root.after(0, lambda: self.stop_btn.configure(state="disabled"))
            self.root.after(0, lambda: self.status_var.set("就绪"))

    def _fill_members(self, text: str) -> None:
        self.members_text.delete("1.0", "end")
        self.members_text.insert("1.0", text)

    # ---- 扫描 @ 弹窗（适用 微信 4.x Qt）----
    def _start_scan_at(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(APP_TITLE, "上一个任务还在运行。")
            return
        params = self._read_params()
        if params is None:
            return
        _, char_delay, _, countdown = params
        if not messagebox.askyesno(
            APP_TITLE,
            "@ 弹窗扫描法（推荐用于微信 4.x）：\n\n"
            "1. 微信里打开目标群\n"
            "2. 点击聊天输入框（光标在里面）\n"
            "3. 别动键盘鼠标\n\n"
            f"点确定后 {countdown} 秒倒计时，程序会自动按 @ 触发成员弹窗，"
            "按 ↓ 翻遍全员收集，最后清掉 @ 字符。",
        ):
            return
        self.cancel_event.clear()
        self.scan_at_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.worker = threading.Thread(
            target=self._do_scan_at, args=(countdown, char_delay), daemon=True
        )
        self.worker.start()

    def _do_scan_at(self, countdown: int, char_delay: float) -> None:
        try:
            auto, err = _load_uia()
            if auto is None:
                self._log(f"[错误] 缺少 uiautomation: {err}"); return
            pyautogui, pyperclip, err = _load_input_deps()
            if pyautogui is None:
                self._log(f"[错误] 缺少依赖: {err}"); return
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = char_delay

            for s in range(countdown, 0, -1):
                if self.cancel_event.is_set():
                    self._log("已取消。"); return
                self._log(f"  倒计时 {s}…切到微信群聊点击聊天输入框！")
                self.root.after(0, lambda v=s: self.status_var.set(f"@ 扫描倒计时 {v}…"))
                time.sleep(1)

            members = extract_via_at_popup(auto, pyautogui, pyperclip, char_delay, self._log)
            if not members:
                self._log("[错误] @ 弹窗未收集到任何成员。可能原因：")
                self._log("  - 倒计时期间没切到微信 / 没点输入框")
                self._log("  - @ 字符未触发弹窗（输入框未获焦）")
                return
            self._log(f"✓ 收集到 {len(members)} 个候选名单，已填入下方")
            text = "\n".join(members)
            self.root.after(0, lambda: self._fill_members(text))
        except Exception as e:
            self._log(f"[错误] {type(e).__name__}: {e}")
            self._log(traceback.format_exc())
        finally:
            self.root.after(0, lambda: self.scan_at_btn.configure(state="normal"))
            self.root.after(0, lambda: self.stop_btn.configure(state="disabled"))
            self.root.after(0, lambda: self.status_var.set("就绪"))

    # ---- 全树诊断 ----
    def _start_dump(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(APP_TITLE, "上一个任务还在运行。")
            return
        params = self._read_params()
        if params is None:
            return
        _, _, _, countdown = params
        if not messagebox.askyesno(
            APP_TITLE,
            "诊断模式：将 dump 微信窗口完整 UI 树到 ui_tree_dump.txt\n"
            "用来排查为什么扫描不到成员。\n\n"
            f"点确定后 {countdown} 秒倒计时，请把微信群聊面板调到目标状态（最好展开成员面板）。",
        ):
            return
        self.cancel_event.clear()
        self.diag_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.worker = threading.Thread(target=self._do_dump, args=(countdown,), daemon=True)
        self.worker.start()

    def _do_dump(self, countdown: int) -> None:
        try:
            auto, err = _load_uia()
            if auto is None:
                self._log(f"[错误] 缺少 uiautomation: {err}"); return
            for s in range(countdown, 0, -1):
                if self.cancel_event.is_set():
                    self._log("已取消。"); return
                self._log(f"  倒计时 {s}…让微信窗口保持目标状态在前台！")
                self.root.after(0, lambda v=s: self.status_var.set(f"诊断倒计时 {v}…"))
                time.sleep(1)
            win = find_wechat_window(auto)
            if win is None:
                self._log("[错误] 找不到微信窗口"); return
            self._log(f"  找到窗口: {getattr(win, 'ClassName', '?')} / {getattr(win, 'Name', '?')}")
            self._log("dump UI 树中（最多 2000 行）…")
            lines = dump_tree(win)
            import os
            out_path = os.path.join(os.path.expanduser("~"), "ui_tree_dump.txt")
            try:
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
                self._log(f"✓ 已写入 {out_path}（{len(lines)} 行）")
            except Exception as e:
                self._log(f"  写文件失败: {e}")
            self._log("--- 前 80 行预览 ---")
            for ln in lines[:80]:
                self._log(ln)
        except Exception as e:
            self._log(f"[错误] {type(e).__name__}: {e}")
            self._log(traceback.format_exc())
        finally:
            self.root.after(0, lambda: self.diag_btn.configure(state="normal"))
            self.root.after(0, lambda: self.stop_btn.configure(state="disabled"))
            self.root.after(0, lambda: self.status_var.set("就绪"))

    # ---- 预览 / 发送 ----
    def _targets(self) -> list[str] | None:
        members = self._parse_members()
        if not members:
            messagebox.showerror(APP_TITLE, "请先填入群成员名单（点【扫描群成员】或手动粘贴）。")
            return None
        exclude = self._parse_exclude()
        targets = [m for m in members if m not in exclude]
        if not targets:
            messagebox.showerror(APP_TITLE, "没有可 @ 的成员（全部被排除）。")
            return None
        return targets

    def _preview(self) -> None:
        params = self._read_params()
        if params is None:
            return
        batch, *_ = params
        targets = self._targets()
        if targets is None:
            return
        batches = list(chunks(targets, batch))
        self._log(f"共 {len(targets)} 人，分 {len(batches)} 批：")
        for i, b in enumerate(batches, 1):
            self._log(f"  第 {i} 批 ({len(b)} 人): {b}")

    def _start_send(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(APP_TITLE, "上一个任务还在运行。")
            return
        params = self._read_params()
        if params is None:
            return
        batch, char_delay, interval, countdown = params

        message = self.msg_text.get("1.0", "end").strip()
        if not message:
            messagebox.showerror(APP_TITLE, "请填写要发送的消息内容。")
            return
        targets = self._targets()
        if targets is None:
            return

        if not messagebox.askyesno(
            APP_TITLE,
            f"即将 @ {len(targets)} 人，分 {((len(targets)-1)//batch)+1} 批发送。\n\n"
            f"点确定后 {countdown} 秒倒计时，请切到微信群聊点击聊天输入框。\n\n"
            "紧急停止：鼠标快速甩到屏幕左上角。",
        ):
            return

        self.cancel_event.clear()
        self.send_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.worker = threading.Thread(
            target=self._do_send,
            args=(targets, message, batch, char_delay, interval, countdown),
            daemon=True,
        )
        self.worker.start()

    def _stop(self) -> None:
        self.cancel_event.set()
        self._log("[!] 用户请求停止。")

    def _do_send(
        self,
        targets: list[str],
        message: str,
        batch: int,
        char_delay: float,
        interval: float,
        countdown: int,
    ) -> None:
        try:
            pyautogui, pyperclip, err = _load_input_deps()
            if pyautogui is None:
                self._log(f"[错误] 缺少依赖: {err}")
                self._log("请执行: pip install pyautogui pyperclip")
                return
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = char_delay

            for s in range(countdown, 0, -1):
                if self.cancel_event.is_set():
                    self._log("已取消。"); return
                self._log(f"  发送倒计时 {s}…切到微信群聊点击输入框！")
                self.root.after(0, lambda v=s: self.status_var.set(f"发送倒计时 {v}…"))
                time.sleep(1)

            self._log(f"开始执行，{len(targets)} 个目标，每批 {batch} 人")
            self.root.after(0, lambda: self.status_var.set("发送中…"))

            batches = list(chunks(targets, batch))
            total_sent = 0
            for bi, b in enumerate(batches, 1):
                if self.cancel_event.is_set():
                    self._log("已取消。"); return
                self._log(f"  第 {bi}/{len(batches)} 批 ({len(b)} 人): {b}")
                for name in b:
                    if self.cancel_event.is_set():
                        self._log("已取消。"); return
                    self._at_one(pyautogui, pyperclip, name, char_delay)
                pyperclip.copy(message)
                time.sleep(char_delay)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(char_delay * 2)
                pyautogui.press("enter")
                total_sent += len(b)
                self._log(f"    ✓ 已发送第 {bi} 批")
                if bi < len(batches):
                    time.sleep(interval)

            self._log(f"✓ 全部完成，共 @ {total_sent} 人，发送 {len(batches)} 条消息。")
        except Exception as e:
            self._log(f"[错误] {type(e).__name__}: {e}")
            self._log(traceback.format_exc())
        finally:
            self.root.after(0, lambda: self.send_btn.configure(state="normal"))
            self.root.after(0, lambda: self.stop_btn.configure(state="disabled"))
            self.root.after(0, lambda: self.status_var.set("就绪"))

    @staticmethod
    def _at_one(pyautogui, pyperclip, name: str, delay: float) -> None:
        pyautogui.write("@", interval=0)
        time.sleep(delay)
        pyperclip.copy(name)
        time.sleep(delay)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(delay * 2)
        pyautogui.press("enter")
        time.sleep(delay)


def main() -> None:
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
