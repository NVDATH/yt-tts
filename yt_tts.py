# ==============================================================================
# YouTube Live TTS Reader (NVDA / Screen Reader Friendly - wxPython Edition)
# ==============================================================================
# วิธีติดตั้งไลบรารีสำหรับรันทดสอบเอง:
#   pip install wxPython pytchat edge-tts==7.3.1 gTTS pygame certifi
#
# คำสั่งบีบอัดเป็นไฟล์ .exe เดียวจบ:
#   pyinstaller --noconsole --onefile --collect-all certifi --name yt_tts yt_tts.py
# ==============================================================================

import asyncio
import os
import queue
import random
import re
import ssl
import tempfile
import threading
import time
import certifi
import edge_tts
from gtts import gTTS
import pygame
import pytchat
import wx

# บังคับใช้ SSL Certificate ของ certifi เพื่อป้องกันปัญหา HTTPS/WSS บน Windows Standalone
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()


class YouTubeTTSFrame(wx.Frame):
    def __init__(self):
        super().__init__(
            None,
            title="YouTube Live TTS Reader",
            size=(560, 660),
            style=wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX),
        )

        # ตัวแปรควบคุมระบบ
        self.is_running = False
        self.speech_queue = queue.Queue()
        self.voice_list = ["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"]

        # เริ่มระบบเสียง Pygame
        pygame.mixer.init()

        self.init_ui()
        self.setup_accelerators()
        self.Centre()

        # โฟกัสที่ช่องใส่ URL ทันทีเมื่อเปิดโปรแกรม
        self.url_text.SetFocus()

        # โหลดรายชื่อเสียง Multilingual ในพื้นหลัง
        threading.Thread(target=self.load_voices_thread, daemon=True).start()
        # เริ่ม Worker รออ่านคิวเสียง
        threading.Thread(target=self.audio_worker, daemon=True).start()

        self.Bind(wx.EVT_CLOSE, self.on_close)

    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # --- ส่วนที่ 1: ช่องใส่ URL ---
        url_box = wx.StaticBox(panel, label="การเชื่อมต่อไลฟ์สด")
        url_sizer = wx.StaticBoxSizer(url_box, wx.VERTICAL)

        url_lbl = wx.StaticText(panel, label="YouTube Live &URL หรือ Video ID:")
        self.url_text = wx.TextCtrl(panel, name="YouTube Live URL หรือ Video ID")
        self.url_text.SetHint("วางลิงก์ไลฟ์สดหรือรหัส Video ID 11 หลักที่นี่")

        url_sizer.Add(url_lbl, 0, wx.ALL, 5)
        url_sizer.Add(self.url_text, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        main_sizer.Add(url_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # --- ส่วนที่ 2: ตั้งค่าเสียง ---
        voice_box = wx.StaticBox(panel, label="ตั้งค่าเสียงอ่าน (TTS)")
        voice_sizer = wx.StaticBoxSizer(voice_box, wx.VERTICAL)

        self.random_voice_cb = wx.CheckBox(
            panel, label="&สุ่มเสียงอัตโนมัติ (จากเสียงไทย และ Multilingual)"
        )
        self.random_voice_cb.Bind(wx.EVT_CHECKBOX, self.on_toggle_random_voice)

        voice_lbl = wx.StaticText(panel, label="&เลือกเสียงอ่านหลัก:")
        self.voice_choice = wx.Choice(
            panel, choices=self.voice_list, name="เลือกเสียงอ่านหลัก"
        )
        self.voice_choice.SetSelection(0)

        self.fallback_gtts_cb = wx.CheckBox(
            panel, label="ใช้เสียงสำรอง &Google TTS (gTTS) อัตโนมัติหาก Edge ขัดข้อง"
        )
        self.fallback_gtts_cb.SetValue(True)

        voice_sizer.Add(self.random_voice_cb, 0, wx.ALL, 5)
        voice_sizer.Add(voice_lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 5)
        voice_sizer.Add(self.voice_choice, 0, wx.EXPAND | wx.ALL, 5)
        voice_sizer.Add(self.fallback_gtts_cb, 0, wx.ALL, 5)
        main_sizer.Add(voice_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- ส่วนที่ 3: เลือกหมวดข้อความที่จะให้อ่าน ---
        filter_box = wx.StaticBox(panel, label="หมวดข้อความที่จะให้อ่านออกเสียง")
        filter_sizer = wx.StaticBoxSizer(filter_box, wx.VERTICAL)

        self.read_normal_cb = wx.CheckBox(panel, label="ข้อความแชท&ปกติ (Normal Chat)")
        self.read_superchat_cb = wx.CheckBox(panel, label="Super &Chat (โดเนทข้อความ)")
        self.read_sticker_cb = wx.CheckBox(panel, label="Super Stic&ker (โดเนทสติกเกอร์)")
        self.read_member_cb = wx.CheckBox(
            panel, label="New &Member / Milestone (สมัครสมาชิกใหม่)"
        )

        # ค่าเริ่มต้นติ๊กเฉพาะตัวพิเศษ
        self.read_normal_cb.SetValue(False)
        self.read_superchat_cb.SetValue(True)
        self.read_sticker_cb.SetValue(True)
        self.read_member_cb.SetValue(True)

        filter_sizer.Add(self.read_normal_cb, 0, wx.ALL, 4)
        filter_sizer.Add(self.read_superchat_cb, 0, wx.ALL, 4)
        filter_sizer.Add(self.read_sticker_cb, 0, wx.ALL, 4)
        filter_sizer.Add(self.read_member_cb, 0, wx.ALL, 4)
        main_sizer.Add(filter_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- ส่วนที่ 4: ปุ่มควบคุม ---
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.start_btn = wx.Button(panel, label="เริ่มอ่านแชท (Alt+S)")
        self.stop_btn = wx.Button(panel, label="หยุดทำงาน (Alt+X)")
        self.test_btn = wx.Button(panel, label="ทดสอบเสียง (Alt+T)")
        self.stop_btn.Disable()

        self.start_btn.Bind(wx.EVT_BUTTON, self.on_start)
        self.stop_btn.Bind(wx.EVT_BUTTON, self.on_stop)
        self.test_btn.Bind(wx.EVT_BUTTON, self.on_test_voice)

        btn_sizer.Add(self.start_btn, 1, wx.EXPAND | wx.RIGHT, 5)
        btn_sizer.Add(self.stop_btn, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)
        btn_sizer.Add(self.test_btn, 1, wx.EXPAND | wx.LEFT, 5)
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- ส่วนที่ 5: กล่องแสดงสถานะ (Log) ---
        log_lbl = wx.StaticText(panel, label="บันทึกการทำงาน (&Log):")
        self.log_text = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
            name="บันทึกการทำงาน",
        )

        main_sizer.Add(log_lbl, 0, wx.LEFT | wx.RIGHT, 10)
        main_sizer.Add(self.log_text, 1, wx.EXPAND | wx.ALL, 10)

        panel.SetSizer(main_sizer)

    def setup_accelerators(self):
        id_start = wx.NewIdRef()
        id_stop = wx.NewIdRef()
        id_test = wx.NewIdRef()

        self.Bind(wx.EVT_MENU, self.on_start, id=id_start)
        self.Bind(wx.EVT_MENU, self.on_stop, id=id_stop)
        self.Bind(wx.EVT_MENU, self.on_test_voice, id=id_test)

        accel_tbl = wx.AcceleratorTable([
            (wx.ACCEL_ALT, ord("S"), id_start),
            (wx.ACCEL_ALT, ord("X"), id_stop),
            (wx.ACCEL_ALT, ord("T"), id_test),
        ])
        self.SetAcceleratorTable(accel_tbl)

    def log(self, message):
        wx.CallAfter(self._append_log, message)

    def _append_log(self, message):
        if self.log_text:
            self.log_text.AppendText(message + "\n")

    def on_toggle_random_voice(self, event):
        if self.random_voice_cb.GetValue():
            self.voice_choice.Disable()
        else:
            self.voice_choice.Enable()

    def extract_video_id(self, url):
        patterns = [
            r"(?:v=|\/live\/|youtu\.be\/|\/shorts\/)([0-9A-Za-z_-]{11})",
            r"^([0-9A-Za-z_-]{11})$",
        ]
        for p in patterns:
            match = re.search(p, url.strip())
            if match:
                return match.group(1)
        return None

    def clean_text_for_tts(self, text):
        if not text:
            return ""
        # ลบอักขระควบคุมและอีโมจิบางช่วงที่ทำให้ XML/SSML ของ TTS เสียรูปแบบ
        cleaned = re.sub(r"[\x00-\x1F\x7F]", " ", str(text))
        cleaned = re.sub(r"[<>"&]", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    # --- โหลดรายชื่อเสียง ---
    def load_voices_thread(self):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            voices = loop.run_until_complete(edge_tts.list_voices())
            loop.close()

            filtered = [
                v["ShortName"]
                for v in voices
                if v["Locale"].startswith("th-TH")
                or "Multilingual" in v["ShortName"]
            ]
            if filtered:
                self.voice_list = sorted(filtered)
                wx.CallAfter(self._update_voice_list)
                self.log(
                    f"โหลดรายชื่อเสียงสำเร็จ ({len(self.voice_list)} เสียงที่รองรับภาษาไทย)"
                )
        except Exception as e:
            self.log(f"โหลดรายชื่อเสียงไม่สำเร็จ (ใช้เสียงพื้นฐานแทน): {e}")

    def _update_voice_list(self):
        self.voice_choice.Clear()
        self.voice_choice.AppendItems(self.voice_list)
        if "th-TH-PremwadeeNeural" in self.voice_list:
            idx = self.voice_list.index("th-TH-PremwadeeNeural")
            self.voice_choice.SetSelection(idx)
        else:
            self.voice_choice.SetSelection(0)

    # --- ปุ่มทดสอบเสียง ---
    def on_test_voice(self, event):
        sample_text = "สวัสดีครับ ทดสอบระบบอ่านออกเสียงภาษาไทย"
        self.log(f"กำลังทดสอบเสียง: {sample_text}")
        self.speech_queue.put((sample_text, True))

    # --- เริ่ม/หยุด การทำงาน ---
    def on_start(self, event):
        if self.is_running:
            return

        raw_input = self.url_text.GetValue()
        video_id = self.extract_video_id(raw_input)
        if not video_id:
            wx.MessageBox(
                "กรุณาใส่ลิงก์ YouTube Live หรือรหัส Video ID (11 หลัก) ให้ถูกต้อง",
                "ข้อมูลไม่ถูกต้อง",
                wx.OK | wx.ICON_WARNING,
            )
            self.url_text.SetFocus()
            return

        self.is_running = True
        self.start_btn.Disable()
        self.url_text.Disable()
        self.stop_btn.Enable()
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
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass

        wx.CallAfter(self._reset_ui_stopped)
        self.log("หยุดการทำงานแล้ว")

    def _reset_ui_stopped(self):
        self.start_btn.Enable()
        self.url_text.Enable()
        self.stop_btn.Disable()
        self.start_btn.SetFocus()

    # --- ตัวดึงแชทสด ---
    def chat_listener_thread(self, video_id):
        try:
            chat = pytchat.create(video_id=video_id, interruptable=False)
            self.log("เชื่อมต่อแชทสำเร็จ! กำลังรอรับข้อความ...")

            while self.is_running and chat.is_alive():
                for c in chat.get().sync_items():
                    if not self.is_running:
                        break

                    msg_type = c.type
                    author = self.clean_text_for_tts(c.author.name)
                    msg = self.clean_text_for_tts(c.message)
                    text_to_speak = None

                    if msg_type == "textMessage" and self.read_normal_cb.GetValue():
                        if msg:
                            text_to_speak = f"{author} พูดว่า {msg}"

                    elif msg_type == "superChat" and self.read_superchat_cb.GetValue():
                        amount = getattr(c, "amountString", "")
                        text_to_speak = (
                            f"ขอบคุณคุณ {author} สำหรับซุปเปอร์แชท {amount} ครับ {msg}"
                        )

                    elif msg_type == "superSticker" and self.read_sticker_cb.GetValue():
                        amount = getattr(c, "amountString", "")
                        text_to_speak = (
                            f"ขอบคุณคุณ {author} สำหรับซุปเปอร์สติกเกอร์ {amount} ครับ"
                        )

                    elif msg_type == "newSponsor" and self.read_member_cb.GetValue():
                        text_to_speak = (
                            f"ยินดีต้อนรับคุณ {author} สู่การเป็นสมาชิกใหม่ครับ {msg}"
                        )

                    if text_to_speak:
                        clean_speech = self.clean_text_for_tts(text_to_speak)
                        if clean_speech:
                            self.log(f"[{msg_type}] {clean_speech}")
                            self.speech_queue.put((clean_speech, False))

                time.sleep(0.5)

            if self.is_running:
                self.log("ไลฟ์สดจบลงแล้ว หรือการเชื่อมต่อถูกตัด")
                self.on_stop()

        except Exception as e:
            self.log(f"เกิดข้อผิดพลาดในการดึงแชท: {e}")
            self.on_stop()

    # --- สังเคราะห์เสียงผ่าน edge-tts v7.3.1+ (Sec-MS-GEC v2 + Cookie MUID) ---
    async def _edge_save_audio(self, text, voice, out_path):
        communicate = edge_tts.Communicate(text, voice)
        audio_bytes = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes.extend(chunk["data"])
        if len(audio_bytes) == 0:
            raise RuntimeError("ได้รับข้อมูลเสียงว่างเปล่าจากเซิร์ฟเวอร์")
        with open(out_path, "wb") as f:
            f.write(audio_bytes)

    def synthesize_audio(self, text, temp_file, is_test=False):
        if self.random_voice_cb.GetValue() and self.voice_list:
            voice = random.choice(self.voice_list)
        else:
            sel = self.voice_choice.GetSelection()
            voice = (
                self.voice_list[sel]
                if 0 <= sel < len(self.voice_list)
                else "th-TH-PremwadeeNeural"
            )

        # 1. ลองใช้ edge-tts (เวอร์ชัน 7.3.1+ ที่รองรับ Sec-MS-GEC v2)
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._edge_save_audio(text, voice, temp_file))
            if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
                return f"Edge TTS ({voice})"
        except Exception as e1:
            self.log(f"Edge TTS ({voice}) ขัดข้อง: {e1}")
            # หากเสียงที่สุ่ม/เลือกไม่ใช่ Premwadee ให้ลองสลับใช้ Premwadee อีกครั้ง
            if voice != "th-TH-PremwadeeNeural":
                try:
                    loop.run_until_complete(
                        self._edge_save_audio(text, "th-TH-PremwadeeNeural", temp_file)
                    )
                    if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
                        return "Edge TTS (th-TH-PremwadeeNeural)"
                except Exception as e2:
                    self.log(f"Edge TTS สำรอง (Premwadee) ขัดข้อง: {e2}")
        finally:
            loop.close()

        # 2. ระบบสำรอง Google TTS (gTTS)
        if self.fallback_gtts_cb.GetValue():
            try:
                tts = gTTS(text=text, lang="th")
                tts.save(temp_file)
                if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
                    return "gTTS (เสียงสำรอง)"
            except Exception as e3:
                self.log(f"gTTS ขัดข้อง: {e3}")

        return None

    # --- ตัวจัดการคิวและเล่นเสียง ---
    def audio_worker(self):
        while True:
            item = self.speech_queue.get()
            text, is_test = item

            if not self.is_running and not is_test:
                self.speech_queue.task_done()
                continue

            temp_file = os.path.join(
                tempfile.gettempdir(), f"yt_tts_{random.randint(10000, 99999)}.mp3"
            )

            try:
                engine_used = self.synthesize_audio(text, temp_file, is_test=is_test)
                if engine_used and os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
                    if is_test:
                        self.log(f"กำลังเล่นเสียงทดสอบผ่าน: {engine_used}")

                    pygame.mixer.music.load(temp_file)
                    pygame.mixer.music.play()
                    while pygame.mixer.music.get_busy() and (self.is_running or is_test):
                        time.sleep(0.1)

                    pygame.mixer.music.unload()
                else:
                    self.log("ไม่สามารถสร้างไฟล์เสียงได้จากทุก Engine")

            except Exception as e:
                self.log(f"เกิดข้อผิดพลาดขณะเล่นเสียง: {e}")
            finally:
                try:
                    pygame.mixer.music.unload()
                except Exception:
                    pass
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except Exception:
                        pass
                self.speech_queue.task_done()

    def on_close(self, event):
        self.is_running = False
        try:
            pygame.mixer.music.stop()
            pygame.mixer.quit()
        except Exception:
            pass
        self.Destroy()


if __name__ == "__main__":
    app = wx.App(False)
    frame = YouTubeTTSFrame()
    frame.Show()
    app.MainLoop()
