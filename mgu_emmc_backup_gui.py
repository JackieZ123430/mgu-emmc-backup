"""Windows desktop interface for the MGU eMMC backup tool."""
import argparse
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from mgu_emmc_backup import BackupPaused, capture, connect_ssh, probe, verify


GIB = 1024**3


class BackupWindow:
    def __init__(self, root, folder=""):
        self.root = root
        root.title("MGU eMMC 备份")
        root.minsize(740, 630)
        root.geometry("840x690")
        root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        self.events = Queue()
        self.job = None
        self.stop = Event()
        self.closing = False
        defaults = {
            "host": "", "port": "22", "user": "root", "disk": "mmcblk0",
            "auth": "SSH 代理 / 默认密钥", "key": "", "secret": "",
            "known_hosts": "", "folder": folder,
        }
        self.values = {name: tk.StringVar(root, value) for name, value in defaults.items()}
        self.status = tk.StringVar(root, "就绪")
        self.progress_text = tk.StringVar(root, "选择备份目录可查看进度")
        self.percent = tk.DoubleVar(root, 0)

        main = ttk.Frame(root, padding=(24, 18, 24, 16))
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="MGU eMMC 备份", style="Title.TLabel").pack(anchor="w", pady=(0, 8))
        ssh = ttk.LabelFrame(main, text="SSH 连接", padding=(12, 8))
        ssh.pack(fill="x", pady=(0, 10))
        ssh.columnconfigure(1, weight=3)
        ssh.columnconfigure(3, weight=1)

        self.field(ssh, "主机 IP / 名称", "host", 0)
        self.field(ssh, "端口", "port", 0, 2)
        self.field(ssh, "用户", "user", 1)
        self.field(ssh, "eMMC 设备", "disk", 1, 2)
        ttk.Label(ssh, text="认证方式").grid(row=2, column=0, sticky="w", pady=3)
        self.auth = ttk.Combobox(ssh, textvariable=self.values["auth"], state="readonly",
                                 values=("SSH 代理 / 默认密钥", "私钥文件", "密码"))
        self.auth.grid(row=2, column=1, sticky="ew", padx=(8, 13), pady=3)
        self.auth.bind("<<ComboboxSelected>>", self.auth_changed)
        self.secret_label = ttk.Label(ssh, text="口令")
        self.secret_label.grid(row=2, column=2, sticky="w", pady=3)
        self.secret = ttk.Entry(ssh, textvariable=self.values["secret"], show="●")
        self.secret.grid(row=2, column=3, sticky="ew", padx=(8, 13), pady=3)
        self.key, self.key_browse = self.file_field(ssh, "私钥文件", "key", 3)
        self.file_field(ssh, "known_hosts", "known_hosts", 4)
        self.auth_changed()

        target = ttk.LabelFrame(main, text="备份目录", padding=(12, 8))
        target.pack(fill="x")
        target.columnconfigure(1, weight=1)
        self.file_field(target, "电脑路径", "folder", 0, directory=True)
        ttk.Label(target, text="开始备份会创建子目录。续传、查看进度或校验时，选择已有备份目录。").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(3, 2))

        buttons = ttk.Frame(main)
        buttons.pack(fill="x", pady=(12, 10))
        actions = (("探测 MGU", "probe"), ("开始备份", "capture"),
                   ("续传备份", "resume"), ("校验镜像", "verify"))
        self.buttons = []
        for label, action in actions:
            button = ttk.Button(buttons, text=label, command=lambda task=action: self.start(task))
            button.pack(side="left", padx=(0, 7))
            self.buttons.append(button)
        self.pause = ttk.Button(buttons, text="停止当前任务", command=self.request_stop, state="disabled")
        self.pause.pack(side="right")

        ttk.Label(main, textvariable=self.progress_text).pack(anchor="w")
        ttk.Progressbar(main, variable=self.percent, maximum=100).pack(fill="x", pady=(4, 8))
        ttk.Label(main, textvariable=self.status).pack(anchor="w", pady=(0, 5))
        activity = ttk.Frame(main)
        activity.pack(fill="both", expand=True)
        self.log = tk.Text(activity, height=7, wrap="word", state="disabled",
                           background="#f5f6f8", foreground="#1b2530",
                           relief="flat", padx=9, pady=6, font=("Microsoft YaHei UI", 9))
        scrollbar = ttk.Scrollbar(activity, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll_events)
        self.read_progress()

    def field(self, parent, label, name, row, column=0, span=1):
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", pady=3)
        entry = ttk.Entry(parent, textvariable=self.values[name])
        entry.grid(row=row, column=column+1, columnspan=span, sticky="ew", padx=(8, 13), pady=3)
        return entry

    def file_field(self, parent, label, name, row, directory=False):
        entry = self.field(parent, label, name, row, span=3)
        button = ttk.Button(parent, text="浏览…", command=lambda: self.choose(name, directory))
        button.grid(row=row, column=4, sticky="e")
        return entry, button

    def choose(self, name, directory):
        chooser = filedialog.askdirectory if directory else filedialog.askopenfilename
        selected = chooser(parent=self.root)
        if selected:
            self.values[name].set(selected)

    def auth_changed(self, _event=None):
        mode = self.values["auth"].get()
        is_key = mode == "私钥文件"
        self.secret_label.configure(text="私钥口令" if is_key else "密码")
        self.secret.configure(state="normal" if is_key or mode == "密码" else "disabled")
        for widget in (self.key, self.key_browse):
            widget.configure(state="normal" if is_key else "disabled")
        self.values["secret"].set("")

    def append(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self, action):
        options = {name: value.get().strip() for name, value in self.values.items()
                   if name != "secret"}
        secret = self.values["secret"].get()
        folder = None
        try:
            if action != "verify":
                if not options["host"] or not options["user"] or not options["disk"]:
                    raise ValueError("请输入主机、SSH 用户和 eMMC 设备名。")
                port = int(options["port"])
                if not 1 <= port <= 65535:
                    raise ValueError("SSH 端口应在 1–65535 之间。")
                options["port"] = port
                if options["auth"] == "私钥文件" and not Path(options["key"]).is_file():
                    raise ValueError("请选择 SSH 私钥文件。")
                if options["auth"] == "密码" and not secret:
                    raise ValueError("请输入 SSH 密码。")
            if action != "probe":
                if not options["folder"]:
                    raise ValueError("请选择电脑上的备份目录。")
                folder = Path(options["folder"]).expanduser().resolve()
                if not folder.is_dir():
                    raise ValueError("所选目录不存在。")
                if action == "capture":
                    folder /= f"MGU-eMMC-{datetime.now():%Y%m%d-%H%M%S-%f}"
                    self.values["folder"].set(str(folder))
                elif not (folder / "manifest.json").is_file():
                    raise ValueError("这个目录里找不到 manifest.json。")
                if action == "resume" and (folder / "capture.lock").exists():
                    raise ValueError("该目录有备份锁。请先确认原备份进程已结束。")
        except (ValueError, OSError) as exc:
            messagebox.showerror("检查设置", str(exc), parent=self.root)
            return
        if action != "verify":
            self.values["secret"].set("")
        self.stop.clear()
        self.job = (action, folder)
        self.status.set("正在校验已保存的镜像…" if action == "verify" else "正在连接 SSH…")
        for button in self.buttons:
            button.configure(state="disabled")
        self.pause.configure(state="normal")
        if folder:
            self.append(str(folder))
        Thread(target=self.run, args=(action, folder, options, secret), daemon=True).start()

    def run(self, action, folder, opts, secret):
        def report(message, **_kwargs):
            if message.startswith("Verifying "):
                self.events.put(("status", "正在读取并校验 " + message.removeprefix("Verifying ")))
            elif ": " not in message or not message.startswith("mmcblk"):
                self.events.put(("log", message))

        try:
            if action != "verify":
                mode = opts["auth"]
                with connect_ssh(
                    opts["host"], user=opts["user"], port=opts["port"],
                    key=opts["key"] if mode == "私钥文件" else None,
                    known_hosts=opts["known_hosts"] or None,
                    password=secret if mode == "密码" else None,
                    passphrase=(secret or None) if mode == "私钥文件" else None,
                ) as client:
                    if action == "probe":
                        inventory = probe(client, opts["disk"])
                        self.events.put(("log", f"找到 {len(inventory['partitions'])} 个分区"))
                        for name, size in inventory["regions"].items():
                            self.events.put(("log", f"/dev/{name}  ·  {size:,} 字节"))
                    else:
                        self.events.put(("status", "正在读取 eMMC，已保存的分段可用于续传。"))
                        capture(client, opts["disk"], folder, resume=action == "resume",
                                report=report, stop=self.stop)
            if action != "probe":
                verify(folder, report=report, stop=self.stop)
            self.events.put(("done", "SSH 探测完成" if action == "probe" else "镜像校验全部通过"))
        except BackupPaused:
            self.events.put(("done", "任务已停止，已保存的分段仍可续传。"))
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            if "not found in known_hosts" in message:
                message = "SSH 主机密钥尚未加入 known_hosts。请先核对主机指纹，再选择已信任的 known_hosts 文件。"
            self.events.put(("error", message))

    def read_progress(self):
        target = self.job[1] if self.job and self.job[1] else self.values["folder"].get().strip()
        self.percent.set(0)
        self.progress_text.set("选择备份目录可查看进度")
        if target:
            folder = Path(target)
            try:
                state = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
                sizes = state["source"]["regions"]
                images = state["images"]
                total = sum(sizes.values())
                saved = sum(sum(chunk["bytes"] for chunk in images.get(name, {}).get("chunks", ()))
                            for name in sizes)
                self.percent.set(100 * saved / total)
                if state["complete"]:
                    stage = "读取完成"
                elif (folder / "capture.lock").exists():
                    stage = "备份目录已锁定"
                else:
                    stage = "可续传"
                self.progress_text.set(f"{stage}  ·  已保存 {saved / GIB:.2f} / {total / GIB:.2f} GiB")
            except FileNotFoundError:
                self.progress_text.set("等待备份清单")
            except (OSError, ValueError, TypeError, KeyError, ZeroDivisionError):
                self.progress_text.set("暂时无法读取备份清单")
        self.root.after(900, self.read_progress)

    def poll_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.append(value)
                elif kind == "status":
                    if not self.stop.is_set():
                        self.status.set(value)
                else:
                    self.finish(kind, value)
                    if self.closing:
                        self.root.destroy()
                        return
        except Empty:
            pass
        self.root.after(100, self.poll_events)

    def finish(self, kind, value):
        self.job = None
        self.status.set("任务失败，详情见下方记录。" if kind == "error" else value)
        self.append(value)
        self.pause.configure(state="disabled")
        for button in self.buttons:
            button.configure(state="normal")

    def request_stop(self):
        self.stop.set()
        self.status.set("正在等待当前分段结束…")
        self.pause.configure(state="disabled")

    def close(self):
        if self.job:
            self.closing = True
            self.request_stop()
        else:
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", default="")
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = tk.Tk()
    window = BackupWindow(root, args.folder)
    if args.self_test:
        root.update()
        assert root.winfo_viewable() and window.buttons[-1].winfo_viewable()
        if args.folder:
            assert window.percent.get() > 0
            assert "GiB" in window.progress_text.get()
        root.after(200, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
