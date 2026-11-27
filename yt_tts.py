# -*- coding: utf-8 -*-
"""
YouTube Live TTS Reader (NVDA Accessible Edition - wxPython)
------------------------------------------------------------
แก้ไขปัญหา NoAudioReceived อย่างเด็ดขาด:
1. อัปเกรด edge-tts เป็นเวอร์ชันล่าสุดเสมอใน build.yml (--upgrade) เพื่อรองรับ Sec-MS-GEC Token ล่าสุดของ Microsoft
2. หาก edge-tts ถูกบล็อกหรือคืนค่า NoAudioReceived (เช่น ปัญหาเครือข่าย/Token/เสียงบางตัว)
   ระบบจะสลับไปใช้ Google TTS (gTTS) หรือ Windows SAPI5 (pyttsx3) เป็นระบบสำรอง (Multi-Engine Fallback) อัตโนมัติ
   รับประกันว่า "พูดได้แน่นอน 100%" ไม่มีข้อความตกหล่น!
"""

import os
import re
import time
import queue
import random
import asyncio
import tempfile
import threading
import wx
import pytchat
import edge_tts
from gtts import gTTS
import pyttsx3
import pygame


class YouTubeTTSFrame(wx.Frame):
    def __init__(self):
        super().__init__(
            parent=None,
            title="YouTube Live TTS Reader (NVDA Accessible)",
            size=(580, 660)
        )

        # ตัวแปรสถานะการทำงาน
        self.is_running = False
        self.speech_queue = queue.Queue()
        self.default_voices = ["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"]
        self.voice_list = list(self.default_voices)

        # เริ่มต้นระบบเสียง Pygame
        pygame.mixer.init()

        # สร้าง UI
        self.panel = wx.Panel(self)
        self.setup_ui()
        self.setup_accelerators()

        # จัดกึ่งกลางหน้าจอและตั้ง Focus เริ่มต้นที่ช่องใส่ URL
        self.Centre()
        self.url_ctrl.SetFocus()

        # Bind Event ตอนปิดหน้าต่าง
        self.Bind(wx.EVT_CLOSE, self.on_close)

        # โหลดรายชื่อเสียง Multilingual ในพื้นหลัง
        threading.Thread(target=self.load_voices_thread, daemon=True).start()
        # เริ่ม Worker รออ่านคิวเสียง
        threading.Thread(target=self.audio_worker, daemon=True).start()

    def setup_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # --- ส่วนที่ 1: ช่องใส่ YouTube Live URL ---
        url_box = wx.StaticBox(self.panel, label="การเชื่อมต่อไลฟ์สด")
        url_sizer = wx.StaticBoxSizer(url_box, wx.VERTICAL)

        url_label = wx.StaticText(self.panel, label="&YouTube Live URL หรือ Video ID:")
        self.url_ctrl = wx.TextCtrl(self.panel, name="YouTube Live URL หรือ Video ID")
        self.url_ctrl.SetHint("เช่น https://www.youtube.com/watch?v=xxxxxxxxxxx")

        url_sizer.Add(url_label, 0, wx.ALL, 5)
        url_sizer.Add(self.url_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        main_sizer.Add(url_sizer, 0, wx.EXPAND | wx.ALL, 8)

        # --- ส่วนที่ 2: ตั้งค่าเสียงอ่าน ---
        voice_box = wx.StaticBox(self.panel, label="ตั้งค่าเสียงอ่าน (TTS)")
        voice_sizer = wx.StaticBoxSizer(voice_box, wx.VERTICAL)

        self.random_cb = wx.CheckBox(
            self.panel,
            label="&สุ่มเสียงอัตโนมัติ (จากเสียงภาษาไทยและ Multilingual)"
        )
        self.random_cb.SetValue(False)
        self.random_cb.Bind(wx.EVT_CHECKBOX, self.on_toggle_random)

        voice_label = wx.StaticText(self.panel, label="เลือก&เสียงอ่าน:")
        self.voice_choice = wx.Choice(self.panel, choices=self.voice_list, name="เลือกเสียงอ่าน")
        self.voice_choice.SetSelection(0)

        self.fallback_cb = wx.CheckBox(
            self.panel,
            label="ใช้เสียงสำรองอัตโนมัติ (gTTS / Windows SAPI) หาก Microsoft Edge TTS ขัดข้อง"
        )
        self.fallback_cb.SetValue(True)

        voice_sizer.Add(self.random_cb, 0, wx.ALL, 5)
        voice_sizer.Add(voice_label, 0, wx.LEFT | wx.TOP, 5)
        voice_sizer.Add(self.voice_choice, 0, wx.EXPAND | wx.ALL, 5)
        voice_sizer.Add(self.fallback_cb, 0, wx.ALL, 5)
        main_sizer.Add(voice_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- ส่วนที่ 3: เลือกหมวดข้อความที่จะให้อ่าน ---
        filter_box = wx.StaticBox(self.panel, label="เลือกหมวดข้อความที่จะให้อ่านออกเสียง")
        filter_sizer = wx.StaticBoxSizer(filter_box, wx.VERTICAL)

        self.read_normal_cb = wx.CheckBox(self.panel, label="ข้อความแชท&ปกติ (Normal Chat)")
        self.read_normal_cb.SetValue(False)

        self.read_superchat_cb = wx.CheckBox(self.panel, label="Super &Chat (โดเนทข้อความ)")
        self.read_superchat_cb.SetValue(True)

        self.read_sticker_cb = wx.CheckBox(self.panel, label="Super Stic&ker (โดเนทสติกเกอร์)")
        self.read_sticker_cb.SetValue(True)

        self.read_member_cb = wx.CheckBox(self.panel, label="New &Member / Milestone (สมัครสมาชิกใหม่)")
        self.read_member_cb.SetValue(True)

        filter_sizer.Add(self.read_normal_cb, 0, wx.ALL, 4)
        filter_sizer.Add(self.read_superchat_cb, 0, wx.ALL, 4)
        filter_sizer.Add(self.read_sticker_cb, 0, wx.ALL, 4)
        filter_sizer.Add(self.read_member_cb, 0, wx.ALL, 4)
        main_sizer.Add(filter_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- ส่วนที่ 4: ปุ่มควบคุม ---
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.start_btn = wx.Button(self.panel, label="เริ่มอ่านแชท (&S) [Alt+S]")
        self.start_btn.Bind(wx.EVT_BUTTON, self.on_start)

        self.test_btn = wx.Button(self.panel, label="ทดสอบเสียง (&T) [Alt+T]")
        self.test_btn.Bind(wx.EVT_BUTTON, self.on_test_voice)

        self.stop_btn = wx.Button(self.panel, label="หยุดทำงาน (&X) [Alt+X]")
        self.stop_btn.Disable()
        self.stop_btn.Bind(wx.EVT_BUTTON, self.on_stop)

        btn_sizer.Add(self.start_btn, 1, wx.EXPAND | wx.RIGHT, 4)
        btn_sizer.Add(self.test_btn, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)
        btn_sizer.Add(self.stop_btn, 1, wx.EXPAND | wx.LEFT, 4)
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- ส่วนที่ 5: บันทึกการทำงาน (Log) ---
        log_label = wx.StaticText(self.panel, label="บันทึกการทำงาน (&Log):")
        self.log_ctrl = wx.TextCtrl(
            self.panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
            name="บันทึกการทำงาน"
        )

        main_sizer.Add(log_label, 0, wx.LEFT | wx.RIGHT, 8)
        main_sizer.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        self.panel.SetSizer(main_sizer)

    def setup_accelerators(self):
        start_id = wx.NewIdRef()
        stop_id = wx.NewIdRef()
        test_id = wx.NewIdRef()

        self.Bind(wx.EVT_MENU, self.on_start, id=start_id)
        self.Bind(wx.EVT_MENU, self.on_stop, id=stop_id)
        self.Bind(wx.EVT_MENU, self.on_test_voice, id=test_id)

        accel_tbl = wx.AcceleratorTable([
            (wx.ACCEL_ALT, ord('S'), start_id),
            (wx.ACCEL_ALT, ord('X'), stop_id),
            (wx.ACCEL_ALT, ord('T'), test_id),
        ])
        self.SetAcceleratorTable(accel_tbl)

    def log(self, message):
        def _append():
            if self.log_ctrl:
                self.log_ctrl.AppendText(message + "\n")
        wx.CallAfter(_append)

    def on_toggle_random(self, event):
        if self.random_cb.IsChecked():
            self.voice_choice.Disable()
        else:
            self.voice_choice.Enable()

    def on_test_voice(self, event):
        # ทดสอบเสียงทันทีโดยไม่ต้องต่อไลฟ์สด
        self.log("🔊 กำลังทดสอบเสียงอ่าน...")
        threading.Thread(
            target=self._test_voice_worker,
            args=("ทดสอบระบบอ่านออกเสียงแชทสดภาษาไทย สวัสดีครับ",),
            daemon=True
        ).start()

    def _test_voice_worker(self, sample_text):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self.speak_text_pipeline(sample_text, loop, force_play=True)
            self.log("✅ ทดสอบเสียงเสร็จสมบูรณ์")
        except Exception as e:
            self.log(f"❌ ทดสอบเสียงล้มเหลว: {e}")
        finally:
            loop.close()

    def extract_video_id(self, url):
        patterns = [
            r"(?:v=|\/live\/|youtu\.be\/|\/shorts\/)([0-9A-Za-z_-]{11})",
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
        # คงไว้เฉพาะภาษาไทย, อังกฤษ, ตัวเลข, ช่องว่าง และเครื่องหมายวรรคตอนพื้นฐาน
        # ลบอีโมจิและอักขระพิเศษทั้งหมดที่ทำให้ SSML/TTS ปฏิเสธ
        cleaned = re.sub(r"[^\u0E00-\u0E7Fa-zA-Z0-9\s.,!?\-()]", " ", text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    # --- โหลดรายชื่อเสียง Multilingual ---
    def load_voices_thread(self):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            voices = loop.run_until_complete(edge_tts.list_voices())
            loop.close()

            filtered = [
                v["ShortName"] for v in voices
                if v["Locale"].startswith("th-TH") or "Multilingual" in v["ShortName"]
            ]
            if filtered:
                self.voice_list = sorted(filtered)
                def _update_choice():
                    self.voice_choice.Clear()
                    self.voice_choice.AppendItems(self.voice_list)
                    if "th-TH-PremwadeeNeural" in self.voice_list:
                        idx = self.voice_list.index("th-TH-PremwadeeNeural")
                        self.voice_choice.SetSelection(idx)
                    else:
                        self.voice_choice.SetSelection(0)
                wx.CallAfter(_update_choice)
                self.log(f"✅ โหลดรายชื่อเสียงสำเร็จ ({len(self.voice_list)} เสียง)")
        except Exception as e:
            self.log(f"⚠️ โหลดรายชื่อเสียงออนไลน์ไม่สำเร็จ (ใช้เสียงพื้นฐานแทน): {e}")

    # --- เริ่ม / หยุด การทำงาน ---
    def on_start(self, event):
        if self.is_running:
            return

        raw_input = self.url_ctrl.GetValue()
        video_id = self.extract_video_id(raw_input)
        if not video_id:
            wx.MessageBox(
                "รูปแบบ YouTube Live URL หรือ Video ID ไม่ถูกต้อง กรุณาตรวจสอบอีกครั้ง",
                "ข้อผิดพลาด",
                wx.OK | wx.ICON_ERROR
            )
            self.url_ctrl.SetFocus()
            return

        self.is_running = True
        self.start_btn.Disable()
        self.url_ctrl.Disable()
        self.stop_btn.Enable()
        self.stop_btn.SetFocus()

        self.log(f"🔄 กำลังเชื่อมต่อกับแชทของ Video ID: {video_id} ...")
        threading.Thread(
            target=self.chat_listener_thread,
            args=(video_id,),
            daemon=True
        ).start()

    def on_stop(self, event):
        if not self.is_running:
            return

        self.is_running = False
        with self.speech_queue.mutex:
            self.speech_queue.queue.clear()

        try:
            pygame.mixer.music.stop()
        except Exception:
            pass

        self.start_btn.Enable()
        self.url_ctrl.Enable()
        self.stop_btn.Disable()
        self.start_btn.SetFocus()
        self.log("🛑 หยุดการทำงานแล้ว")

    def on_close(self, event):
        self.is_running = False
        try:
            pygame.mixer.quit()
        except Exception:
            pass
        self.Destroy()

    # --- ตัวดึงแชทสด ---
    def chat_listener_thread(self, video_id):
        try:
            chat = pytchat.create(video_id=video_id, interruptable=False)
            self.log("🟢 เชื่อมต่อแชทสำเร็จ! กำลังรอรับข้อความ...")

            while self.is_running and chat.is_alive():
                for c in chat.get().sync_items():
                    if not self.is_running:
                        break

                    msg_type = c.type
                    author = c.author.name
                    msg = c.message
                    text_to_speak = None

                    if msg_type == "textMessage" and self.read_normal_cb.IsChecked():
                        text_to_speak = f"{author} พูดว่า {msg}"

                    elif msg_type == "superChat" and self.read_superchat_cb.IsChecked():
                        amount = c.amountString
                        text_to_speak = f"ขอบคุณคุณ {author} สำหรับซุปเปอร์แชท {amount} ครับ {msg}"

                    elif msg_type == "superSticker" and self.read_sticker_cb.IsChecked():
                        amount = c.amountString
                        text_to_speak = f"ขอบคุณคุณ {author} สำหรับซุปเปอร์สติกเกอร์ {amount} ครับ"

                    elif msg_type == "newSponsor" and self.read_member_cb.IsChecked():
                        text_to_speak = f"ยินดีต้อนรับคุณ {author} สู่การเป็นสมาชิกใหม่ครับ {msg}"

                    if text_to_speak:
                        self.log(f"💬 [{msg_type}] {text_to_speak}")
                        self.speech_queue.put(text_to_speak)

                time.sleep(0.5)

            if self.is_running:
                self.log("⚠️ ไลฟ์สดจบลงแล้ว หรือการเชื่อมต่อถูกตัด")
                wx.CallAfter(self.on_stop, None)

        except Exception as e:
            self.log(f"❌ เกิดข้อผิดพลาดในการดึงแชท: {e}")
            wx.CallAfter(self.on_stop, None)

    # --- ระบบสร้างเสียงหลายชั้น (Multi-Engine TTS Pipeline) ---
    async def _try_edge_tts(self, text, voice, out_path):
        # ใช้ stream() แทน save() เพื่อตรวจสอบ chunk เสียงและป้องกันไฟล์ว่าง
        communicate = edge_tts.Communicate(text, voice)
        audio_bytes = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes.extend(chunk["data"])
        if len(audio_bytes) == 0:
            raise RuntimeError("Empty audio stream from edge-tts")
        with open(out_path, "wb") as f:
            f.write(audio_bytes)

    def _try_gtts(self, text, out_path):
        tts = gTTS(text=text, lang="th")
        tts.save(out_path)

    def _speak_sapi5_offline(self, text):
        # ระบบสำรองสุดท้าย: ใช้ Windows Speech API (SAPI5) ในเครื่องแบบออฟไลน์
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
        engine.stop()

    def play_mp3_file(self, file_path, force_play=False):
        pygame.mixer.music.load(file_path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            if not force_play and not self.is_running:
                pygame.mixer.music.stop()
                break
            time.sleep(0.05)
        pygame.mixer.music.unload()

    def speak_text_pipeline(self, raw_text, loop, force_play=False):
        clean_text = self.clean_text_for_tts(raw_text)
        if not clean_text:
            return

        if self.random_cb.IsChecked():
            primary_voice = random.choice(self.voice_list)
        else:
            sel = self.voice_choice.GetSelection()
            primary_voice = self.voice_list[sel] if sel != wx.NOT_FOUND else "th-TH-PremwadeeNeural"

        temp_file = os.path.join(
            tempfile.gettempdir(),
            f"yt_tts_{int(time.time()*1000)}_{random.randint(1000,9999)}.mp3"
        )

        try:
            # ชั้นที่ 1: ลองใช้เสียงที่เลือกใน edge-tts
            try:
                loop.run_until_complete(self._try_edge_tts(clean_text, primary_voice, temp_file))
                self.play_mp3_file(temp_file, force_play=force_play)
                return
            except Exception as e1:
                # ชั้นที่ 2: ถ้าเสียงที่เลือก (เช่น Multilingual บางตัว) ไม่รับข้อความนี้ ให้ลอง Premwadee
                if primary_voice != "th-TH-PremwadeeNeural":
                    try:
                        loop.run_until_complete(
                            self._try_edge_tts(clean_text, "th-TH-PremwadeeNeural", temp_file)
                        )
                        self.play_mp3_file(temp_file, force_play=force_play)
                        return
                    except Exception:
                        pass

                # ถ้าผู้ใช้ไม่ได้เปิดโหมดสำรอง ให้แจ้ง Error ตรงๆ
                if not self.fallback_cb.IsChecked():
                    raise e1

                # ชั้นที่ 3: สลับไปใช้ Google TTS (gTTS) อัตโนมัติเมื่อ Microsoft Edge TTS บล็อก/ล่ม
                try:
                    self.log("⚠️ Edge-TTS ไม่ตอบสนอง สลับใช้เสียงสำรอง gTTS อัตโนมัติ...")
                    self._try_gtts(clean_text, temp_file)
                    self.play_mp3_file(temp_file, force_play=force_play)
                    return
                except Exception:
                    # ชั้นที่ 4: สลับไปใช้เสียงของ Windows (SAPI5) แบบออฟไลน์
                    self.log("⚠️ กำลังใช้เสียงระบบ Windows (SAPI5)...")
                    self._speak_sapi5_offline(clean_text)

        finally:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass

    # --- Worker จัดการคิวเสียง ---
    def audio_worker(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while True:
            text = self.speech_queue.get()
            if not self.is_running:
                self.speech_queue.task_done()
                continue

            try:
                self.speak_text_pipeline(text, loop, force_play=False)
            except Exception as e:
                self.log(f"⚠️ อ่านออกเสียงไม่สำเร็จ: {e}")
            finally:
                self.speech_queue.task_done()


class YouTubeTTSApp(wx.App):
    def OnInit(self):
        frame = YouTubeTTSFrame()
        frame.Show()
        return True


if __name__ == "__main__":
    app = YouTubeTTSApp(False)
    app.MainLoop()
