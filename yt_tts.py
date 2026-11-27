import asyncio
import os
import queue
import random
import re
import tempfile
import threading
import time
import wx
import edge_tts
import pygame
import pytchat

# เสียง Multilingual ที่ทดสอบแล้วว่าอ่านภาษาไทยได้เสถียร (คัดเฉพาะตัวที่รองรับจริง)
SAFE_THAI_VOICES = [
    "th-TH-PremwadeeNeural",
    "th-TH-NiwatNeural",
    "en-US-AndrewMultilingualNeural",
    "en-US-AvaMultilingualNeural",
    "en-US-BrianMultilingualNeural",
    "en-US-EmmaMultilingualNeural",
    "de-DE-FlorianMultilingualNeural",
    "de-DE-SeraphinaMultilingualNeural",
    "fr-FR-RemyMultilingualNeural",
    "fr-FR-VivienneMultilingualNeural",
    "ja-JP-MasaruMultilingualNeural",
    "ko-KR-HyunsuMultilingualNeural",
]

def clean_text_for_tts(text):
    # ลบอีโมจิและอักขระพิเศษที่อาจทำให้ edge-tts ปฏิเสธการสร้างเสียง (NoAudioReceived)
    # คงไว้เฉพาะภาษาไทย, อังกฤษ, ตัวเลข, เว้นวรรค และเครื่องหมายวรรคตอนพื้นฐาน
    cleaned = re.sub(r"[^\u0E00-\u0E7Fa-zA-Z0-9\s.,!?()\-]", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


class YouTubeTTSFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="YouTube Live TTS Reader (NVDA Accessible)", size=(580, 650))

        self.is_running = False
        self.speech_queue = queue.Queue()
        self.voice_list = list(SAFE_THAI_VOICES)

        pygame.mixer.init()

        self.panel = wx.Panel(self)
        self.setup_ui()
        self.setup_accelerators()
        self.Centre()

        # โหลดรายชื่อเสียงเพิ่มเติมในพื้นหลัง
        threading.Thread(target=self.load_voices_thread, daemon=True).start()
        # เริ่ม Worker รออ่านคิวเสียง
        threading.Thread(target=self.audio_worker, daemon=True).start()

        self.Bind(wx.EVT_CLOSE, self.on_close)

    def setup_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # --- ส่วนที่ 1: ช่องใส่ URL ---
        url_box = wx.StaticBox(self.panel, label="การเชื่อมต่อไลฟ์สด")
        url_sizer = wx.StaticBoxSizer(url_box, wx.VERTICAL)

        url_label = wx.StaticText(self.panel, label="YouTube Live &URL หรือ Video ID:")
        self.url_entry = wx.TextCtrl(self.panel)
        self.url_entry.SetName("YouTube Live URL หรือ Video ID")

        url_sizer.Add(url_label, 0, wx.ALL, 5)
        url_sizer.Add(self.url_entry, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        main_sizer.Add(url_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # --- ส่วนที่ 2: ตั้งค่าเสียง ---
        voice_box = wx.StaticBox(self.panel, label="ตั้งค่าเสียงอ่าน")
        voice_sizer = wx.StaticBoxSizer(voice_box, wx.VERTICAL)

        self.random_cb = wx.CheckBox(self.panel, label="&สุ่มเสียงอัตโนมัติ (จากเสียงไทยและ Multilingual)")
        self.random_cb.Bind(wx.EVT_CHECKBOX, self.on_toggle_random)

        voice_select_label = wx.StaticText(self.panel, label="เลือกเสียงอ่านหลัก:")
        self.voice_choice = wx.Choice(self.panel, choices=self.voice_list)
        self.voice_choice.SetName("เลือกเสียงอ่านหลัก")
        self.voice_choice.SetSelection(0)

        voice_sizer.Add(self.random_cb, 0, wx.ALL, 5)
        voice_sizer.Add(voice_select_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 5)
        voice_sizer.Add(self.voice_choice, 0, wx.EXPAND | wx.ALL, 8)
        main_sizer.Add(voice_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- ส่วนที่ 3: เลือกหมวดข้อความที่จะให้อ่าน ---
        filter_box = wx.StaticBox(self.panel, label="เลือกหมวดข้อความที่จะให้อ่านออกเสียง")
        filter_sizer = wx.StaticBoxSizer(filter_box, wx.VERTICAL)

        self.read_normal_cb = wx.CheckBox(self.panel, label="ข้อความแชท&ปกติ (Normal Chat)")
        self.read_superchat_cb = wx.CheckBox(self.panel, label="Super &Chat (โดเนทข้อความ)")
        self.read_sticker_cb = wx.CheckBox(self.panel, label="Super S&ticker (โดเนทสติกเกอร์)")
        self.read_member_cb = wx.CheckBox(self.panel, label="New &Member (สมัครสมาชิกใหม่)")

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

        self.start_btn = wx.Button(self.panel, label="เริ่มอ่านแชท (&Start) [Alt+S]")
        self.stop_btn = wx.Button(self.panel, label="หยุดทำงาน (&Stop) [Alt+X]")
        self.stop_btn.Disable()

        self.start_btn.Bind(wx.EVT_BUTTON, self.on_start)
        self.stop_btn.Bind(wx.EVT_BUTTON, self.on_stop)

        btn_sizer.Add(self.start_btn, 1, wx.RIGHT, 5)
        btn_sizer.Add(self.stop_btn, 1, wx.LEFT, 5)
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- ส่วนที่ 5: กล่องแสดงสถานะ (Log) ---
        log_label = wx.StaticText(self.panel, label="บันทึกการทำงาน (&Log):")
        self.log_box = wx.TextCtrl(
            self.panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2
        )
        self.log_box.SetName("บันทึกการทำงาน Log")

        main_sizer.Add(log_label, 0, wx.LEFT | wx.RIGHT, 10)
        main_sizer.Add(self.log_box, 1, wx.EXPAND | wx.ALL, 10)

        self.panel.SetSizer(main_sizer)
        self.url_entry.SetFocus()

    def setup_accelerators(self):
        start_id = wx.NewIdRef()
        stop_id = wx.NewIdRef()
        self.Bind(wx.EVT_MENU, self.on_start, id=start_id)
        self.Bind(wx.EVT_MENU, self.on_stop, id=stop_id)

        accel_tbl = wx.AcceleratorTable([
            (wx.ACCEL_ALT, ord('S'), start_id),
            (wx.ACCEL_ALT, ord('X'), stop_id),
        ])
        self.SetAcceleratorTable(accel_tbl)

    def log(self, message):
        def _append():
            if self.log_box:
                self.log_box.AppendText(message + "\n")
        wx.CallAfter(_append)

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

    def load_voices_thread(self):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            voices = loop.run_until_complete(edge_tts.list_voices())
            # กรองเฉพาะเสียงที่ขึ้นต้นด้วย th-TH หรือมี Multilingual และไม่ใช่เสียงรุ่นเก่าที่เลิกใช้
            filtered = [
                v["ShortName"] for v in voices
                if (v["Locale"].startswith("th-TH") or "Multilingual" in v["ShortName"])
                and "Neural" in v["ShortName"]
            ]
            if filtered:
                # รวมกับรายการปลอดภัยและเรียงลำดับ
                combined = sorted(list(set(filtered + SAFE_THAI_VOICES)))
                def _update_choices():
                    current = self.voice_choice.GetStringSelection()
                    self.voice_list = combined
                    self.voice_choice.SetItems(self.voice_list)
                    if current in self.voice_list:
                        self.voice_choice.SetStringSelection(current)
                    elif "th-TH-PremwadeeNeural" in self.voice_list:
                        self.voice_choice.SetStringSelection("th-TH-PremwadeeNeural")
                    else:
                        self.voice_choice.SetSelection(0)
                wx.CallAfter(_update_choices)
                self.log(f"โหลดรายชื่อเสียงสำเร็จ ({len(combined)} เสียง)")
        except Exception as e:
            self.log(f"ใช้รายชื่อเสียงมาตรฐาน ({len(self.voice_list)} เสียง): {e}")

    def on_start(self, event):
        if self.is_running:
            return
        raw_input = self.url_entry.GetValue()
        video_id = self.extract_video_id(raw_input)
        if not video_id:
            wx.MessageBox("รูปแบบ URL หรือ Video ID ไม่ถูกต้อง กรุณาตรวจสอบอีกครั้ง", "ข้อผิดพลาด", wx.OK | wx.ICON_ERROR)
            self.url_entry.SetFocus()
            return

        self.is_running = True
        self.start_btn.Disable()
        self.stop_btn.Enable()
        self.url_entry.Disable()
        self.stop_btn.SetFocus()
        self.log(f"กำลังเชื่อมต่อกับแชทของ Video ID: {video_id} ...")

        threading.Thread(target=self.chat_listener_thread, args=(video_id,), daemon=True).start()

    def on_stop(self, event):
        if not self.is_running:
            return
        self.is_running = False
        with self.speech_queue.mutex:
            self.speech_queue.queue.clear()
        pygame.mixer.music.stop()

        self.start_btn.Enable()
        self.stop_btn.Disable()
        self.url_entry.Enable()
        self.start_btn.SetFocus()
        self.log("หยุดการทำงานแล้ว")

    def reset_ui_on_disconnect(self):
        def _reset():
            self.is_running = False
            self.start_btn.Enable()
            self.stop_btn.Disable()
            self.url_entry.Enable()
        wx.CallAfter(_reset)

    def chat_listener_thread(self, video_id):
        try:
            chat = pytchat.create(video_id=video_id, interruptable=False)
            self.log("เชื่อมต่อแชทสำเร็จ! กำลังรอรับข้อความ...")

            while self.is_running and chat.is_alive():
                for c in chat.get().sync_items():
                    if not self.is_running:
                        break

                    msg_type = c.type
                    author = c.author.name
                    msg = c.message
                    text_to_speak = None

                    if msg_type == "textMessage" and self.read_normal_cb.GetValue():
                        text_to_speak = f"{author} พูดว่า {msg}"
                    elif msg_type == "superChat" and self.read_superchat_cb.GetValue():
                        amount = c.amountString
                        text_to_speak = f"ขอบคุณคุณ {author} สำหรับซุปเปอร์แชท {amount} ครับ {msg}"
                    elif msg_type == "superSticker" and self.read_sticker_cb.GetValue():
                        amount = c.amountString
                        text_to_speak = f"ขอบคุณคุณ {author} สำหรับซุปเปอร์สติกเกอร์ {amount} ครับ"
                    elif msg_type == "newSponsor" and self.read_member_cb.GetValue():
                        text_to_speak = f"ยินดีต้อนรับคุณ {author} สู่การเป็นสมาชิกใหม่ครับ {msg}"

                    if text_to_speak:
                        self.log(f"[{msg_type}] {text_to_speak}")
                        self.speech_queue.put(text_to_speak)

                time.sleep(0.5)

            if self.is_running:
                self.log("ไลฟ์สดจบลงแล้ว หรือการเชื่อมต่อถูกตัด")
                self.reset_ui_on_disconnect()

        except Exception as e:
            self.log(f"เกิดข้อผิดพลาดในการดึงแชท: {e}")
            self.reset_ui_on_disconnect()

    async def synthesize_with_fallback(self, text, preferred_voice):
        # ลองสร้างเสียงด้วยเสียงที่เลือกก่อน หากไม่ได้รับไฟล์เสียง (NoAudioReceived) ให้สลับไปใช้เสียงไทยหลักทันที
        voices_to_try = [preferred_voice]
        if preferred_voice != "th-TH-PremwadeeNeural":
            voices_to_try.append("th-TH-PremwadeeNeural")

        last_error = None
        for v in voices_to_try:
            temp_file = os.path.join(tempfile.gettempdir(), f"tts_{random.randint(10000,99999)}.mp3")
            try:
                communicate = edge_tts.Communicate(text, v)
                await communicate.save(temp_file)
                if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
                    return temp_file, v
            except Exception as e:
                last_error = e
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except OSError:
                        pass
        raise last_error or RuntimeError("ไม่สามารถสร้างไฟล์เสียงได้")

    def audio_worker(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while True:
            raw_text = self.speech_queue.get()
            if not self.is_running:
                self.speech_queue.task_done()
                continue

            clean_text = clean_text_for_tts(raw_text)
            if not clean_text:
                self.speech_queue.task_done()
                continue

            try:
                if self.random_cb.GetValue():
                    chosen_voice = random.choice(self.voice_list)
                else:
                    chosen_voice = self.voice_choice.GetStringSelection()

                temp_file, used_voice = loop.run_until_complete(
                    self.synthesize_with_fallback(clean_text, chosen_voice)
                )

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


class YouTubeTTSApp(wx.App):
    def OnInit(self):
        frame = YouTubeTTSFrame()
        frame.Show()
        return True


if __name__ == "__main__":
    app = YouTubeTTSApp(False)
    app.MainLoop()
