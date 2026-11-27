import asyncio
import hashlib
import os
import queue
import random
import re
import tempfile
import threading
import time
import urllib.parse
import uuid
import wx
import aiohttp
import edge_tts
import pygame
import pytchat
import pyttsx3
from gtts import gTTS

# ==============================================================================
# การเชื่อมต่อ WebSocket ของ Microsoft Edge Read Aloud โดยตรง (Sec-MS-GEC v2)
# อ้างอิงเทคนิคจาก NaturalVoiceSAPIAdapter & edge-tts handshake ล่าสุด
# เพื่อแก้ปัญหา HTTP 403 / NoAudioReceived และทำงานแบบ Standalone 100%
# ==============================================================================

TRUSTED_CLIENT_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
CHROMIUM_FULL_VERSION = "143.0.3650.75"
CHROMIUM_MAJOR_VERSION = "143"
SEC_MS_GEC_VERSION = f"1-{CHROMIUM_FULL_VERSION}"
WIN_EPOCH_OFFSET = 11644473600  # วินาทีระหว่าง 1601-01-01 ถึง 1970-01-01


def generate_sec_ms_gec():
    """คำนวณโทเค็น Sec-MS-GEC ตามมาตรฐานเวลา Windows FileTime (ปัดเศษทุก 5 นาที)"""
    ticks = time.time() + WIN_EPOCH_OFFSET
    ticks -= ticks % 300
    ticks_100ns = int(ticks * 10_000_000)
    raw_str = f"{ticks_100ns}{TRUSTED_CLIENT_TOKEN}"
    return hashlib.sha256(raw_str.encode("ascii")).hexdigest().upper()


def escape_xml(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


async def synthesize_edge_direct(text, voice_name, output_path):
    """
    เชื่อมต่อ WebSocket ไปยัง endpoint ของ Microsoft Edge โดยตรง
    กำหนด Header, User-Agent, Origin และ Sec-MS-GEC ให้ตรงกับเบราว์เซอร์ Edge
    """
    conn_id = uuid.uuid4().hex.upper()
    sec_ms_gec = generate_sec_ms_gec()

    query = urllib.parse.urlencode({
        "TrustedClientToken": TRUSTED_CLIENT_TOKEN,
        "ConnectionId": conn_id,
        "Sec-MS-GEC": sec_ms_gec,
        "Sec-MS-GEC-Version": SEC_MS_GEC_VERSION,
    })
    ws_url = f"wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1?{query}"

    headers = {
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "Origin": "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
        "User-Agent": (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{CHROMIUM_MAJOR_VERSION}.0.0.0 Safari/537.36 "
            f"Edg/{CHROMIUM_MAJOR_VERSION}.0.0.0"
        ),
    }

    timestamp = time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime())
    req_id = uuid.uuid4().hex.upper()

    # สกัด Locale จากชื่อเสียง เช่น th-TH-PremwadeeNeural -> th-TH
    parts = voice_name.split("-")
    locale = f"{parts[0]}-{parts[1]}" if len(parts) >= 2 else "th-TH"

    config_msg = (
        f"X-Timestamp:{timestamp}\r\n"
        f"Content-Type:application/json; charset=utf-8\r\n"
        f"Path:speech.config\r\n\r\n"
        f'{{"context":{{"synthesis":{{"audio":{{"metadataoptions":{{"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"}},"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}}}}}'
    )

    ssml_body = (
        f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='{locale}'>"
        f"<voice name='{voice_name}'>"
        f"<prosody pitch='+0Hz' rate='+0%' volume='+0%'>{escape_xml(text)}</prosody>"
        f"</voice></speak>"
    )

    ssml_msg = (
        f"X-RequestId:{req_id}\r\n"
        f"Content-Type:application/ssml+xml\r\n"
        f"X-Timestamp:{timestamp}Z\r\n"
        f"Path:ssml\r\n\r\n"
        f"{ssml_body}"
    )

    audio_chunks = bytearray()
    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(ws_url, headers=headers) as ws:
            await ws.send_str(config_msg)
            await ws.send_str(ssml_msg)

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    data = msg.data
                    if len(data) >= 2:
                        header_len = int.from_bytes(data[:2], "big")
                        header_bytes = data[2 : 2 + header_len]
                        if b"Path:audio" in header_bytes:
                            audio_chunks.extend(data[2 + header_len :])
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    if "Path:turn.end" in msg.data:
                        break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    if len(audio_chunks) < 100:
        raise RuntimeError("No audio bytes received from Edge WebSocket")

    with open(output_path, "wb") as f:
        f.write(audio_chunks)


class YouTubeTTSFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="YouTube Live TTS Reader", size=(580, 680))

        self.is_running = False
        self.speech_queue = queue.Queue()
        self.voice_list = [
            "th-TH-PremwadeeNeural",
            "th-TH-NiwatNeural",
            "en-US-AndrewMultilingualNeural",
            "en-US-AvaMultilingualNeural",
            "en-US-BrianMultilingualNeural",
            "en-US-EmmaMultilingualNeural",
            "ja-JP-NanamiMultilingualNeural",
            "ko-KR-HyunsuMultilingualNeural",
        ]

        pygame.mixer.init()

        self.setup_ui()
        self.setup_accelerators()
        self.Centre()

        threading.Thread(target=self.load_voices_thread, daemon=True).start()
        threading.Thread(target=self.audio_worker, daemon=True).start()

        self.Bind(wx.EVT_CLOSE, self.on_close)

    def setup_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # --- ส่วนที่ 1: ช่องใส่ URL ---
        url_box = wx.StaticBox(panel, label="การเชื่อมต่อไลฟ์สด")
        url_sizer = wx.StaticBoxSizer(url_box, wx.VERTICAL)

        url_lbl = wx.StaticText(panel, label="YouTube Live URL หรือ Video ID:")
        self.url_txt = wx.TextCtrl(panel, name="YouTube Live URL หรือ Video ID")

        url_sizer.Add(url_lbl, 0, wx.ALL, 5)
        url_sizer.Add(self.url_txt, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        main_sizer.Add(url_sizer, 0, wx.EXPAND | wx.ALL, 8)

        # --- ส่วนที่ 2: ตั้งค่าเสียง ---
        voice_box = wx.StaticBox(panel, label="ตั้งค่าเสียงพูด")
        voice_sizer = wx.StaticBoxSizer(voice_box, wx.VERTICAL)

        self.random_cb = wx.CheckBox(
            panel, label="สุ่มเสียงอัตโนมัติ (จากเสียงไทย และ Multilingual)"
        )
        self.random_cb.Bind(wx.EVT_CHECKBOX, self.on_toggle_random)

        voice_lbl = wx.StaticText(panel, label="เลือกเสียงพูดหลัก:")
        self.voice_choice = wx.Choice(
            panel, choices=self.voice_list, name="เลือกเสียงพูดหลัก"
        )
        self.voice_choice.SetSelection(0)

        self.fallback_gtts_cb = wx.CheckBox(
            panel, label="สลับใช้เสียงสำรอง (Google TTS / Windows SAPI5) หากเชื่อมต่อ Microsoft ไม่สำเร็จ"
        )
        self.fallback_gtts_cb.SetValue(True)

        voice_sizer.Add(self.random_cb, 0, wx.ALL, 5)
        voice_sizer.Add(voice_lbl, 0, wx.LEFT | wx.RIGHT | wx.TOP, 5)
        voice_sizer.Add(self.voice_choice, 0, wx.EXPAND | wx.ALL, 5)
        voice_sizer.Add(self.fallback_gtts_cb, 0, wx.ALL, 5)
        main_sizer.Add(voice_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- ส่วนที่ 3: เลือกหมวดข้อความ ---
        filter_box = wx.StaticBox(panel, label="หมวดหมู่ข้อความที่จะให้อ่านออกเสียง")
        filter_sizer = wx.StaticBoxSizer(filter_box, wx.VERTICAL)

        self.cb_normal = wx.CheckBox(panel, label="ข้อความแชทปกติ (Normal Chat)")
        self.cb_superchat = wx.CheckBox(panel, label="Super Chat (โดเนทข้อความ)")
        self.cb_sticker = wx.CheckBox(panel, label="Super Sticker (โดเนทสติกเกอร์)")
        self.cb_member = wx.CheckBox(panel, label="New Member / Milestone (สมัครสมาชิกใหม่)")

        self.cb_superchat.SetValue(True)
        self.cb_sticker.SetValue(True)
        self.cb_member.SetValue(True)

        filter_sizer.Add(self.cb_normal, 0, wx.ALL, 4)
        filter_sizer.Add(self.cb_superchat, 0, wx.ALL, 4)
        filter_sizer.Add(self.cb_sticker, 0, wx.ALL, 4)
        filter_sizer.Add(self.cb_member, 0, wx.ALL, 4)
        main_sizer.Add(filter_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- ส่วนที่ 4: ปุ่มควบคุม ---
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.btn_start = wx.Button(panel, label="เริ่มอ่านแชท (&S)")
        self.btn_stop = wx.Button(panel, label="หยุดทำงาน (&X)")
        self.btn_test = wx.Button(panel, label="ทดสอบเสียง (&T)")
        self.btn_stop.Disable()

        self.btn_start.Bind(wx.EVT_BUTTON, self.on_start)
        self.btn_stop.Bind(wx.EVT_BUTTON, self.on_stop)
        self.btn_test.Bind(wx.EVT_BUTTON, self.on_test_voice)

        btn_sizer.Add(self.btn_start, 1, wx.EXPAND | wx.RIGHT, 4)
        btn_sizer.Add(self.btn_stop, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)
        btn_sizer.Add(self.btn_test, 1, wx.EXPAND | wx.LEFT, 4)
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- ส่วนที่ 5: บันทึกการทำงาน (Log) ---
        log_lbl = wx.StaticText(panel, label="บันทึกการทำงาน (Log):")
        self.log_txt = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
            name="บันทึกการทำงาน",
        )

        main_sizer.Add(log_lbl, 0, wx.LEFT | wx.RIGHT, 8)
        main_sizer.Add(self.log_txt, 1, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(main_sizer)
        self.url_txt.SetFocus()

    def setup_accelerators(self):
        accel_tbl = wx.AcceleratorTable([
            (wx.ACCEL_ALT, ord("S"), self.btn_start.GetId()),
            (wx.ACCEL_ALT, ord("X"), self.btn_stop.GetId()),
            (wx.ACCEL_ALT, ord("T"), self.btn_test.GetId()),
        ])
        self.SetAcceleratorTable(accel_tbl)

    def log(self, message):
        wx.CallAfter(self._append_log, message)

    def _append_log(self, message):
        if self.log_txt:
            self.log_txt.AppendText(message + "\n")

    def on_toggle_random(self, event):
        if self.random_cb.IsChecked():
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

    def clean_text_for_tts(self, text):
        if not text:
            return ""
        cleaned = re.sub(r"[^\u0E00-\u0E7Fa-zA-Z0-9\s.,!?\-()']", " ", text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

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
                wx.CallAfter(self._update_voice_choice)
                self.log(f"โหลดรายชื่อเสียงสำเร็จ ({len(self.voice_list)} เสียงที่รองรับภาษาไทย)")
        except Exception:
            self.log("ใช้รายการเสียงพื้นฐานในระบบ")

    def _update_voice_choice(self):
        self.voice_choice.SetItems(self.voice_list)
        if "th-TH-PremwadeeNeural" in self.voice_list:
            self.voice_choice.SetStringSelection("th-TH-PremwadeeNeural")
        else:
            self.voice_choice.SetSelection(0)

    def on_test_voice(self, event):
        sample_text = "สวัสดีครับ ทดสอบระบบอ่านออกเสียงภาษาไทย"
        self.log(f"กำลังทดสอบเสียง: {sample_text}")
        self.speech_queue.put(("TEST", sample_text))

    def on_start(self, event):
        video_id = self.extract_video_id(self.url_txt.GetValue())
        if not video_id:
            wx.MessageBox(
                "กรุณาใส่ลิงก์ YouTube Live หรือ Video ID 11 หลักให้ถูกต้อง",
                "ข้อมูลไม่ถูกต้อง",
                wx.OK | wx.ICON_WARNING,
            )
            self.url_txt.SetFocus()
            return

        self.is_running = True
        self.btn_start.Disable()
        self.url_txt.Disable()
        self.btn_stop.Enable()
        self.btn_stop.SetFocus()

        self.log(f"กำลังเชื่อมต่อกับแชทของ Video ID: {video_id} ...")
        threading.Thread(
            target=self.chat_listener_thread, args=(video_id,), daemon=True
        ).start()

    def on_stop(self, event):
        self._reset_ui_stopped("หยุดการทำงานแล้ว")

    def _reset_ui_stopped(self, reason):
        self.is_running = False
        with self.speech_queue.mutex:
            self.speech_queue.queue.clear()
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass

        def _ui():
            self.btn_start.Enable()
            self.url_txt.Enable()
            self.btn_stop.Disable()
            self.btn_start.SetFocus()
            self._append_log(reason)

        wx.CallAfter(_ui)

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

                    if msg_type == "textMessage" and self.cb_normal.IsChecked():
                        text_to_speak = f"{author} พูดว่า {msg}"
                    elif msg_type == "superChat" and self.cb_superchat.IsChecked():
                        amount = c.amountString
                        text_to_speak = f"ขอบคุณคุณ {author} สำหรับซุปเปอร์แชท {amount} ครับ {msg}"
                    elif msg_type == "superSticker" and self.cb_sticker.IsChecked():
                        amount = c.amountString
                        text_to_speak = f"ขอบคุณคุณ {author} สำหรับซุปเปอร์สติกเกอร์ {amount} ครับ"
                    elif msg_type == "newSponsor" and self.cb_member.IsChecked():
                        text_to_speak = f"ยินดีต้อนรับคุณ {author} สู่การเป็นสมาชิกใหม่ครับ {msg}"

                    if text_to_speak:
                        self.log(f"[{msg_type}] {text_to_speak}")
                        self.speech_queue.put(("LIVE", text_to_speak))

                time.sleep(0.5)

            if self.is_running:
                self._reset_ui_stopped("ไลฟ์สดจบลงแล้ว หรือการเชื่อมต่อถูกตัด")

        except Exception as e:
            self._reset_ui_stopped(f"เกิดข้อผิดพลาดในการดึงแชท: {e}")

    def speak_with_sapi5(self, text):
        try:
            engine = pyttsx3.init()
            engine.say(text)
            engine.runAndWait()
            engine.stop()
            return True
        except Exception as e:
            self.log(f"SAPI5 ล้มเหลว: {e}")
            return False

    def generate_audio_file(self, loop, text, temp_file):
        # 1. ลองเชื่อมต่อ WebSocket ของ Microsoft Edge โดยตรง (Sec-MS-GEC v2)
        if self.random_cb.IsChecked():
            primary_voice = random.choice(self.voice_list)
        else:
            primary_voice = self.voice_choice.GetStringSelection()

        voices_to_try = [primary_voice]
        if primary_voice != "th-TH-PremwadeeNeural":
            voices_to_try.append("th-TH-PremwadeeNeural")

        for v in voices_to_try:
            try:
                loop.run_until_complete(synthesize_edge_direct(text, v, temp_file))
                if os.path.exists(temp_file) and os.path.getsize(temp_file) > 100:
                    return f"Edge Direct ({v})"
            except Exception as e:
                self.log(f"Edge Direct ({v}) ไม่สำเร็จ: {e}")

        # 2. ลองผ่านไลบรารี edge-tts มาตรฐานเผื่อมีการอัปเดต endpoint ภายใน
        try:
            communicate = edge_tts.Communicate(text, "th-TH-PremwadeeNeural")
            loop.run_until_complete(communicate.save(temp_file))
            if os.path.exists(temp_file) and os.path.getsize(temp_file) > 100:
                return "edge-tts"
        except Exception:
            pass

        # 3. สลับใช้ Google TTS (gTTS) หากเปิดใช้งานเสียงสำรอง
        if self.fallback_gtts_cb.IsChecked():
            try:
                tts = gTTS(text=text, lang="th")
                tts.save(temp_file)
                if os.path.exists(temp_file) and os.path.getsize(temp_file) > 100:
                    return "gTTS (สำรอง)"
            except Exception as e:
                self.log(f"gTTS ล้มเหลว: {e}")

        return None

    def audio_worker(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while True:
            mode, raw_text = self.speech_queue.get()
            if mode == "LIVE" and not self.is_running:
                self.speech_queue.task_done()
                continue

            text = self.clean_text_for_tts(raw_text)
            if not text:
                self.speech_queue.task_done()
                continue

            temp_file = os.path.join(
                tempfile.gettempdir(), f"yt_tts_{random.randint(10000, 99999)}.mp3"
            )

            try:
                used_engine = self.generate_audio_file(loop, text, temp_file)

                if used_engine:
                    if mode == "TEST":
                        self.log(f"กำลังเล่นเสียงทดสอบผ่าน: {used_engine}")
                    pygame.mixer.music.load(temp_file)
                    pygame.mixer.music.play()
                    while pygame.mixer.music.get_busy():
                        if mode == "LIVE" and not self.is_running:
                            pygame.mixer.music.stop()
                            break
                        time.sleep(0.1)
                    pygame.mixer.music.unload()
                else:
                    if self.fallback_gtts_cb.IsChecked():
                        self.log("ใช้เสียง Windows SAPI5 (ออฟไลน์) อ่านแทน")
                        self.speak_with_sapi5(text)
                    else:
                        self.log("ไม่สามารถสร้างเสียงพูดได้")

            except Exception as e:
                self.log(f"เกิดข้อผิดพลาดขณะเล่นเสียง: {e}")
            finally:
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
