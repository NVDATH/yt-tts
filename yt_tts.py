"""
===========================================================================
YouTube Live TTS Reader (อ่านออกเสียงแชทสด / Super Chat / Member อัตโนมัติ)
===========================================================================

ขั้นตอนการติดตั้งและใช้งาน:

1. ติดตั้งไลบรารีที่จำเป็น (เปิด Terminal / CMD แล้วรันคำสั่งนี้):
   pip install customtkinter pytchat edge-tts pygame pyinstaller

2. ทดสอบรันโปรแกรมด้วย Python:
   python yt_tts.py

3. (ทางเลือก) บีบอัดเป็นไฟล์ .exe เดียวจบ เพื่อนำไปใช้โดยไม่ต้องเปิดผ่าน Python:
   pyinstaller --noconsole --onefile --collect-all customtkinter yt_tts.py
   
   * ไฟล์โปรแกรมสำเร็จรูปจะอยู่ที่โฟลเดอร์ dist/yt_tts.exe
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
import customtkinter as ctk
import edge_tts
import pygame
import pytchat

# ตั้งค่าธีมของ UI
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class YouTubeTTSApp(ctk.CTk):

  def __init__(self):
    super().__init__()

    self.title("YouTube Live TTS Reader")
    self.geometry("540x620")
    self.resizable(False, False)

    # ตัวแปรควบคุมระบบ
    self.is_running = False
    self.speech_queue = queue.Queue()
    self.voice_list = ["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"]

    # เริ่มระบบเสียง Pygame
    pygame.mixer.init()

    self.setup_ui()

    # โหลดรายชื่อเสียง Multilingual ในพื้นหลังตอนเปิดโปรแกรม
    threading.Thread(target=self.load_voices_thread, daemon=True).start()
    # เริ่ม Worker รออ่านคิวเสียง
    threading.Thread(target=self.audio_worker, daemon=True).start()

  def setup_ui(self):
    # --- ส่วนที่ 1: ช่องใส่ URL ---
    url_frame = ctk.CTkFrame(self)
    url_frame.pack(pady=10, padx=15, fill="x")

    ctk.CTkLabel(
        url_frame,
        text="YouTube Live URL หรือ Video ID:",
        font=("Arial", 13, "bold"),
    ).pack(anchor="w", padx=10, pady=(8, 2))
    self.url_entry = ctk.CTkEntry(
        url_frame,
        placeholder_text="เช่น https://www.youtube.com/watch?v=xxxxxxxxxxx",
    )
    self.url_entry.pack(fill="x", padx=10, pady=(0, 10))

    # --- ส่วนที่ 2: ตั้งค่าเสียง ---
    voice_frame = ctk.CTkFrame(self)
    voice_frame.pack(pady=5, padx=15, fill="x")

    ctk.CTkLabel(
        voice_frame, text="ตั้งค่าเสียงอ่าน:", font=("Arial", 13, "bold")
    ).pack(anchor="w", padx=10, pady=(8, 2))

    self.random_voice_var = ctk.BooleanVar(value=False)
    self.random_cb = ctk.CTkCheckBox(
        voice_frame,
        text="สุ่มเสียงอัตโนมัติ (จากเสียงไทย & Multilingual)",
        variable=self.random_voice_var,
        command=self.toggle_voice_dropdown,
    )
    self.random_cb.pack(anchor="w", padx=10, pady=5)

    self.voice_dropdown = ctk.CTkOptionMenu(
        voice_frame, values=self.voice_list
    )
    self.voice_dropdown.pack(fill="x", padx=10, pady=(2, 10))

    # --- ส่วนที่ 3: เลือกหมวดข้อความที่จะให้อ่าน ---
    filter_frame = ctk.CTkFrame(self)
    filter_frame.pack(pady=5, padx=15, fill="x")

    ctk.CTkLabel(
        filter_frame,
        text="เลือกหมวดข้อความที่จะให้อ่านออกเสียง:",
        font=("Arial", 13, "bold"),
    ).pack(anchor="w", padx=10, pady=(8, 2))

    self.read_normal_var = ctk.BooleanVar(value=False)
    self.read_superchat_var = ctk.BooleanVar(value=True)
    self.read_sticker_var = ctk.BooleanVar(value=True)
    self.read_member_var = ctk.BooleanVar(value=True)

    ctk.CTkCheckBox(
        filter_frame,
        text="ข้อความแชทปกติ (Normal Chat)",
        variable=self.read_normal_var,
    ).pack(anchor="w", padx=10, pady=3)
    ctk.CTkCheckBox(
        filter_frame,
        text="Super Chat (โดเนทข้อความ)",
        variable=self.read_superchat_var,
    ).pack(anchor="w", padx=10, pady=3)
    ctk.CTkCheckBox(
        filter_frame,
        text="Super Sticker (โดเนทสติกเกอร์)",
        variable=self.read_sticker_var,
    ).pack(anchor="w", padx=10, pady=3)
    ctk.CTkCheckBox(
        filter_frame,
        text="New Member / Milestone (สมัครสมาชิกใหม่)",
        variable=self.read_member_var,
    ).pack(anchor="w", padx=10, pady=(3, 10))

    # --- ส่วนที่ 4: ปุ่มควบคุม ---
    btn_frame = ctk.CTkFrame(self, fg_color="transparent")
    btn_frame.pack(pady=10, padx=15, fill="x")

    self.start_btn = ctk.CTkButton(
        btn_frame,
        text="▶ เริ่มอ่านแชท",
        fg_color="#28a745",
        hover_color="#218838",
        command=self.start_bot,
    )
    self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 5))

    self.stop_btn = ctk.CTkButton(
        btn_frame,
        text="⏹ หยุดทำงาน",
        fg_color="#dc3545",
        hover_color="#c82333",
        state="disabled",
        command=self.stop_bot,
    )
    self.stop_btn.pack(side="right", expand=True, fill="x", padx=(5, 0))

    # --- ส่วนที่ 5: กล่องแสดงสถานะ (Log) ---
    ctk.CTkLabel(self, text="บันทึกการทำงาน (Log):").pack(
        anchor="w", padx=15
    )
    self.log_box = ctk.CTkTextbox(self, height=160, state="disabled")
    self.log_box.pack(pady=(2, 15), padx=15, fill="both", expand=True)

  def log(self, message):
    self.log_box.configure(state="normal")
    self.log_box.insert("end", message + "\n")
    self.log_box.see("end")
    self.log_box.configure(state="disabled")

  def toggle_voice_dropdown(self):
    if self.random_voice_var.get():
      self.voice_dropdown.configure(state="disabled")
    else:
      self.voice_dropdown.configure(state="normal")

  def extract_video_id(self, url):
    # รองรับทั้งลิงก์เต็ม, ลิงก์สั้น youtu.be, ลิงก์ /live/ หรือใส่ ID ตรงๆ
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
          if v["Locale"].startswith("th-TH")
          or "Multilingual" in v["ShortName"]
      ]
      if filtered:
        self.voice_list = sorted(filtered)
        self.voice_dropdown.configure(values=self.voice_list)
        if "th-TH-PremwadeeNeural" in self.voice_list:
          self.voice_dropdown.set("th-TH-PremwadeeNeural")
        self.log(
            f"✅ โหลดรายชื่อเสียงสำเร็จ ({len(self.voice_list)} เสียงที่พูดไทยได้)"
        )
    except Exception as e:
      self.log(f"⚠️ โหลดรายชื่อเสียงไม่สำเร็จ (ใช้เสียงพื้นฐานแทน): {e}")

  # --- เริ่ม/หยุด การทำงาน ---
  def start_bot(self):
    raw_input = self.url_entry.get()
    video_id = self.extract_video_id(raw_input)
    if not video_id:
      self.log("❌ รูปแบบ URL หรือ Video ID ไม่ถูกต้อง")
      return

    self.is_running = True
    self.start_btn.configure(state="disabled")
    self.stop_btn.configure(state="normal")
    self.url_entry.configure(state="disabled")
    self.log(f"🔄 กำลังเชื่อมต่อกับแชทของ Video ID: {video_id} ...")

    threading.Thread(
        target=self.chat_listener_thread, args=(video_id,), daemon=True
    ).start()

  def stop_bot(self):
    self.is_running = False
    # ล้างคิวเสียงที่ค้างอยู่
    with self.speech_queue.mutex:
      self.speech_queue.queue.clear()
    pygame.mixer.music.stop()

    self.start_btn.configure(state="normal")
    self.stop_btn.configure(state="disabled")
    self.url_entry.configure(state="normal")
    self.log("🛑 หยุดการทำงานแล้ว")

  # --- ตัวดึงแชทสด ---
  def chat_listener_thread(self, video_id):
    try:
      chat = pytchat.create(video_id=video_id)
      self.log("🟢 เชื่อมต่อแชทสำเร็จ! กำลังรอรับข้อความ...")

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
            text_to_speak = (
                f"ขอบคุณคุณ {author} สำหรับซุปเปอร์แชท {amount} ครับ {msg}"
            )

          elif msg_type == "superSticker" and self.read_sticker_var.get():
            amount = c.amountString
            text_to_speak = (
                f"ขอบคุณคุณ {author} สำหรับซุปเปอร์สติกเกอร์ {amount} ครับ"
            )

          elif msg_type == "newSponsor" and self.read_member_var.get():
            text_to_speak = f"ยินดีต้อนรับคุณ {author} สู่การเป็นสมาชิกใหม่ครับ {msg}"

          if text_to_speak:
            self.log(f"💬 [{msg_type}] {text_to_speak}")
            self.speech_queue.put(text_to_speak)

        time.sleep(0.5)

      if self.is_running:
        self.log("⚠️ ไลฟ์สดจบลงแล้ว หรือการเชื่อมต่อถูกตัด")
        self.after(0, self.stop_bot)

    except Exception as e:
      self.log(f"❌ เกิดข้อผิดพลาดในการดึงแชท: {e}")
      self.after(0, self.stop_bot)

  # --- ตัวจัดการคิวและเล่นเสียง (ทำงานทีละคิว ไม่ตีกัน) ---
  def audio_worker(self):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    while True:
      text = self.speech_queue.get()
      if not self.is_running:
        self.speech_queue.task_done()
        continue

      try:
        # เลือกเสียง (สุ่ม หรือ ตามที่เลือก)
        if self.random_voice_var.get():
          voice = random.choice(self.voice_list)
        else:
          voice = self.voice_dropdown.get()

        # สร้างไฟล์เสียงชั่วคราวในโฟลเดอร์ Temp ของ Windows
        temp_file = os.path.join(
            tempfile.gettempdir(), f"tts_{random.randint(10000,99999)}.mp3"
        )

        communicate = edge_tts.Communicate(text, voice)
        loop.run_until_complete(communicate.save(temp_file))

        # เล่นเสียงและรอจนกว่าจะพูดจบ
        pygame.mixer.music.load(temp_file)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy() and self.is_running:
          time.sleep(0.1)

        pygame.mixer.music.unload()
        if os.path.exists(temp_file):
          os.remove(temp_file)

      except Exception as e:
        self.log(f"⚠️ อ่านออกเสียงไม่สำเร็จ: {e}")
      finally:
        self.speech_queue.task_done()


if __name__ == "__main__":
  app = YouTubeTTSApp()
  app.mainloop()
