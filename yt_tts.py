"""
===========================================================================
YouTube Live TTS Reader (NVDA / Screen Reader Accessible Version)
===========================================================================
เปลี่ยนไปใช้ Standard Tkinter / ttk (Native Windows Controls)
เพื่อรองรับโปรแกรมอ่านหน้าจอ (NVDA, JAWS, Narrator) 100%

วิธีใช้งาน:
1. ติดตั้งไลบรารี: pip install -r requirements.txt
2. รันทดสอบ: python yt_tts.py
3. สร้างไฟล์ .exe: pyinstaller --noconsole --onefile yt_tts.py
===========================================================================
"""

import asyncio
import os
import queue
import random
import re
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext
import edge_tts
import pygame
import pytchat


class YouTubeTTSApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("YouTube Live TTS Reader (Accessible)")
        self.geometry("560x640")
        self.resizable(True, True)

        # ใช้ธีมมาตรฐานของ Windows เพื่อให้ NVDA อ่าน Focus และสถานะได้สมบูรณ์
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        # ตัวแปรควบคุมระบบ
        self.is_running = False
        self.speech_queue = queue.Queue()
        self.voice_list = ["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"]

        # เริ่มระบบเสียง Pygame
        pygame.mixer.init()

        self.setup_ui()

        # โหลดรายชื่อเสียง Multilingual ในพื้นหลัง
        threading.Thread(target=self.load_voices_thread, daemon=True).start()
        # เริ่ม Worker รออ่านคิวเสียง
        threading.Thread(target=self.audio_worker, daemon=True).start()

    def setup_ui(self):
        main_frame = ttk.Frame(self, padding="12")
        main_frame.pack(fill="both", expand=True)

        # --- ส่วนที่ 1: ช่องใส่ URL ---
        url_group = ttk.LabelFrame(main_frame, text="1. ลิงก์ไลฟ์สด YouTube", padding="10")
        url_group.pack(fill="x", pady=(0, 10))

        ttk.Label(url_group, text="YouTube Live URL หรือ Video ID:").pack(anchor="w")
        self.url_entry = ttk.Entry(url_group)
        self.url_entry.pack(fill="x", pady=(4, 0))
        self.url_entry.focus_set()  # ให้โฟกัสเริ่มต้นที่ช่องกรอกลิงก์ทันที

        # --- ส่วนที่ 2: ตั้งค่าเสียง ---
        voice_group = ttk.LabelFrame(main_frame, text="2. ตั้งค่าเสียงอ่าน", padding="10")
        voice_group.pack(fill="x", pady=(0, 10))

        self.random_voice_var = tk.BooleanVar(value=False)
        self.random_cb = ttk.Checkbutton(
            voice_group,
            text="สุ่มเสียงอัตโนมัติ (จากเสียงไทย และ Multilingual)",
            variable=self.random_voice_var,
            command=self.toggle_voice_dropdown,
        )
        self.random_cb.pack(anchor="w", pady=(0, 6))

        ttk.Label(voice_group, text="เลือกเสียงอ่าน (กรณีไม่ได้ติ๊กสุ่มเสียง):").pack(anchor="w")
        self.voice_var = tk.StringVar(value="th-TH-PremwadeeNeural")
        self.voice_combobox = ttk.Combobox(
            voice_group,
            textvariable=self.voice_var,
            values=self.voice_list,
            state="readonly",
        )
        self.voice_combobox.pack(fill="x", pady=(4, 0))

        # --- ส่วนที่ 3: เลือกหมวดข้อความที่จะให้อ่าน ---
        filter_group = ttk.LabelFrame(main_frame, text="3. เลือกหมวดข้อความที่จะให้อ่านออกเสียง", padding="10")
        filter_group.pack(fill="x", pady=(0, 10))

        self.read_normal_var = tk.BooleanVar(value=False)
        self.read_superchat_var = tk.BooleanVar(value=True)
        self.read_sticker_var = tk.BooleanVar(value=True)
        self.read_member_var = tk.BooleanVar(value=True)

        ttk.Checkbutton(
            filter_group,
            text="ข้อความแชทปกติ (Normal Chat)",
            variable=self.read_normal_var,
        ).pack(anchor="w", pady=2)

        ttk.Checkbutton(
            filter_group,
            text="Super Chat (โดเนทข้อความ)",
            variable=self.read_superchat_var,
        ).pack(anchor="w", pady=2)

        ttk.Checkbutton(
            filter_group,
            text="Super Sticker (โดเนทสติกเกอร์)",
            variable=self.read_sticker_var,
        ).pack(anchor="w", pady=2)

        ttk.Checkbutton(
            filter_group,
            text="New Member / Milestone (สมัครสมาชิกใหม่)",
            variable=self.read_member_var,
        ).pack(anchor="w", pady=2)

        # --- ส่วนที่ 4: ปุ่มควบคุม (รองรับคีย์ลัด Alt+S และ Alt+X) ---
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x", pady=(0, 10))

        self.start_btn = ttk.Button(
            btn_frame,
            text="เริ่มอ่านแชท (Alt+S)",
            underline=14,
            command=self.start_bot,
        )
        self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 5))

        self.stop_btn = ttk.Button(
            btn_frame,
            text="หยุดทำงาน (Alt+X)",
            underline=12,
            state="disabled",
            command=self.stop_bot,
        )
        self.stop_btn.pack(side="right", expand=True, fill="x", padx=(5, 0))

        # ผูกคีย์ลัดระดับหน้าต่างเพื่อให้กดได้ทันทีจากทุกจุด
        self.bind("<Alt-s>", lambda e: self.start_bot() if str(self.start_btn["state"]) == "normal" else None)
        self.bind("<Alt-S>", lambda e: self.start_bot() if str(self.start_btn["state"]) == "normal" else None)
        self.bind("<Alt-x>", lambda e: self.stop_bot() if str(self.stop_btn["state"]) == "normal" else None)
        self.bind("<Alt-X>", lambda e: self.stop_bot() if str(self.stop_btn["state"]) == "normal" else None)

        # --- ส่วนที่ 5: กล่องแสดงสถานะ (Log) ที่ NVDA เลื่อนลูกศรอ่านได้ ---
        log_group = ttk.LabelFrame(main_frame, text="4. บันทึกการทำงาน (ใช้ลูกศรขึ้น-ลงเพื่ออ่านข้อความ)", padding="10")
        log_group.pack(fill="both", expand=True)

        self.log_box = scrolledtext.ScrolledText(log_group, height=10, wrap="word", state="disabled")
        self.log_box.pack(fill="both", expand=True)

    def log(self, message):
        def _append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", message + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(0, _append)

    def toggle_voice_dropdown(self):
        if self.random_voice_var.get():
            self.voice_combobox.configure(state="disabled")
        else:
            self.voice_combobox.configure(state="readonly")

    def extract_video_id(self, url):
        patterns = [
            r"(?:v=|\/live\/|youtu\.be\/)([0-9A-Za-z_-]{11})",
            r"^([0-9A-Za-z_-]{11})$",
        ]
        for p in patterns:
            match = re.search(p, url.strip())
            if match:
                return match.group(1)
        return None

    def load_voices_thread(self):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            voices = loop.run_until_complete(edge_tts.list_voices())
            filtered = [
                v["ShortName"]
                for v in voices
                if v["Locale"].startswith("th-TH") or "Multilingual" in v["ShortName"]
            ]
            if filtered:
                self.voice_list = sorted(filtered)
                def _update_combo():
                    self.voice_combobox.configure(values=self.voice_list)
                    if "th-TH-PremwadeeNeural" in self.voice_list:
                        self.voice_var.set("th-TH-PremwadeeNeural")
                self.after(0, _update_combo)
                self.log(f"โหลดรายชื่อเสียงสำเร็จ ({len(self.voice_list)} เสียงที่พูดไทยได้)")
        except Exception as e:
            self.log(f"โหลดรายชื่อเสียงไม่สำเร็จ (ใช้เสียงพื้นฐานแทน): {e}")

    def start_bot(self):
        raw_input = self.url_entry.get()
        video_id = self.extract_video_id(raw_input)
        if not video_id:
            self.log("ข้อผิดพลาด: รูปแบบ URL หรือ Video ID ไม่ถูกต้อง")
            self.url_entry.focus_set()
            return

        self.is_running = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.url_entry.configure(state="disabled")
        self.stop_btn.focus_set()  # ย้ายโฟกัสไปที่ปุ่มหยุดทำงานเพื่อให้กด Spacebar หยุดได้ทันที
        self.log(f"กำลังเชื่อมต่อกับแชทของ Video ID: {video_id} ...")

        threading.Thread(
            target=self.chat_listener_thread, args=(video_id,), daemon=True
        ).start()

    def stop_bot(self):
        self.is_running = False
        with self.speech_queue.mutex:
            self.speech_queue.queue.clear()
        pygame.mixer.music.stop()

        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.url_entry.configure(state="normal")
        self.start_btn.focus_set()
        self.log("หยุดการทำงานแล้ว")

    def chat_listener_thread(self, video_id):
        try:
            chat = pytchat.create(video_id=video_id)
            self.log("เชื่อมต่อแชทสำเร็จ! กำลังรอรับข้อความ...")

            while self.is_running and chat.is_alive():
                for c in chat.get().sync_items():
                    if not self.is_running:
                        break

                    msg_type = c.type
                    author = c.author.name
                    msg = c.message
                    text_to_speak = None

                    if msg_type == "textMessage" and self.read_normal_var.get():
                        text_to_speak = f"{author} พูดว่า {msg}"

                    elif msg_type == "superChat" and self.read_superchat_var.get():
                        amount = c.amountString
                        text_to_speak = f"ขอบคุณคุณ {author} สำหรับซุปเปอร์แชท {amount} ครับ {msg}"

                    elif msg_type == "superSticker" and self.read_sticker_var.get():
                        amount = c.amountString
                        text_to_speak = f"ขอบคุณคุณ {author} สำหรับซุปเปอร์สติกเกอร์ {amount} ครับ"

                    elif msg_type == "newSponsor" and self.read_member_var.get():
                        text_to_speak = f"ยินดีต้อนรับคุณ {author} สู่การเป็นสมาชิกใหม่ครับ {msg}"

                    if text_to_speak:
                        self.log(f"[{msg_type}] {text_to_speak}")
                        self.speech_queue.put(text_to_speak)

                time.sleep(0.5)

            if self.is_running:
                self.log("ไลฟ์สดจบลงแล้ว หรือการเชื่อมต่อถูกตัด")
                self.after(0, self.stop_bot)

        except Exception as e:
            self.log(f"เกิดข้อผิดพลาดในการดึงแชท: {e}")
            self.after(0, self.stop_bot)

    def audio_worker(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while True:
            text = self.speech_queue.get()
            if not self.is_running:
                self.speech_queue.task_done()
                continue

            try:
                if self.random_voice_var.get():
                    voice = random.choice(self.voice_list)
                else:
                    voice = self.voice_var.get()

                temp_file = os.path.join(
                    tempfile.gettempdir(), f"tts_{random.randint(10000,99999)}.mp3"
                )

                communicate = edge_tts.Communicate(text, voice)
                loop.run_until_complete(communicate.save(temp_file))

                pygame.mixer.music.load(temp_file)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy() and self.is_running:
                    time.sleep(0.1)

                pygame.mixer.music.unload()
                if os.path.exists(temp_file):
                    os.remove(temp_file)

            except Exception as e:
                self.log(f"อ่านออกเสียงไม่สำเร็จ: {e}")
            finally:
                self.speech_queue.task_done()


if __name__ == "__main__":
    app = YouTubeTTSApp()
    app.mainloop()
