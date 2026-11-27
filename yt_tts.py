# ==============================================================================
# YouTube Live TTS Reader (NVDA / Screen Reader Friendly - wxPython Edition)
# ==============================================================================
# วิธีติดตั้งไลบรารีสำหรับรันทดสอบเอง:
#   pip install wxPython pytchat edge-tts==7.3.1 gTTS pygame certifi pywin32
#
# คำสั่งบีบอัดเป็นไฟล์ .exe เดียวจบ:
#   pyinstaller --noconsole --onefile --collect-all certifi --name yt_tts yt_tts.py
# ==============================================================================

import asyncio
import os
import queue
import random
import re
import sys
import tempfile
import threading
import time

import certifi
import edge_tts
from gtts import gTTS
import pygame
import pytchat
import wx

# SAPI5 ใช้ได้เฉพาะ Windows (ต้องมี pywin32) ถ้าไม่มีก็ตัดตัวเลือกนี้ออกอัตโนมัติ
try:
    import pythoncom
    import win32com.client

    SAPI_AVAILABLE = sys.platform == "win32"
except Exception:  # pragma: no cover
    SAPI_AVAILABLE = False

# บังคับใช้ SSL Certificate ของ certifi เพื่อป้องกันปัญหา HTTPS/WSS บน Windows Standalone
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

DEFAULT_EDGE_VOICE = "th-TH-PremwadeeNeural"

# รหัสภายในของแต่ละ Engine และชื่อที่แสดงในหน้าจอ
ENGINE_LABELS = {
    "edge": "Edge TTS (ออนไลน์)",
    "gtts": "Google TTS / gTTS (ออนไลน์)",
    "sapi": "SAPI5 (เสียงในเครื่อง Windows)",
}
SELECTABLE_ENGINES = ["edge", "gtts"] + (["sapi"] if SAPI_AVAILABLE else [])

# gTTS ไม่มีให้เลือกเสียง มีแค่ภาษา
GTTS_VOICES = ["ภาษาไทย (th)"]

# รายการ custom checkbox ของหมวดข้อความ: (รหัสภายใน, ป้ายที่แสดง, ค่าเริ่มต้น)
CONTENT_TYPES = [
    ("normal", "ข้อความแชทปกติ (Normal Chat)", False),
    ("superchat", "Super Chat (โดเนทข้อความ)", True),
    ("sticker", "Super Sticker (โดเนทสติกเกอร์)", True),
    ("member", "New Member / Milestone (สมัครสมาชิกใหม่)", True),
]

# รายการ custom checkbox ของตัวเลือกเสียง: (ป้ายที่แสดง, ค่าเริ่มต้น)
VOICE_OPTIONS = [
    ("สุ่มเสียงอัตโนมัติ (จากเสียงของ Engine ที่เลือก)", False),
    ("ใช้เสียงสำรองอัตโนมัติหากเสียงหลักขัดข้อง (Edge, gTTS, SAPI5)", True),
]


class YouTubeTTSFrame(wx.Frame):
    def __init__(self):
        super().__init__(
            None,
            title="YouTube Live TTS Reader",
            size=(560, 700),
            style=wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX),
        )

        # ตัวแปรควบคุมระบบ
        self.is_running = False
        self.speech_queue = queue.Queue()
        self.edge_voices = ["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"]
        self.sapi_voices = []
        # ค่าตั้งค่าที่ thread อื่นอ่านได้อย่างปลอดภัย (อัปเดตจาก GUI thread เท่านั้น)
        self.cfg = {}

        # เริ่มระบบเสียง Pygame
        pygame.mixer.init()

        self.init_ui()
        self.setup_accelerators()
        self.Centre()

        self.populate_voices()
        self.refresh_cfg()

        # โฟกัสที่ช่องใส่ URL ทันทีเมื่อเปิดโปรแกรม
        self.url_text.SetFocus()

        # โหลดรายชื่อเสียงในพื้นหลัง
        threading.Thread(target=self.load_voices_thread, daemon=True).start()
        # เริ่ม Worker รออ่านคิวเสียง
        threading.Thread(target=self.audio_worker, daemon=True).start()

        self.Bind(wx.EVT_CLOSE, self.on_close)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # --- ส่วนที่ 1: ช่องใส่ URL ---
        url_box = wx.StaticBox(panel, label="การเชื่อมต่อไลฟ์สด")
        url_sizer = wx.StaticBoxSizer(url_box, wx.VERTICAL)

        url_lbl = wx.StaticText(url_box, label="YouTube Live &URL หรือ Video ID:")
        self.url_text = wx.TextCtrl(
            url_box, name="YouTube Live URL หรือ Video ID"
        )
        self.url_text.SetHint("วางลิงก์ไลฟ์สดหรือรหัส Video ID 11 หลักที่นี่")

        url_sizer.Add(url_lbl, 0, wx.ALL, 5)
        url_sizer.Add(self.url_text, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        main_sizer.Add(url_sizer, 0, wx.EXPAND | wx.ALL, 10)

        # --- ส่วนที่ 2: ตั้งค่าเสียง ---
        voice_box = wx.StaticBox(panel, label="ตั้งค่าเสียงอ่าน (TTS)")
        voice_sizer = wx.StaticBoxSizer(voice_box, wx.VERTICAL)

        grid = wx.FlexGridSizer(rows=0, cols=2, vgap=6, hgap=8)
        grid.AddGrowableCol(1)

        engine_lbl = wx.StaticText(voice_box, label="เอนจินเสียง (&E):")
        self.engine_choice = wx.Choice(
            voice_box,
            choices=[ENGINE_LABELS[e] for e in SELECTABLE_ENGINES],
            name="เอนจินเสียง",
        )
        self.engine_choice.SetSelection(0)
        self.engine_choice.Bind(wx.EVT_CHOICE, self.on_engine_changed)

        voice_lbl = wx.StaticText(voice_box, label="เสียงอ่านหลัก (&V):")
        self.voice_choice = wx.Choice(voice_box, name="เสียงอ่านหลัก")
        self.voice_choice.Bind(wx.EVT_CHOICE, self.refresh_cfg)

        options_lbl = wx.StaticText(voice_box, label="ตัวเลือกเสียง (&O):")
        self.options_list = wx.CheckListBox(
            voice_box,
            choices=[label for label, _ in VOICE_OPTIONS],
            size=(-1, 54),
            name="ตัวเลือกเสียง",
        )
        for i, (_, default) in enumerate(VOICE_OPTIONS):
            self.options_list.Check(i, default)
        self.options_list.SetSelection(0)
        self.options_list.Bind(wx.EVT_CHECKLISTBOX, self.refresh_cfg)

        speed_lbl = wx.StaticText(voice_box, label="ความเร็วในการอ่าน % (&R):")
        self.speed_slider = wx.Slider(
            voice_box, value=100, minValue=50, maxValue=200,
            style=wx.SL_HORIZONTAL | wx.SL_LABELS, name="ความเร็วในการอ่าน",
        )
        self.speed_slider.SetPageSize(10)
        self.speed_slider.Bind(wx.EVT_SLIDER, self.refresh_cfg)

        volume_lbl = wx.StaticText(voice_box, label="ความดังเสียง % (&D):")
        self.volume_slider = wx.Slider(
            voice_box, value=100, minValue=0, maxValue=100,
            style=wx.SL_HORIZONTAL | wx.SL_LABELS, name="ความดังเสียง",
        )
        self.volume_slider.SetPageSize(10)
        self.volume_slider.Bind(wx.EVT_SLIDER, self.refresh_cfg)

        grid.Add(engine_lbl, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.engine_choice, 1, wx.EXPAND)
        grid.Add(voice_lbl, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.voice_choice, 1, wx.EXPAND)
        grid.Add(speed_lbl, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.speed_slider, 1, wx.EXPAND)
        grid.Add(volume_lbl, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.volume_slider, 1, wx.EXPAND)
        grid.Add(options_lbl, 0, wx.ALIGN_TOP | wx.TOP, 3)
        grid.Add(self.options_list, 1, wx.EXPAND)

        voice_sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 8)
        main_sizer.Add(voice_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- ส่วนที่ 3: เลือกหมวดข้อความที่จะให้อ่าน (custom checkbox) ---
        filter_box = wx.StaticBox(panel, label="หมวดข้อความที่จะให้อ่านออกเสียง")
        filter_sizer = wx.StaticBoxSizer(filter_box, wx.VERTICAL)

        types_lbl = wx.StaticText(filter_box, label="เลือกหมวดที่จะอ่าน (&C):")
        self.types_list = wx.CheckListBox(
            filter_box,
            choices=[label for _, label, _ in CONTENT_TYPES],
            size=(-1, 90),
            name="เลือกหมวดที่จะอ่าน",
        )
        for i, (_, _, default) in enumerate(CONTENT_TYPES):
            self.types_list.Check(i, default)
        self.types_list.SetSelection(0)
        self.types_list.Bind(wx.EVT_CHECKLISTBOX, self.refresh_cfg)

        filter_sizer.Add(types_lbl, 0, wx.ALL, 5)
        filter_sizer.Add(self.types_list, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
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
        log_lbl = wx.StaticText(panel, label="บันทึกการทำงาน (&L):")
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

    # ------------------------------------------------------------------
    # Log / Config
    # ------------------------------------------------------------------
    def log(self, message):
        wx.CallAfter(self._append_log, message)

    def _append_log(self, message):
        if self.log_text:
            self.log_text.AppendText(message + "\n")

    def current_engine(self):
        idx = self.engine_choice.GetSelection()
        if 0 <= idx < len(SELECTABLE_ENGINES):
            return SELECTABLE_ENGINES[idx]
        return "edge"

    def refresh_cfg(self, event=None):
        """สรุปค่าจากหน้าจอเก็บไว้ใน dict ให้ thread อื่นอ่าน (ห้ามแตะ GUI จาก thread อื่น)"""
        sel = self.voice_choice.GetSelection()
        voice = self.voice_choice.GetString(sel) if sel != wx.NOT_FOUND else ""
        random_on = self.options_list.IsChecked(0)

        # ถ้าสุ่มเสียงอยู่ ช่องเลือกเสียงจะไม่มีผล จึงปิดไว้
        self.voice_choice.Enable(not random_on)

        self.cfg = {
            "engine": self.current_engine(),
            "voice": voice,
            "random": random_on,
            "fallback": self.options_list.IsChecked(1),
            "speed": self.speed_slider.GetValue(),
            "volume": self.volume_slider.GetValue(),
            "types": {
                key: self.types_list.IsChecked(i)
                for i, (key, _, _) in enumerate(CONTENT_TYPES)
            },
        }
        if event is not None:
            event.Skip()

    def on_engine_changed(self, event):
        self.populate_voices()
        self.refresh_cfg()
        event.Skip()

    def populate_voices(self):
        engine = self.current_engine()
        if engine == "sapi":
            voices = self.sapi_voices
        elif engine == "gtts":
            voices = GTTS_VOICES
        else:
            voices = self.edge_voices
        self.voice_choice.Set(list(voices))
        if not voices:
            return
        if engine == "edge" and DEFAULT_EDGE_VOICE in voices:
            self.voice_choice.SetSelection(voices.index(DEFAULT_EDGE_VOICE))
        else:
            self.voice_choice.SetSelection(0)

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
        # ลบอักขระควบคุม และอักขระที่ทำให้ XML/SSML ของ TTS เสียรูปแบบ
        cleaned = re.sub(r"[\x00-\x1F\x7F]", " ", str(text))
        cleaned = re.sub(r'[<>"&]', " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    # ------------------------------------------------------------------
    # โหลดรายชื่อเสียง
    # ------------------------------------------------------------------
    def load_voices_thread(self):
        # SAPI5 (ออฟไลน์ เร็ว) โหลดก่อน
        if SAPI_AVAILABLE:
            try:
                pythoncom.CoInitialize()
                try:
                    sp = win32com.client.Dispatch("SAPI.SpVoice")
                    tokens = sp.GetVoices()
                    names = [
                        tokens.Item(i).GetDescription() for i in range(tokens.Count)
                    ]
                finally:
                    pythoncom.CoUninitialize()
                self.sapi_voices = names
                self.log(f"พบเสียง SAPI5 ในเครื่อง {len(names)} เสียง")
            except Exception as e:
                self.log(f"โหลดรายชื่อเสียง SAPI5 ไม่สำเร็จ: {e}")

        # Edge TTS (ต้องใช้อินเทอร์เน็ต)
        try:
            loop = asyncio.new_event_loop()
            try:
                voices = loop.run_until_complete(edge_tts.list_voices())
            finally:
                loop.close()

            filtered = [
                v["ShortName"]
                for v in voices
                if v["Locale"].startswith("th-TH") or "Multilingual" in v["ShortName"]
            ]
            if filtered:
                self.edge_voices = sorted(filtered)
                self.log(
                    f"โหลดรายชื่อเสียง Edge สำเร็จ ({len(self.edge_voices)} เสียง)"
                )
        except Exception as e:
            self.log(f"โหลดรายชื่อเสียง Edge ไม่สำเร็จ (ใช้เสียงพื้นฐานแทน): {e}")

        wx.CallAfter(self._update_voice_list)

    def _update_voice_list(self):
        self.populate_voices()
        self.refresh_cfg()

    # ------------------------------------------------------------------
    # ปุ่มทดสอบ / เริ่ม / หยุด
    # ------------------------------------------------------------------
    def on_test_voice(self, event):
        sample_text = "สวัสดีครับ ทดสอบระบบอ่านออกเสียงภาษาไทย"
        self.log(f"กำลังทดสอบเสียง: {sample_text}")
        self.speech_queue.put((sample_text, True))

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

    # ------------------------------------------------------------------
    # ตัวดึงแชทสด
    # ------------------------------------------------------------------
    def chat_listener_thread(self, video_id):
        try:
            chat = pytchat.create(video_id=video_id, interruptable=False)
            self.log("เชื่อมต่อแชทสำเร็จ! กำลังรอรับข้อความ...")

            while self.is_running and chat.is_alive():
                types = self.cfg.get("types", {})
                for c in chat.get().sync_items():
                    if not self.is_running:
                        break

                    msg_type = c.type
                    author = self.clean_text_for_tts(c.author.name)
                    msg = self.clean_text_for_tts(c.message)
                    text_to_speak = None

                    if msg_type == "textMessage" and types.get("normal"):
                        if msg:
                            text_to_speak = f"{author} พูดว่า {msg}"

                    elif msg_type == "superChat" and types.get("superchat"):
                        amount = getattr(c, "amountString", "")
                        text_to_speak = (
                            f"ขอบคุณคุณ {author} สำหรับซุปเปอร์แชท {amount} ครับ {msg}"
                        )

                    elif msg_type == "superSticker" and types.get("sticker"):
                        amount = getattr(c, "amountString", "")
                        text_to_speak = (
                            f"ขอบคุณคุณ {author} สำหรับซุปเปอร์สติกเกอร์ {amount} ครับ"
                        )

                    elif msg_type == "newSponsor" and types.get("member"):
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

    # ------------------------------------------------------------------
    # Engine สังเคราะห์เสียง (ทำงานใน audio_worker thread เท่านั้น)
    # ------------------------------------------------------------------
    async def _edge_save_audio(self, text, voice, out_path, rate="+0%"):
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        audio_bytes = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes.extend(chunk["data"])
        if len(audio_bytes) == 0:
            raise RuntimeError("ได้รับข้อมูลเสียงว่างเปล่าจากเซิร์ฟเวอร์")
        with open(out_path, "wb") as f:
            f.write(audio_bytes)

    def _play_file(self, path, is_test):
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(max(0.0, min(1.0, self.cfg["volume"] / 100)))
        pygame.mixer.music.play()
        try:
            while pygame.mixer.music.get_busy() and (self.is_running or is_test):
                time.sleep(0.1)
        finally:
            try:
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()
            except Exception:
                pass

    def _new_temp_path(self):
        return os.path.join(
            tempfile.gettempdir(),
            f"yt_tts_{os.getpid()}_{random.randint(100000, 999999)}.mp3",
        )

    def _speak_edge(self, text, cfg, primary, is_test):
        voices = self.edge_voices
        if primary and cfg["random"] and voices:
            voice = random.choice(voices)
        elif cfg["voice"] in voices and cfg["engine"] == "edge":
            voice = cfg["voice"]
        else:
            voice = DEFAULT_EDGE_VOICE

        rate = f"{cfg['speed'] - 100:+d}%"  # 50..200% -> -50%..+100%

        candidates = [voice]
        if voice != DEFAULT_EDGE_VOICE:
            candidates.append(DEFAULT_EDGE_VOICE)

        temp_file = self._new_temp_path()
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            for v in candidates:
                try:
                    loop.run_until_complete(
                        self._edge_save_audio(text, v, temp_file, rate)
                    )
                except Exception as e:
                    self.log(f"Edge TTS ({v}) ขัดข้อง: {e}")
                    continue
                if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
                    if is_test:
                        self.log(f"กำลังเล่นเสียงทดสอบผ่าน: Edge TTS ({v})")
                    self._play_file(temp_file, is_test)
                    return True
            return False
        finally:
            loop.close()
            self._remove_quietly(temp_file)

    def _speak_gtts(self, text, is_test):
        temp_file = self._new_temp_path()
        try:
            # gTTS ปรับความเร็วได้แค่ปกติ/ช้า (ต่ำกว่า 80% ถือว่าช้า)
            slow = self.cfg["speed"] < 80
            gTTS(text=text, lang="th", slow=slow).save(temp_file)
            if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
                if is_test:
                    self.log("กำลังเล่นเสียงทดสอบผ่าน: gTTS")
                self._play_file(temp_file, is_test)
                return True
            return False
        except Exception as e:
            self.log(f"gTTS ขัดข้อง: {e}")
            return False
        finally:
            self._remove_quietly(temp_file)

    def _speak_sapi(self, text, cfg, primary, is_test):
        if not SAPI_AVAILABLE:
            return False
        try:
            voices = self.sapi_voices
            if primary and cfg["random"] and voices:
                voice_name = random.choice(voices)
            elif cfg["engine"] == "sapi" and cfg["voice"] in voices:
                voice_name = cfg["voice"]
            else:
                voice_name = ""  # ใช้เสียงเริ่มต้นของ Windows

            sp = win32com.client.Dispatch("SAPI.SpVoice")
            speed = cfg["speed"]
            # SAPI Rate: -10..10 (100% = 0) ; Volume: 0..100
            sp.Rate = int(round((speed - 100) / 10)) if speed >= 100 else int(round((speed - 100) / 5))
            sp.Rate = max(-10, min(10, sp.Rate))
            sp.Volume = max(0, min(100, cfg["volume"]))
            if voice_name:
                tokens = sp.GetVoices()
                for i in range(tokens.Count):
                    token = tokens.Item(i)
                    if token.GetDescription() == voice_name:
                        sp.Voice = token
                        break

            if is_test:
                self.log(f"กำลังเล่นเสียงทดสอบผ่าน: SAPI5 ({voice_name or 'ค่าเริ่มต้น'})")

            sp.Speak(text, 1)  # SVSFlagsAsync
            while not sp.WaitUntilDone(100):
                if not (self.is_running or is_test):
                    sp.Speak("", 3)  # Async + PurgeBeforeSpeak = หยุดทันที
                    break
            return True
        except Exception as e:
            self.log(f"SAPI5 ขัดข้อง: {e}")
            return False

    def speak(self, text, is_test):
        cfg = self.cfg
        primary = cfg["engine"]
        order = [primary]
        if cfg["fallback"]:
            order += [e for e in ("edge", "gtts", "sapi") if e != primary]

        for engine in order:
            if not self.is_running and not is_test:
                return
            is_primary = engine == primary
            if engine == "edge":
                ok = self._speak_edge(text, cfg, is_primary, is_test)
            elif engine == "gtts":
                ok = self._speak_gtts(text, is_test)
            else:
                ok = self._speak_sapi(text, cfg, is_primary, is_test)
            if ok:
                return

        self.log("ไม่สามารถสร้างเสียงได้จากทุก Engine")

    @staticmethod
    def _remove_quietly(path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # ตัวจัดการคิวและเล่นเสียง
    # ------------------------------------------------------------------
    def audio_worker(self):
        if SAPI_AVAILABLE:
            pythoncom.CoInitialize()
        while True:
            text, is_test = self.speech_queue.get()
            try:
                if self.is_running or is_test:
                    self.speak(text, is_test)
            except Exception as e:
                self.log(f"เกิดข้อผิดพลาดขณะเล่นเสียง: {e}")
            finally:
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
