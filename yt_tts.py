# ==============================================================================
# YouTube Live TTS Reader (wxPython Edition - NVDA / Screen Reader Accessible)
# ==============================================================================
# วิธีติดตั้งไลบรารี:
#   pip install wxPython pytchat edge-tts pygame pyinstaller
#
# วิธีบีบอัดเป็นไฟล์ .exe:
#   pyinstaller --noconsole --onefile yt_tts.py
# ==============================================================================

import asyncio
import os
import queue
import random
import re
import tempfile
import threading
import time
import edge_tts
import pygame
import pytchat
import wx


class YouTubeTTSFrame(wx.Frame):

    def __init__(self):
        super().__init__(
            parent=None,
            title="YouTube Live TTS Reader",
            size=(560, 650),
            style=wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX),
        )

        # ตัวแปรควบคุมระบบ
        self.is_running = False
        self.speech_queue = queue.Queue()
        self.voice_list = ["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"]

        # เริ่มระบบเสียง Pygame
        pygame.mixer.init()

        self.setup_ui()
        self.setup_accelerators()
        self.Centre()

        # ตั้งโฟกัสเริ่มต้นที่ช่องใส่ URL เพื่อความสะดวกของ Screen Reader
        self.url_entry.SetFocus()

        # โหลดรายชื่อเสียง Multilingual ในพื้นหลัง
        threading.Thread(target=self.load_voices_thread, daemon=True).start()
        # เริ่ม Worker รออ่านคิวเสียง
        threading.Thread(target=self.audio_worker, daemon=True).start()

        self.Bind(wx.EVT_CLOSE, self.on_close)

    def setup_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # --- ส่วนที่ 1: ช่องใส่ URL ---
        url_box = wx.StaticBox(panel, label="การเชื่อมต่อไลฟ์สด")
        url_sizer = wx.StaticBoxSizer(url_box, wx.VERTICAL)

        url_label = wx.StaticText(panel, label="YouTube Live &URL หรือ Video ID:")
        self.url_entry = wx.TextCtrl(panel, name="YouTube Live URL หรือ Video ID")

        url_sizer.Add(url_label, 0, wx.ALL, 5)
        url_sizer.Add(self.url_entry, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        main_sizer.Add(url_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # --- ส่วนที่ 2: ตั้งค่าเสียง ---
        voice_box = wx.StaticBox(panel, label="ตั้งค่าเสียงอ่าน")
        voice_sizer = wx.StaticBoxSizer(voice_box, wx.VERTICAL)

        self.random_cb = wx.CheckBox(
            panel, label="&สุ่มเสียงอัตโนมัติ (จากเสียงไทย และ Multilingual)"
        )
        self.random_cb.Bind(wx.EVT_CHECKBOX, self.on_toggle_random)

        voice_label = wx.StaticText(panel, label="เลือกเสียง&พูด:")
        self.voice_choice = wx.Choice(
            panel, choices=self.voice_list, name="เลือกเสียงพูด"
        )
        self.voice_choice.SetSelection(0)

        voice_sizer.Add(self.random_cb, 0, wx.ALL, 6)
        voice_sizer.Add(voice_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        voice_sizer.Add(
            self.voice_choice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8
        )
        main_sizer.Add(voice_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- ส่วนที่ 3: เลือกหมวดข้อความที่จะให้อ่าน ---
        filter_box = wx.StaticBox(panel, label="หมวดข้อความที่จะให้อ่านออกเสียง")
        filter_sizer = wx.StaticBoxSizer(filter_box, wx.VERTICAL)

        self.read_normal_cb = wx.CheckBox(panel, label="ข้อความแชท&ปกติ (Normal Chat)")
        self.read_superchat_cb = wx.CheckBox(
            panel, label="Super &Chat (โดเนทข้อความ)"
        )
        self.read_sticker_cb = wx.CheckBox(
            panel, label="Super S&ticker (โดเนทสติกเกอร์)"
        )
        self.read_member_cb = wx.CheckBox(
            panel, label="New &Member / Milestone (สมัครสมาชิกใหม่)"
        )

        # ค่าเริ่มต้น: ติ๊กเฉพาะตัวพิเศษ
        self.read_normal_cb.SetValue(False)
        self.read_superchat_cb.SetValue(True)
        self.read_sticker_cb.SetValue(True)
        self.read_member_cb.SetValue(True)

        filter_sizer.Add(self.read_normal_cb, 0, wx.ALL, 5)
        filter_sizer.Add(self.read_superchat_cb, 0, wx.ALL, 5)
        filter_sizer.Add(self.read_sticker_cb, 0, wx.ALL, 5)
        filter_sizer.Add(self.read_member_cb, 0, wx.ALL, 5)
        main_sizer.Add(filter_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- ส่วนที่ 4: ปุ่มควบคุม ---
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.start_btn = wx.Button(panel, label="เริ่มอ่านแชท (Alt+S)")
        self.stop_btn = wx.Button(panel, label="หยุดทำงาน (Alt+X)")
        self.stop_btn.Disable()

        self.start_btn.Bind(wx.EVT_BUTTON, self.on_start)
        self.stop_btn.Bind(wx.EVT_BUTTON, self.on_stop)

        btn_sizer.Add(self.start_btn, 1, wx.EXPAND | wx.RIGHT, 5)
        btn_sizer.Add(self.stop_btn, 1, wx.EXPAND | wx.LEFT, 5)
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- ส่วนที่ 5: กล่องแสดงสถานะ (Log) ---
        log_label = wx.StaticText(panel, label="บันทึกการทำงาน (&Log):")
        self.log_box = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL,
            name="บันทึกการทำงาน",
        )

        main_sizer.Add(log_label, 0, wx.LEFT | wx.RIGHT, 10)
        main_sizer.Add(self.log_box, 1, wx.EXPAND | wx.ALL, 10)

        panel.SetSizer(main_sizer)

    def setup_accelerators(self):
        # คีย์ลัด Alt+S เพื่อเริ่ม และ Alt+X เพื่อหยุด
        start_id = wx.NewIdRef()
        stop_id = wx.NewIdRef()

        self.Bind(wx.EVT_MENU, self.on_start, id=start_id)
        self.Bind(wx.EVT_MENU, self.on_stop, id=stop_id)

        accel_tbl = wx.AcceleratorTable([
            (wx.ACCEL_ALT, ord("S"), start_id),
            (wx.ACCEL_ALT, ord("X"), stop_id),
        ])
        self.SetAcceleratorTable(accel_tbl)

    def log(self, message):
        wx.CallAfter(self._append_log, message)

    def _append_log(self, message):
        if self.log_box:
            self.log_box.AppendText(message + "\n")

    def on_toggle_random(self, event):
        if self.random_cb.GetValue():
            self.voice_choice.Disable()
        else:
            self.voice_choice.Enable()

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

    # --- โหลดเสียง Multilingual ทั้งหมด ---
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
                wx.CallAfter(self._update_voice_choices)
                self.log(
                    f"โหลดรายชื่อเสียงสำเร็จ ({len(self.voice_list)} เสียงที่พูดไทยได้)"
                )
        except Exception as e:
            self.log(f"โหลดรายชื่อเสียงไม่สำเร็จ (ใช้เสียงพื้นฐานแทน): {e}")

    def _update_voice_choices(self):
        self.voice_choice.SetItems(self.voice_list)
        if "th-TH-PremwadeeNeural" in self.voice_list:
            idx = self.voice_list.index("th-TH-PremwadeeNeural")
            self.voice_choice.SetSelection(idx)
        else:
            self.voice_choice.SetSelection(0)

    # --- เริ่ม / หยุด การทำงาน ---
    def on_start(self, event):
        if self.is_running:
            return

        raw_input = self.url_entry.GetValue()
        video_id = self.extract_video_id(raw_input)
        if not video_id:
            wx.MessageBox(
                "กรุณาใส่ YouTube Live URL หรือ Video ID ให้ถูกต้อง",
                "รูปแบบลิงก์ไม่ถูกต้อง",
                wx.OK | wx.ICON_WARNING,
                parent=self,
            )
            self.url_entry.SetFocus()
            return

        self.is_running = True
        self.start_btn.Disable()
        self.stop_btn.Enable()
        self.url_entry.Disable()
        self.stop_btn.SetFocus()

        self.log(f"กำลังเชื่อมต่อกับแชทของ Video ID: {video_id} ...")

        threading.Thread(
            target=self.chat_listener_thread, args=(video_id,), daemon=True
        ).start()

    def on_stop(self, event=None):
        if not self.is_running:
            return

        self.is_running = False
        with self.speech_queue.mutex:
            self.speech_queue.queue.clear()
        pygame.mixer.music.stop()

        wx.CallAfter(self._reset_controls_after_stop)
        self.log("หยุดการทำงานแล้ว")

    def _reset_controls_after_stop(self):
        self.start_btn.Enable()
        self.stop_btn.Disable()
        self.url_entry.Enable()
        self.start_btn.SetFocus()

    # --- ตัวดึงแชทสด ---
    def chat_listener_thread(self, video_id):
        try:
            # ใส่ interruptable=False เพื่อป้องกัน Error: signal only works in main thread
            chat = pytchat.create(video_id=video_id, interruptable=False)
            self.log("เชื่อมต่อแชทสำเร็จ! กำลังรอรับข้อความ...")

            while self.is_running and chat.is_alive():
                for c in chat.get().sync_items():
                    if not self.is_running:
                        break

                    msg_type = c.type
                    author = (c.author.name or "").strip()
                    msg = (c.message or "").strip()
                    text_to_speak = None

                    if msg_type == "textMessage" and self.read_normal_cb.GetValue():
                        if msg:
                            text_to_speak = f"{author} พูดว่า {msg}"

                    elif msg_type == "superChat" and self.read_superchat_cb.GetValue():
                        amount = c.amountString
                        text_to_speak = (
                            f"ขอบคุณคุณ {author} สำหรับซุปเปอร์แชท {amount} ครับ {msg}"
                        ).strip()

                    elif msg_type == "superSticker" and self.read_sticker_cb.GetValue():
                        amount = c.amountString
                        text_to_speak = (
                            f"ขอบคุณคุณ {author} สำหรับซุปเปอร์สติกเกอร์ {amount} ครับ"
                        )

                    elif msg_type == "newSponsor" and self.read_member_cb.GetValue():
                        text_to_speak = (
                            f"ยินดีต้อนรับคุณ {author} สู่การเป็นสมาชิกใหม่ครับ {msg}"
                        ).strip()

                    if text_to_speak:
                        self.log(f"[{msg_type}] {text_to_speak}")
                        self.speech_queue.put(text_to_speak)

                time.sleep(0.5)

            if self.is_running:
                self.log("ไลฟ์สดจบลงแล้ว หรือการเชื่อมต่อถูกตัด")
                self.on_stop()

        except Exception as e:
            self.log(f"เกิดข้อผิดพลาดในการดึงแชท: {e}")
            self.on_stop()

    # --- ตัวจัดการคิวและเล่นเสียง ---
    def audio_worker(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while True:
            text = self.speech_queue.get()
            if not self.is_running or not text.strip():
                self.speech_queue.task_done()
                continue

            try:
                if self.random_cb.GetValue():
                    voice = random.choice(self.voice_list)
                else:
                    sel_idx = self.voice_choice.GetSelection()
                    voice = (
                        self.voice_list[sel_idx]
                        if sel_idx != wx.NOT_FOUND
                        else self.voice_list[0]
                    )

                temp_file = os.path.join(
                    tempfile.gettempdir(), f"tts_{random.randint(10000,99999)}.mp3"
                )

                communicate = edge_tts.Communicate(text, voice)
                loop.run_until_complete(communicate.save(temp_file))

                if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
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

    def on_close(self, event):
        self.is_running = False
        pygame.mixer.quit()
        self.Destroy()


if __name__ == "__main__":
    app = wx.App(False)
    frame = YouTubeTTSFrame()
    frame.Show()
    app.MainLoop()
