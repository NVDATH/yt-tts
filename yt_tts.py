# ==============================================================================
# YouTube Live TTS Reader (Accessible wxPython Edition - Sec-MS-GEC v2 Fixed)
# ==============================================================================

import wx
import threading
import queue
import asyncio
import random
import re
import os
import time
import tempfile
import hashlib
import uuid
from datetime import datetime, timezone
import pytchat
import edge_tts
from gtts import gTTS
import pygame

# ตรวจสอบเวอร์ชัน edge-tts และบังคับอัปเดตค่าคงที่ให้รองรับ Sec-MS-GEC v2 (ถ้าจำเป็น)
try:
    import edge_tts.constants as edge_const
    import edge_tts.drm as edge_drm
    # ตรวจสอบว่า edge-tts ที่ติดตั้งมาเป็นรุ่น v2 หรือยัง (รุ่น 7.3.0+ จะมี SEC_MS_GEC_VERSION ขึ้นต้นด้วย 2-)
    if hasattr(edge_const, "SEC_MS_GEC_VERSION") and not str(edge_const.SEC_MS_GEC_VERSION).startswith("2-"):
        edge_const.SEC_MS_GEC_VERSION = "2-143.0.3650.75"
except Exception:
    pass


class YouTubeTTSFrame(wx.Frame):
    def __init__(self):
        super().__init__(
            parent=None,
            title="YouTube Live TTS Reader",
            size=(580, 680),
            style=wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX)
        )

        self.is_running = False
        self.speech_queue = queue.Queue()
        self.voice_list = ["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"]

        pygame.mixer.init()

        self.init_ui()
        self.Centre()
        self.Show()

        self.url_ctrl.SetFocus()

        threading.Thread(target=self.load_voices_thread, daemon=True).start()
        threading.Thread(target=self.audio_worker, daemon=True).start()

    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # --- ส่วนที่ 1: URL ---
        url_box = wx.StaticBox(panel, label="การเชื่อมต่อ YouTube Live")
        url_sizer = wx.StaticBoxSizer(url_box, wx.VERTICAL)

        url_label = wx.StaticText(panel, label="YouTube Live &URL หรือ Video ID:")
        self.url_ctrl = wx.TextCtrl(panel, name="YouTube Live URL หรือ Video ID")

        url_sizer.Add(url_label, 0, wx.ALL, 5)
        url_sizer.Add(self.url_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        main_sizer.Add(url_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # --- ส่วนที่ 2: ตั้งค่าเสียง ---
        voice_box = wx.StaticBox(panel, label="ตั้งค่าเสียงอ่าน (TTS)")
        voice_sizer = wx.StaticBoxSizer(voice_box, wx.VERTICAL)

        self.random_cb = wx.CheckBox(
            panel,
            label="&สุ่มเสียงอัตโนมัติ (จากเสียงไทย และ Multilingual)",
            name="สุ่มเสียงอัตโนมัติ"
        )
        self.random_cb.Bind(wx.EVT_CHECKBOX, self.on_toggle_random)

        voice_select_label = wx.StaticText(panel, label="เลือกเสียงพูดหลัก:")
        self.voice_choice = wx.Choice(panel, choices=self.voice_list, name="เลือกเสียงพูดหลัก")
        self.voice_choice.SetSelection(0)

        self.fallback_cb = wx.CheckBox(
            panel,
            label="ใช้เสียงสำรอง Google TTS อัตโนมัติหากเซิร์ฟเวอร์ Microsoft ขัดข้อง",
            name="ใช้เสียงสำรอง Google TTS"
        )
        self.fallback_cb.SetValue(True)

        voice_sizer.Add(self.random_cb, 0, wx.ALL, 5)
        voice_sizer.Add(voice_select_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 5)
        voice_sizer.Add(self.voice_choice, 0, wx.EXPAND | wx.ALL, 5)
        voice_sizer.Add(self.fallback_cb, 0, wx.ALL, 5)
        main_sizer.Add(voice_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- ส่วนที่ 3: หมวดข้อความ ---
        filter_box = wx.StaticBox(panel, label="หมวดข้อความที่จะให้อ่านออกเสียง")
        filter_sizer = wx.StaticBoxSizer(filter_box, wx.VERTICAL)

        self.cb_normal = wx.CheckBox(panel, label="ข้อความแชทปกติ (Normal Chat)", name="ข้อความแชทปกติ")
        self.cb_superchat = wx.CheckBox(panel, label="Super Chat (โดเนทข้อความ)", name="Super Chat")
        self.cb_sticker = wx.CheckBox(panel, label="Super Sticker (โดเนทสติกเกอร์)", name="Super Sticker")
        self.cb_member = wx.CheckBox(panel, label="New Member / Milestone (สมัครสมาชิกใหม่)", name="New Member")

        self.cb_superchat.SetValue(True)
        self.cb_sticker.SetValue(True)
        self.cb_member.SetValue(True)

        filter_sizer.Add(self.cb_normal, 0, wx.ALL, 4)
        filter_sizer.Add(self.cb_superchat, 0, wx.ALL, 4)
        filter_sizer.Add(self.cb_sticker, 0, wx.ALL, 4)
        filter_sizer.Add(self.cb_member, 0, wx.ALL, 4)
        main_sizer.Add(filter_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- ส่วนที่ 4: ปุ่มควบคุม ---
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.start_btn = wx.Button(panel, label="เริ่มอ่านแชท (&S)", name="เริ่มอ่านแชท")
        self.stop_btn = wx.Button(panel, label="หยุดทำงาน (&X)", name="หยุดทำงาน")
        self.test_btn = wx.Button(panel, label="ทดสอบเสียง (&T)", name="ทดสอบเสียง")
        self.stop_btn.Disable()

        self.start_btn.Bind(wx.EVT_BUTTON, self.on_start)
        self.stop_btn.Bind(wx.EVT_BUTTON, self.on_stop)
        self.test_btn.Bind(wx.EVT_BUTTON, self.on_test_voice)

        btn_sizer.Add(self.start_btn, 1, wx.EXPAND | wx.RIGHT, 5)
        btn_sizer.Add(self.stop_btn, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)
        btn_sizer.Add(self.test_btn, 1, wx.EXPAND | wx.LEFT, 5)
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- ส่วนที่ 5: Log ---
        log_label = wx.StaticText(panel, label="บันทึกการทำงาน (&Log):")
        self.log_ctrl = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
            name="บันทึกการทำงาน"
        )
        main_sizer.Add(log_label, 0, wx.LEFT | wx.RIGHT, 10)
        main_sizer.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, 10)

        panel.SetSizer(main_sizer)

        accel_tbl = wx.AcceleratorTable([
            (wx.ACCEL_ALT, ord('S'), self.start_btn.GetId()),
            (wx.ACCEL_ALT, ord('X'), self.stop_btn.GetId()),
            (wx.ACCEL_ALT, ord('T'), self.test_btn.GetId()),
        ])
        self.SetAcceleratorTable(accel_tbl)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def append_log(self, message):
        wx.CallAfter(self._safe_append_log, message)

    def _safe_append_log(self, message):
        if self.log_ctrl:
            self.log_ctrl.AppendText(message + "\n")

    def on_toggle_random(self, event):
        if self.random_cb.GetValue():
            self.voice_choice.Disable()
        else:
            self.voice_choice.Enable()

    def extract_video_id(self, url):
        patterns = [
            r"(?:v=|\/live\/|youtu\.be\/)([0-9A-Za-z_-]{11})",
            r"^([0-9A-Za-z_-]{11})$"
        ]
        for p in patterns:
            match = re.search(p, url.strip())
            if match:
                return match.group(1)
        return None

    def clean_text_for_tts(self, text):
        if not text:
            return ""
        cleaned = re.sub(r"[^\u0E00-\u0E7Fa-zA-Z0-9\s.,!?\-()\"\']", " ", text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def load_voices_thread(self):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            voices = loop.run_until_complete(edge_tts.list_voices())
            filtered = [
                v["ShortName"] for v in voices
                if v["Locale"].startswith("th-TH") or "Multilingual" in v["ShortName"]
            ]
            if filtered:
                self.voice_list = sorted(filtered)
                wx.CallAfter(self._update_voice_choice)
                self.append_log(f"โหลดรายชื่อเสียงสำเร็จ ({len(self.voice_list)} เสียงที่รองรับภาษาไทย)")
        except Exception as e:
            self.append_log(f"โหลดรายชื่อเสียงออนไลน์ไม่สำเร็จ (ใช้เสียงพื้นฐาน): {e}")

    def _update_voice_choice(self):
        self.voice_choice.SetItems(self.voice_list)
        if "th-TH-PremwadeeNeural" in self.voice_list:
            self.voice_choice.SetStringSelection("th-TH-PremwadeeNeural")
        else:
            self.voice_choice.SetSelection(0)

    def on_test_voice(self, event):
        test_text = "สวัสดีครับ ทดสอบระบบอ่านออกเสียงภาษาไทย"
        self.append_log(f"กำลังทดสอบเสียง: {test_text}")
        self.speech_queue.put((test_text, True))

    def on_start(self, event):
        raw_input = self.url_ctrl.GetValue()
        video_id = self.extract_video_id(raw_input)
        if not video_id:
            wx.MessageBox(
                "รูปแบบ YouTube Live URL หรือ Video ID ไม่ถูกต้อง กรุณาตรวจสอบอีกครั้ง",
                "ข้อมูลไม่ถูกต้อง",
                wx.OK | wx.ICON_WARNING
            )
            self.url_ctrl.SetFocus()
            return

        self.is_running = True
        self.start_btn.Disable()
        self.url_ctrl.Disable()
        self.stop_btn.Enable()
        self.stop_btn.SetFocus()

        self.append_log(f"กำลังเชื่อมต่อกับแชทของ Video ID: {video_id} ...")
        threading.Thread(target=self.chat_listener_thread, args=(video_id,), daemon=True).start()

    def on_stop(self, event=None):
        self.is_running = False
        with self.speech_queue.mutex:
            self.speech_queue.queue.clear()
        if pygame.mixer.music.get_busy():
            pygame.mixer.music.stop()

        wx.CallAfter(self._reset_controls_after_stop)
        self.append_log("หยุดการทำงานแล้ว")

    def _reset_controls_after_stop(self):
        self.start_btn.Enable()
        self.url_ctrl.Enable()
        self.stop_btn.Disable()
        self.start_btn.SetFocus()

    def chat_listener_thread(self, video_id):
        try:
            chat = pytchat.create(video_id=video_id, interruptable=False)
            self.append_log("เชื่อมต่อแชทสำเร็จ กำลังรอรับข้อความ...")

            while self.is_running and chat.is_alive():
                for c in chat.get().sync_items():
                    if not self.is_running:
                        break

                    msg_type = c.type
                    author = c.author.name
                    msg = c.message
                    text_to_speak = None

                    if msg_type == "textMessage" and self.cb_normal.GetValue():
                        text_to_speak = f"{author} พูดว่า {msg}"
                    elif msg_type == "superChat" and self.cb_superchat.GetValue():
                        amount = c.amountString
                        text_to_speak = f"ขอบคุณคุณ {author} สำหรับซุปเปอร์แชท {amount} ครับ {msg}"
                    elif msg_type == "superSticker" and self.cb_sticker.GetValue():
                        amount = c.amountString
                        text_to_speak = f"ขอบคุณคุณ {author} สำหรับซุปเปอร์สติกเกอร์ {amount} ครับ"
                    elif msg_type == "newSponsor" and self.cb_member.GetValue():
                        text_to_speak = f"ยินดีต้อนรับคุณ {author} สู่การเป็นสมาชิกใหม่ครับ {msg}"

                    if text_to_speak:
                        self.append_log(f"[{msg_type}] {text_to_speak}")
                        self.speech_queue.put((text_to_speak, False))

                time.sleep(0.5)

            if self.is_running:
                self.append_log("ไลฟ์สดจบลงแล้ว หรือการเชื่อมต่อถูกตัด")
                self.on_stop()

        except Exception as e:
            self.append_log(f"เกิดข้อผิดพลาดในการดึงแชท: {e}")
            self.on_stop()

    async def _synthesize_edge_official(self, text, voice, out_path):
        """ใช้ไลบรารี edge-tts รุ่น 7.3.1+ ที่มีอัลกอริทึม Sec-MS-GEC v2 ตัวจริง"""
        communicate = edge_tts.Communicate(text, voice)
        audio_bytes = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes.extend(chunk["data"])
        if len(audio_bytes) == 0:
            raise RuntimeError("ได้รับข้อมูลเสียงว่างเปล่าจากเซิร์ฟเวอร์")
        with open(out_path, "wb") as f:
            f.write(audio_bytes)

    def synthesize_audio(self, text, temp_file):
        clean_text = self.clean_text_for_tts(text)
        if not clean_text:
            return None

        if self.random_cb.GetValue() and self.voice_list:
            selected_voice = random.choice(self.voice_list)
        else:
            sel_idx = self.voice_choice.GetSelection()
            selected_voice = self.voice_choice.GetString(sel_idx) if sel_idx != wx.NOT_FOUND else "th-TH-PremwadeeNeural"

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # ชั้นที่ 1: ใช้ edge-tts รุ่นล่าสุด (7.3.1+) ด้วยเสียงที่เลือก
        try:
            loop.run_until_complete(self._synthesize_edge_official(clean_text, selected_voice, temp_file))
            if os.path.exists(temp_file) and os.path.getsize(temp_file) > 100:
                return f"Edge TTS ({selected_voice})"
        except Exception as e1:
            self.append_log(f"Edge TTS ({selected_voice}) ขัดข้อง: {e1}")

        # ชั้นที่ 2: ลองสลับเป็น th-TH-PremwadeeNeural (กรณีเสียง Multilingual บางตัวไม่รองรับคำนั้น)
        if selected_voice != "th-TH-PremwadeeNeural":
            try:
                loop.run_until_complete(self._synthesize_edge_official(clean_text, "th-TH-PremwadeeNeural", temp_file))
                if os.path.exists(temp_file) and os.path.getsize(temp_file) > 100:
                    return "Edge TTS (th-TH-PremwadeeNeural)"
            except Exception as e2:
                self.append_log(f"Edge TTS (Premwadee) ขัดข้อง: {e2}")

        # ชั้นที่ 3: สำรองด้วย Google TTS (gTTS)
        if self.fallback_cb.GetValue():
            try:
                tts = gTTS(text=clean_text, lang="th")
                tts.save(temp_file)
                if os.path.exists(temp_file) and os.path.getsize(temp_file) > 100:
                    return "gTTS (เสียงสำรอง)"
            except Exception as e3:
                self.append_log(f"gTTS ไม่สำเร็จ: {e3}")

        return None

    def audio_worker(self):
        while True:
            item = self.speech_queue.get()
            text, is_test = item

            if not self.is_running and not is_test:
                self.speech_queue.task_done()
                continue

            temp_file = os.path.join(
                tempfile.gettempdir(),
                f"yt_tts_{random.randint(100000, 999999)}.mp3"
            )

            try:
                used_engine = self.synthesize_audio(text, temp_file)
                if used_engine and os.path.exists(temp_file):
                    if is_test:
                        self.append_log(f"กำลังเล่นเสียงทดสอบผ่าน: {used_engine}")
                    pygame.mixer.music.load(temp_file)
                    pygame.mixer.music.play()
                    while pygame.mixer.music.get_busy() and (self.is_running or is_test):
                        time.sleep(0.1)
                    pygame.mixer.music.unload()
                else:
                    self.append_log("ไม่สามารถสังเคราะห์เสียงได้ในขณะนี้")
            except Exception as e:
                self.append_log(f"เกิดข้อผิดพลาดขณะเล่นเสียง: {e}")
            finally:
                try:
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                except Exception:
                    pass
                self.speech_queue.task_done()

    def on_close(self, event):
        self.is_running = False
        pygame.mixer.quit()
        self.Destroy()


if __name__ == "__main__":
    app = wx.App(False)
    frame = YouTubeTTSFrame()
    app.MainLoop()
