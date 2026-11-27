# ==============================================================================
# YouTube Live TTS Reader (Standalone & Screen-Reader Friendly)
# ==============================================================================
# วิธีติดตั้งและคอมไพล์ด้วยตัวเอง:
# 1. pip install -r requirements.txt
# 2. pyinstaller --noconsole --onefile --name yt_tts yt_tts.py
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
import uuid

import certifi
import edge_tts
from gtts import gTTS
import pygame
import websockets
import wx
import pytchat

# ==============================================================================
# วิธีแก้ถาวรจาก Issue #490 ของ rany2/edge-tts:
# ย้าย Endpoint ไปที่ wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1
# พร้อม Ocp-Apim-Subscription-Key แทนการคำนวณ Sec-MS-GEC v2 ที่ติดสิทธิบัตร/โดนบล็อก
# ==============================================================================
TRUSTED_CLIENT_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
MS_EDGE_DIRECT_URL = (
    f"wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
    f"?Ocp-Apim-Subscription-Key={TRUSTED_CLIENT_TOKEN}"
)


def clean_text_for_tts(text: str) -> str:
    """ล้างอีโมจิและอักขระควบคุมที่อาจทำให้ XML/SSML ของ TTS ปฏิเสธการสร้างเสียง"""
    if not text:
        return ""
    # คงไว้เฉพาะภาษาไทย อังกฤษ ตัวเลข เว้นวรรค และเครื่องหมายวรรคตอนพื้นฐาน
    cleaned = re.sub(
        r"[^\u0E00-\u0E7Fa-zA-Z0-9\s.,!?()'\"\-:+/฿$%&]",
        " ",
        text,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


async def synthesize_msedgeservices_ws(text: str, voice: str, out_path: str) -> bool:
    """เชื่อมต่อ WebSocket ตรงไปยัง api.msedgeservices.com (ไม่ติด Sec-MS-GEC v2)"""
    conn_id = uuid.uuid4().hex.upper()
    req_id = uuid.uuid4().hex.upper()
    url = f"{MS_EDGE_DIRECT_URL}&ConnectionId={conn_id}"

    # ระบุ xml:lang ให้ตรงกับ Locale ของเสียง
    lang_match = re.match(r"^([a-z]{2}-[A-Z]{2})", voice)
    xml_lang = lang_match.group(1) if lang_match else "th-TH"

    headers = {
        "Origin": "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
        ),
    }

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    config_msg = (
        f"X-Timestamp:2026-11-27T00:00:00.000Z\r\n"
        f"Content-Type:application/json; charset=utf-8\r\n"
        f"Path:speech.config\r\n\r\n"
        f'{{"context":{{"synthesis":{{"audio":{{"metadataoptions":{{"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"}},"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}}}}}'
    )

    ssml_body = (
        f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='{xml_lang}'>"
        f"<voice name='{voice}'>"
        f"<prosody pitch='+0Hz' rate='+0%' volume='+0%'>{escape_xml(text)}</prosody>"
        f"</voice></speak>"
    )

    ssml_msg = (
        f"X-RequestId:{req_id}\r\n"
        f"Content-Type:application/ssml+xml\r\n"
        f"X-Timestamp:2026-11-27T00:00:00.000Z\r\n"
        f"Path:ssml\r\n\r\n"
        f"{ssml_body}"
    )

    audio_chunks = bytearray()
    async with websockets.connect(
        url,
        additional_headers=headers,
        ssl=ssl_ctx,
        open_timeout=10,
        close_timeout=5,
    ) as ws:
        await ws.send(config_msg)
        await ws.send(ssml_msg)

        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=12)
            if isinstance(msg, bytes):
                # รูปแบบ binary frame: 2 bytes header length + header + audio payload
                if len(msg) > 2:
                    header_len = int.from_bytes(msg[:2], "big")
                    payload = msg[2 + header_len :]
                    if payload:
                        audio_chunks.extend(payload)
            elif isinstance(msg, str):
                if "Path:turn.end" in msg:
                    break

    if len(audio_chunks) > 512:
        with open(out_path, "wb") as f:
            f.write(audio_chunks)
        return True
    return False


class YouTubeTTSFrame(wx.Frame):
    def __init__(self):
        super().__init__(
            None,
            title="YouTube Live TTS Reader",
            size=(580, 660),
            style=wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX),
        )

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

        self.init_ui()
        self.bind_shortcuts()
        self.Centre()

        # โหลดรายชื่อเสียงล่าสุดในพื้นหลัง
        threading.Thread(target=self.load_voices_thread, daemon=True).start()
        # เริ่ม Worker เล่นเสียงตามคิว
        threading.Thread(target=self.audio_worker, daemon=True).start()

    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # --- ส่วนที่ 1: ลิงก์ไลฟ์สด ---
        url_box = wx.StaticBox(panel, label="การเชื่อมต่อ YouTube Live")
        url_sizer = wx.StaticBoxSizer(url_box, wx.VERTICAL)

        url_lbl = wx.StaticText(panel, label="YouTube Live &URL หรือ Video ID:")
        self.url_txt = wx.TextCtrl(panel, name="YouTube Live URL หรือ Video ID")
        self.url_txt.SetHint("วางลิงก์ไลฟ์สด หรือรหัส Video ID 11 ตัวอักษร")

        url_sizer.Add(url_lbl, 0, wx.ALL, 5)
        url_sizer.Add(self.url_txt, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        main_sizer.Add(url_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # --- ส่วนที่ 2: ตั้งค่าเสียง ---
        voice_box = wx.StaticBox(panel, label="ตั้งค่าเสียงอ่าน (Microsoft Edge Neural)")
        voice_sizer = wx.StaticBoxSizer(voice_box, wx.VERTICAL)

        self.random_cb = wx.CheckBox(
            panel, label="&สุ่มเสียงอัตโนมัติ (จากเสียงภาษาไทยและ Multilingual)"
        )
        self.random_cb.Bind(wx.EVT_CHECKBOX, self.on_toggle_random)

        voice_lbl = wx.StaticText(panel, label="เลือกเสียงที่ต้องการ:")
        self.voice_choice = wx.Choice(
            panel, choices=self.voice_list, name="เลือกเสียงที่ต้องการ"
        )
        self.voice_choice.SetSelection(0)

        self.fallback_cb = wx.CheckBox(
            panel, label="เปิดใช้เสียงสำรอง gTTS อัตโนมัติเมื่อเซิร์ฟเวอร์ขัดข้อง"
        )
        self.fallback_cb.SetValue(True)

        voice_sizer.Add(self.random_cb, 0, wx.ALL, 6)
        voice_sizer.Add(voice_lbl, 0, wx.LEFT | wx.TOP, 6)
        voice_sizer.Add(self.voice_choice, 0, wx.EXPAND | wx.ALL, 6)
        voice_sizer.Add(self.fallback_cb, 0, wx.ALL, 6)
        main_sizer.Add(voice_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- ส่วนที่ 3: เลือกหมวดข้อความ ---
        filter_box = wx.StaticBox(panel, label="หมวดหมู่ข้อความที่จะให้อ่านออกเสียง")
        filter_sizer = wx.StaticBoxSizer(filter_box, wx.VERTICAL)

        self.normal_cb = wx.CheckBox(panel, label="ข้อความแชทปกติ (Normal Chat)")
        self.superchat_cb = wx.CheckBox(panel, label="Super Chat (โดเนทข้อความ)")
        self.sticker_cb = wx.CheckBox(panel, label="Super Sticker (โดเนทสติกเกอร์)")
        self.member_cb = wx.CheckBox(panel, label="New Member / Milestone (สมาชิกใหม่)")

        self.superchat_cb.SetValue(True)
        self.sticker_cb.SetValue(True)
        self.member_cb.SetValue(True)

        for cb in (self.normal_cb, self.superchat_cb, self.sticker_cb, self.member_cb):
            filter_sizer.Add(cb, 0, wx.ALL, 4)

        main_sizer.Add(filter_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- ส่วนที่ 4: ปุ่มควบคุม ---
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.start_btn = wx.Button(panel, label="เริ่มอ่านแชท (Alt+&S)")
        self.stop_btn = wx.Button(panel, label="หยุดทำงาน (Alt+&X)")
        self.test_btn = wx.Button(panel, label="ทดสอบเสียง (Alt+&T)")

        self.stop_btn.Disable()

        self.start_btn.Bind(wx.EVT_BUTTON, self.on_start)
        self.stop_btn.Bind(wx.EVT_BUTTON, self.on_stop)
        self.test_btn.Bind(wx.EVT_BUTTON, self.on_test_voice)

        btn_sizer.Add(self.start_btn, 1, wx.EXPAND | wx.RIGHT, 5)
        btn_sizer.Add(self.stop_btn, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)
        btn_sizer.Add(self.test_btn, 1, wx.EXPAND | wx.LEFT, 5)
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- ส่วนที่ 5: บันทึกการทำงาน (Log) ---
        log_lbl = wx.StaticText(panel, label="บันทึกการทำงาน (Log):")
        self.log_txt = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL,
            name="บันทึกการทำงาน",
        )

        main_sizer.Add(log_lbl, 0, wx.LEFT | wx.RIGHT, 10)
        main_sizer.Add(self.log_txt, 1, wx.EXPAND | wx.ALL, 10)

        panel.SetSizer(main_sizer)
        self.url_txt.SetFocus()

    def bind_shortcuts(self):
        id_start = wx.NewIdRef()
        id_stop = wx.NewIdRef()
        id_test = wx.NewIdRef()

        self.Bind(wx.EVT_MENU, self.on_start, id=id_start)
        self.Bind(wx.EVT_MENU, self.on_stop, id=id_stop)
        self.Bind(wx.EVT_MENU, self.on_test_voice, id=id_test)

        accel = wx.AcceleratorTable(
            [
                (wx.ACCEL_ALT, ord("S"), id_start),
                (wx.ACCEL_ALT, ord("X"), id_stop),
                (wx.ACCEL_ALT, ord("T"), id_test),
            ]
        )
        self.SetAcceleratorTable(accel)

    def log(self, message: str):
        wx.CallAfter(self._append_log, message)

    def _append_log(self, message: str):
        self.log_txt.AppendText(message + "\n")

    def on_toggle_random(self, event):
        self.voice_choice.Enable(not self.random_cb.GetValue())

    def extract_video_id(self, raw_url: str):
        patterns = [
            r"(?:v=|\/live\/|youtu\.be\/)([0-9A-Za-z_-]{11})",
            r"^([0-9A-Za-z_-]{11})$",
        ]
        for p in patterns:
            m = re.search(p, raw_url.strip())
            if m:
                return m.group(1)
        return None

    def load_voices_thread(self):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            voices = loop.run_until_complete(edge_tts.list_voices())
            loop.close()

            filtered = sorted(
                [
                    v["ShortName"]
                    for v in voices
                    if v["Locale"].startswith("th-TH")
                    or "Multilingual" in v["ShortName"]
                ]
            )
            if filtered:
                wx.CallAfter(self._update_voice_list, filtered)
        except Exception as e:
            self.log(f"ใช้รายชื่อเสียงเริ่มต้น ({len(self.voice_list)} เสียง): {e}")

    def _update_voice_list(self, new_voices):
        self.voice_list = new_voices
        self.voice_choice.SetItems(self.voice_list)
        if "th-TH-PremwadeeNeural" in self.voice_list:
            self.voice_choice.SetStringSelection("th-TH-PremwadeeNeural")
        else:
            self.voice_choice.SetSelection(0)
        self._append_log(f"โหลดรายชื่อเสียงสำเร็จ ({len(self.voice_list)} เสียงที่รองรับภาษาไทย)")

    def on_test_voice(self, event):
        sample = "สวัสดีครับ ทดสอบระบบอ่านออกเสียงภาษาไทย"
        self.log(f"กำลังทดสอบเสียง: {sample}")
        self.speech_queue.put((sample, True))

    def on_start(self, event):
        if self.is_running:
            return
        video_id = self.extract_video_id(self.url_txt.GetValue())
        if not video_id:
            wx.MessageBox(
                "กรุณากรอก YouTube Live URL หรือ Video ID ให้ถูกต้อง",
                "ข้อมูลไม่ถูกต้อง",
                wx.OK | wx.ICON_WARNING,
            )
            self.url_txt.SetFocus()
            return

        self.is_running = True
        self.start_btn.Disable()
        self.url_txt.Disable()
        self.stop_btn.Enable()
        self.stop_btn.SetFocus()

        self.log(f"กำลังเชื่อมต่อกับแชทของ Video ID: {video_id} ...")
        threading.Thread(
            target=self.chat_listener_thread, args=(video_id,), daemon=True
        ).start()

    def on_stop(self, event):
        if not self.is_running:
            return
        self.is_running = False
        with self.speech_queue.mutex:
            self.speech_queue.queue.clear()
        pygame.mixer.music.stop()

        self.start_btn.Enable()
        self.url_txt.Enable()
        self.stop_btn.Disable()
        self.start_btn.SetFocus()
        self.log("หยุดการทำงานแล้ว")

    def chat_listener_thread(self, video_id: str):
        try:
            chat = pytchat.create(video_id=video_id, interruptable=False)
            self.log("เชื่อมต่อแชทสำเร็จ กำลังรอรับข้อความ...")

            while self.is_running and chat.is_alive():
                for c in chat.get().sync_items():
                    if not self.is_running:
                        break

                    msg_type = c.type
                    author = clean_text_for_tts(c.author.name) or "ผู้ชม"
                    msg = clean_text_for_tts(c.message)
                    text_to_speak = None

                    if msg_type == "textMessage" and self.normal_cb.GetValue():
                        if msg:
                            text_to_speak = f"{author} พูดว่า {msg}"

                    elif msg_type == "superChat" and self.superchat_cb.GetValue():
                        amount = c.amountString
                        text_to_speak = (
                            f"ขอบคุณคุณ {author} สำหรับซุปเปอร์แชท {amount} ครับ {msg}"
                        ).strip()

                    elif msg_type == "superSticker" and self.sticker_cb.GetValue():
                        amount = c.amountString
                        text_to_speak = (
                            f"ขอบคุณคุณ {author} สำหรับซุปเปอร์สติกเกอร์ {amount} ครับ"
                        )

                    elif msg_type == "newSponsor" and self.member_cb.GetValue():
                        text_to_speak = (
                            f"ยินดีต้อนรับคุณ {author} สู่การเป็นสมาชิกใหม่ครับ {msg}"
                        ).strip()

                    if text_to_speak:
                        self.log(f"[{msg_type}] {text_to_speak}")
                        self.speech_queue.put((text_to_speak, False))

                time.sleep(0.5)

            if self.is_running:
                self.log("ไลฟ์สดจบลงแล้ว หรือการเชื่อมต่อถูกตัด")
                wx.CallAfter(self.on_stop, None)

        except Exception as e:
            self.log(f"เกิดข้อผิดพลาดในการดึงแชท: {e}")
            wx.CallAfter(self.on_stop, None)

    def get_selected_voice(self) -> str:
        if self.random_cb.GetValue() and self.voice_list:
            return random.choice(self.voice_list)
        idx = self.voice_choice.GetSelection()
        if idx != wx.NOT_FOUND:
            return self.voice_choice.GetString(idx)
        return "th-TH-PremwadeeNeural"

    def audio_worker(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        while True:
            raw_text, is_test = self.speech_queue.get()
            if not self.is_running and not is_test:
                self.speech_queue.task_done()
                continue

            text = clean_text_for_tts(raw_text)
            if not text:
                self.speech_queue.task_done()
                continue

            temp_file = os.path.join(
                tempfile.gettempdir(), f"yt_tts_{uuid.uuid4().hex}.mp3"
            )
            audio_ready = False
            chosen_voice = self.get_selected_voice()

            # 1. ยิงตรงไปที่ api.msedgeservices.com (Cognitive Services Endpoint)
            try:
                audio_ready = loop.run_until_complete(
                    synthesize_msedgeservices_ws(text, chosen_voice, temp_file)
                )
                if audio_ready and is_test:
                    self.log(f"กำลังเล่นเสียงผ่าน Edge Cognitive ({chosen_voice})")
            except Exception as e:
                self.log(f"Edge Cognitive ({chosen_voice}) ขัดข้อง: {e}")

            # 2. หากเสียงที่เลือกไม่รองรับ ลองสลับเป็น PremwadeeNeural ผ่าน Endpoint เดียวกัน
            if not audio_ready and chosen_voice != "th-TH-PremwadeeNeural":
                try:
                    audio_ready = loop.run_until_complete(
                        synthesize_msedgeservices_ws(
                            text, "th-TH-PremwadeeNeural", temp_file
                        )
                    )
                except Exception:
                    pass

            # 3. เสียงสำรอง gTTS (ทำงานเฉพาะเมื่อ Edge ล้มเหลวจริงๆ)
            if not audio_ready and self.fallback_cb.GetValue():
                try:
                    tts = gTTS(text=text, lang="th")
                    tts.save(temp_file)
                    if os.path.exists(temp_file) and os.path.getsize(temp_file) > 512:
                        audio_ready = True
                        if is_test:
                            self.log("กำลังเล่นเสียงทดสอบผ่าน: gTTS (เสียงสำรอง)")
                except Exception as e:
                    self.log(f"เสียงสำรอง gTTS ขัดข้อง: {e}")

            # เล่นไฟล์เสียง
            if audio_ready and os.path.exists(temp_file):
                try:
                    pygame.mixer.music.load(temp_file)
                    pygame.mixer.music.play()
                    while pygame.mixer.music.get_busy() and (
                        self.is_running or is_test
                    ):
                        time.sleep(0.1)
                    pygame.mixer.music.unload()
                except Exception as e:
                    self.log(f"เล่นไฟล์เสียงไม่สำเร็จ: {e}")

            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

            self.speech_queue.task_done()


if __name__ == "__main__":
    app = wx.App(False)
    frame = YouTubeTTSFrame()
    frame.Show()
    app.MainLoop()
