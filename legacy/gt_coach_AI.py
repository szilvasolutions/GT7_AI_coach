import socket
import struct
import sys
import time
import threading
import queue
import csv
import math
import os
import warnings
import datetime
from google import genai
import pyttsx3
from Crypto.Cipher import Salsa20

# --- CONFIGURATION ---
CONFIG = {
    # Network
    'PS5_IP': '192.168.1.120',   
    'PORT_RX': 33740,
    'PORT_TX': 33739,
    
    # AI
    'GEMINI_KEY': "gemini-XXXXXXXXXXXXXXXX", 
    'AI_MODEL': "gemini-2.5-flash-lite",
    
    # Audio
    'VOICE_SPEED': 230,
    
    # --- SMART LATCH TRIGGERS ---
    'MIN_SPEED': 45,             
    
    # SMOOTH DRIVER FIX: 
    # Ignore the first 25% of brake pedal travel (approx input 65).
    # This prevents the AI from seeing the "feathering" phase as "hesitation".
    'ENTRY_BRAKE': 65,           
    'ENTRY_G_LAT': 0.95,         
    
    # --- EXIT CONDITIONS ---
    'EXIT_THROTTLE': 230,        
    'EXIT_STEER_RAD': 0.08,      
    
    # --- DATA RESOLUTION ---
    'TRACE_INTERVAL': 0.05,      # Record data every 0.05s (20Hz)
    
    # --- POST-FILTER ---
    'MIN_DURATION': 1.5,         
    'MIN_ROTATION': 35,          
    'MIN_PEAK_G': 1.05           
}

# --- SETUP LOGGING & VOICE ---
logging_lock = threading.Lock()
def log_print(msg):
    with logging_lock:
        print(msg)

engine = pyttsx3.init()
engine.setProperty('rate', CONFIG['VOICE_SPEED'])

def speak_worker(q):
    while True:
        text = q.get()
        if text is None: break
        try:
            engine.say(text)
            engine.runAndWait()
        except: pass
        q.task_done()

voice_q = queue.Queue()
threading.Thread(target=speak_worker, args=(voice_q,), daemon=True).start()

# --- GT7 PACKET DECRYPTION ---
def salsa20_dec(dat):
    key = b'Simulator Interface Packet GT7__'
    if len(dat) > 0:
        oiv = dat[0x40:0x44]
        iv1 = int.from_bytes(oiv, byteorder='little')
        iv2 = iv1 ^ 0xDEADBEEF
        IV = byteorder_iv = iv2.to_bytes(4, 'little') + oiv
        cipher = Salsa20.new(key=key, nonce=IV)
        return cipher.decrypt(dat)
    return b''

# --- AI BRAIN ---
class CoachBrain:
    def __init__(self):
        self.client = genai.Client(api_key=CONFIG['GEMINI_KEY'])

    def analyze(self, trace_data, turn_stats):
        csv_text = "\n".join(trace_data)
        
        prompt = f"""
        ROLE: Expert Racing Driver Coach.
        CONTEXT: Gran Turismo 7 Telemetry.
        
        [DRIVER PROFILE]
        Style: SMOOTH / PROGRESSIVE.
        Note: The driver applies brakes smoothly. DO NOT critique the "ramp up" speed of braking. 
        Only critique if the TOTAL braking force was insufficient or the release was poor.
        
        TURN STATS:
        - Duration: {turn_stats['duration']:.2f}s
        - Entry Speed: {turn_stats['entry_spd']} km/h
        - Exit Speed: {turn_stats['exit_spd']} km/h
        - Peak G-Force: {turn_stats['peak_g']:.2f} G
        
        DATA LEGEND:
        Time | Spd | Thr | Brk | G-Lat | FL_Slip | FR_Slip | RL_Slip | RR_Slip
        
        [TELEMETRY TRACE (0.05s Interval)]
        {csv_text}
        
        INSTRUCTION:
        Analyze the corner. Focus on:
        1. UNDERSTEER (Front Slip > Rear Slip)
        2. OVERSTEER (Rear Slip > Front Slip)
        3. EXIT SPEED
        
        Give ONE short, direct coaching command (Max 12 words).
        """
        
        try:
            response = self.client.models.generate_content(
                model=CONFIG['AI_MODEL'], contents=prompt
            )
            advice = response.text.replace("Advice:", "").strip()
            advice = advice.replace("*", "")
            
            # Post-Process Safety:
            if "brake later" in advice.lower() and turn_stats['peak_g'] > 1.35:
                log_print(f"xx SUPPRESSED (Bad Advice): {advice} xx")
            else:
                log_print(f"\n🤖 COACH: {advice}")
                voice_q.put(advice)
                
        except Exception as e:
            log_print(f"AI Error: {e}")

# --- SMART LATCH LOGIC ---
class SmartLatch:
    def __init__(self, brain):
        self.brain = brain
        self.state = "STRAIGHT"
        self.buffer = []
        self.start_time = 0
        self.last_log_time = 0
        self.turn_stats = {'total_yaw': 0.0, 'peak_g': 0.0}
        
        self.session_file = f"gt7_v62_{datetime.datetime.now().strftime('%H%M%S')}.csv"
        with open(self.session_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["Time", "State", "Speed", "Thr", "Brk", "G_Lat", "FL_Slip", "FR_Slip", "RL_Slip", "RR_Slip"])

    def process(self, packet):
        now = time.time()
        
        speed = packet['speed'] * 3.6 
        thr = packet['thr']
        brk = packet['brk']
        steer = packet['steer'] 
        g_lat = packet['g_lat']
        g_sum = math.sqrt(packet['g_lat']**2 + packet['g_lon']**2)
        
        # Slip Calcs
        v_car = max(packet['speed'], 1.0)
        r_tire = 0.33 
        fl_slip = (packet['w_fl'] * r_tire) / v_car
        fr_slip = (packet['w_fr'] * r_tire) / v_car
        rl_slip = (packet['w_rl'] * r_tire) / v_car
        rr_slip = (packet['w_rr'] * r_tire) / v_car
        
        # --- STATE MACHINE ---
        if self.state == "STRAIGHT":
            if speed > CONFIG['MIN_SPEED']:
                # Trigger on meaningful input
                if brk > CONFIG['ENTRY_BRAKE'] or abs(g_lat) > CONFIG['ENTRY_G_LAT']:
                    self.state = "CORNERING"
                    self.start_time = now
                    self.last_log_time = now
                    self.buffer = []
                    self.turn_stats = {
                        'total_yaw': 0.0, 
                        'peak_g': 0.0, 
                        'min_spd': speed, 
                        'entry_spd': speed, 
                        'exit_spd': 0
                    }
                    log_print(f"--> ENTER TURN ({int(speed)}kmh)")

        elif self.state == "CORNERING":
            dt = 0.016 
            
            # 1. Update Stats EVERY FRAME (High Frequency)
            self.turn_stats['total_yaw'] += abs(packet['yaw_rate'] * dt)
            self.turn_stats['peak_g'] = max(self.turn_stats['peak_g'], g_sum)
            self.turn_stats['min_spd'] = min(self.turn_stats['min_spd'], speed)
            
            # 2. Record Data at 0.05s INTERVALS (Resolution Control)
            if (now - self.last_log_time) >= CONFIG['TRACE_INTERVAL']:
                t_rel = now - self.start_time
                slip_str = f"{fl_slip:.2f}|{fr_slip:.2f}|{rl_slip:.2f}|{rr_slip:.2f}"
                row = f"{t_rel:.2f}|{int(speed)}|{thr}|{brk}|{g_lat:.2f}|{slip_str}"
                self.buffer.append(row)
                self.last_log_time = now
            
            # 3. Check Exit
            is_exit = (thr > CONFIG['EXIT_THROTTLE']) and (abs(steer) < CONFIG['EXIT_STEER_RAD'])
            is_timeout = (now - self.start_time) > 15.0 
            
            if is_exit or is_timeout:
                self.turn_stats['exit_spd'] = speed
                self.turn_stats['duration'] = now - self.start_time
                self._finish_turn()

        # Log to CSV
        with open(self.session_file, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([f"{now:.3f}", self.state, int(speed), thr, brk, f"{g_lat:.2f}", fl_slip, fr_slip, rl_slip, rr_slip])

    def _finish_turn(self):
        valid = True
        reason = ""
        
        if self.turn_stats['duration'] < CONFIG['MIN_DURATION']:
            valid = False; reason = "Too Short"
        elif self.turn_stats['peak_g'] < CONFIG['MIN_PEAK_G']:
            valid = False; reason = "Low G (Coasting)"
        elif math.degrees(self.turn_stats['total_yaw']) < CONFIG['MIN_ROTATION']: 
            valid = False; reason = "Low Rotation"

        if valid:
            log_print(f"<-- EXIT TURN: {len(self.buffer)} frames (0.05s res). Sending...")
            t = threading.Thread(target=self.brain.analyze, args=(list(self.buffer), self.turn_stats))
            t.start()
        else:
            log_print(f"xx IGNORED TURN ({reason}) xx")
            
        self.state = "STRAIGHT"

# --- MAIN LOOP ---
def main():
    UDP_IP = CONFIG['PS5_IP']
    UDP_PORT = CONFIG['PORT_RX']
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', UDP_PORT))
    sock.settimeout(1.0)
    
    log_print(f"GT7 V62 (SmartLatch 0.05s + SmoothFix) Listening on {UDP_PORT}...")
    
    brain = CoachBrain()
    latch = SmartLatch(brain)
    
    def send_heartbeat():
        while True:
            try:
                sock.sendto(b'A', (CONFIG['PS5_IP'], CONFIG['PORT_TX']))
            except: pass
            time.sleep(10)
            
    if CONFIG['PS5_IP']:
        threading.Thread(target=send_heartbeat, daemon=True).start()
    
    while True:
        try:
            data, addr = sock.recvfrom(4096)
            if len(data) == 296: 
                dec = salsa20_dec(data)
                
                speed = struct.unpack('f', dec[0x4C:0x50])[0]
                thr = dec[0x91]
                brk = dec[0x92]
                ang_vel_y = struct.unpack('f', dec[0x2C:0x30])[0] 
                
                w_fl = struct.unpack('f', dec[0xA0:0xA4])[0]
                w_fr = struct.unpack('f', dec[0xA4:0xA8])[0]
                w_rl = struct.unpack('f', dec[0xA8:0xAC])[0]
                w_rr = struct.unpack('f', dec[0xAC:0xB0])[0]
                
                pkt = {
                    'pkt_id': int.from_bytes(dec[0x00:0x04], 'little'),
                    'speed': speed,
                    'thr': thr,
                    'brk': brk,
                    'steer': 0, 
                    'yaw_rate': ang_vel_y,
                    'g_lat': (speed * ang_vel_y) / 9.81,
                    'g_lon': 0, 
                    'w_fl': abs(w_fl),
                    'w_fr': abs(w_fr),
                    'w_rl': abs(w_rl),
                    'w_rr': abs(w_rr)
                }
                
                latch.process(pkt)
                
        except socket.timeout:
            pass
        except Exception as e:
            pass

if __name__ == "__main__":
    if not CONFIG['PS5_IP']:
        log_print("Scanning for PS5...")
    main()