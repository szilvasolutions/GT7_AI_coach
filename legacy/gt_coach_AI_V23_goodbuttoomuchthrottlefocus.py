# --- 1. CRITICAL WARNING SUPPRESSION ---
import os
import warnings
import logging
import datetime
import math
import statistics
import subprocess 
import winsound   
import pyttsx3    # Fallback voice

# Kill all warnings immediately
os.environ['GRPC_VERBOSITY'] = 'ERROR'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
warnings.simplefilter(action='ignore', category=FutureWarning)
warnings.simplefilter(action='ignore', category=UserWarning)
warnings.filterwarnings("ignore")

import socket
import struct
import sys
import time
import threading
import queue
import csv
from google import genai 
from Crypto.Cipher import Salsa20 

# --- CONFIGURATION (USER TUNING) ---
CONFIG = {
    'PS5_IP': None,              
    'LOCAL_IP': None,            
    'PORT_RX': 33740,
    'PORT_TX': 33739,
    'GEMINI_KEY': "gemini-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
    'VOICE_SPEED': 230,          
    'VOICE_MODEL': "en_US-amy-medium.onnx", 
    'TRIGGER_SENSITIVITY': 0.218, 
    'MIN_ROTATION_DEG': 35, 
    'COACH_LEVEL': 4,
    'LAP_ANALYSIS': True         
}

KEY = b'Simulator Interface Packet GT7 ver 0.0'[:32]
MAGIC_NONCE_OFFSET = 0xDEADBEAF

# 🗣️ PIPER CONFIGURATION
PIPER_FOLDER = r"C:\GT7\VOICE" 
PIPER_BINARY = os.path.join(PIPER_FOLDER, "piper.exe")
PIPER_MODEL  = os.path.join(PIPER_FOLDER, CONFIG['VOICE_MODEL'])
PIPER_CONFIG = PIPER_MODEL + ".json" 
TEMP_WAV     = os.path.join(PIPER_FOLDER, "temp_speech.wav")

# --- DYNAMIC LOGGING SYSTEM ---
SESSION_ID = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
AI_LOG_FILE = os.path.join(os.getcwd(), f"gt7_ai_log_{SESSION_ID}.txt")
DEBUG_LOG_FILE = os.path.join(os.getcwd(), f"gt7_debug_log_{SESSION_ID}.txt")

logging.basicConfig(
    filename=DEBUG_LOG_FILE, level=logging.DEBUG, filemode='w', 
    format='[%(asctime)s.%(msecs)03d] %(levelname)s: %(message)s', datefmt='%H:%M:%S'
)

def log_system(msg, level="info"):
    try:
        timestamp = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]
        if level in ["info", "error"]:
            sys.stdout.write(f"\n[{timestamp}] {msg}\n") 
            sys.stdout.flush()
        if level == "info": logging.info(msg)
        elif level == "error": logging.error(msg)
        elif level == "debug": logging.debug(msg)
    except: pass

def log_ai_interaction(turn_id, prompt_text, response_text):
    try:
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        with open(AI_LOG_FILE, "a", encoding='utf-8') as f:
            f.write(f"\n{'='*40}\nTIMESTAMP: {timestamp}\nLINK ID: {turn_id}\nPROMPT:\n{prompt_text}\nRESPONSE:\n{response_text}\n{'='*40}\n")
    except Exception as e:
        log_system(f"Failed to write AI log: {e}", "error")

log_system(f"--- SYSTEM STARTUP INITIATED (Session: {SESSION_ID}) ---")

# --- 2. NETWORK DISCOVERY ---
class NetworkScanner:
    @staticmethod
    def get_local_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        except Exception:
            local_ip = "127.0.0.1"
        finally:
            s.close()
        return local_ip

    @staticmethod
    def find_ps5(voice_sys):
        local_ip = NetworkScanner.get_local_ip()
        subnet = '.'.join(local_ip.split('.')[:-1]) + '.'
        
        log_system(f"🔍 Scanning subnet {subnet}1-254 for PS5 (My IP: {local_ip})...", "info")
        voice_sys.say("Scanning network...", important=True)
        
        scan_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        scan_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        scan_sock.settimeout(0.05) 
        
        try:
            scan_sock.bind((local_ip, CONFIG['PORT_RX']))
        except:
            scan_sock.bind(('0.0.0.0', CONFIG['PORT_RX']))

        def send_pings():
            for i in range(1, 255):
                target_ip = f"{subnet}{i}"
                if target_ip == local_ip: continue
                try:
                    scan_sock.sendto(b'A', (target_ip, CONFIG['PORT_TX']))
                    time.sleep(0.002) 
                except: pass
        
        t = threading.Thread(target=send_pings)
        t.start()
            
        scan_sock.settimeout(4.0) 
        found_ip = None
        try:
            data, addr = scan_sock.recvfrom(4096)
            found_ip = addr[0]
            log_system(f"✅ FOUND PS5 at: {found_ip}", "info")
            voice_sys.say("Console Found.", important=True)
        except socket.timeout:
            log_system("❌ PS5 Not Found via Auto-Scan.", "error")
        
        scan_sock.close()
        t.join() 
        return found_ip

# --- 3. VOICE SYSTEM ---
class VoiceSystem:
    def __init__(self):
        self.queue = queue.Queue()
        self.use_piper = False
        self.running = True
        
        if os.path.exists(TEMP_WAV):
            try: os.remove(TEMP_WAV)
            except: pass
        
        if os.path.exists(PIPER_BINARY) and os.path.exists(PIPER_MODEL):
            self.use_piper = True
            log_system(f"✅ Piper TTS Active ({CONFIG['VOICE_MODEL']})", "info")
        else:
            log_system("⚠️ Piper not found. Using Windows Voice.", "warning")

        self.thread = threading.Thread(target=self._speak_worker, daemon=True)
        self.thread.start()

    def say(self, text, important=False):
        if important:
            with self.queue.mutex: self.queue.queue.clear()
        self.queue.put(text)
        
    def stop(self):
        self.running = False
        self.queue.put(None)

    def _speak_worker(self):
        fallback_engine = None
        if not self.use_piper:
            try:
                fallback_engine = pyttsx3.init()
                fallback_engine.setProperty('rate', CONFIG['VOICE_SPEED']) 
            except: pass

        while self.running:
            try:
                text = self.queue.get(timeout=0.1)
                if text is None: break
                
                if self.use_piper:
                    try:
                        speed_factor = max(0.5, 1.0 - ((CONFIG['VOICE_SPEED'] - 150) / 200))
                        cmd = [PIPER_BINARY, '--model', PIPER_MODEL, '--output_file', TEMP_WAV, '--length_scale', str(f"{speed_factor:.2f}")]
                        subprocess.run(cmd, input=text.encode('utf-8'), capture_output=True, cwd=PIPER_FOLDER)
                        if os.path.exists(TEMP_WAV):
                            winsound.PlaySound(TEMP_WAV, winsound.SND_FILENAME)
                            try: os.remove(TEMP_WAV)
                            except: pass
                    except Exception as e:
                        logging.error(f"Piper Error: {e}")
                else:
                    if fallback_engine:
                        fallback_engine.say(text)
                        fallback_engine.runAndWait()
                self.queue.task_done()
            except queue.Empty: continue
            except: pass

# --- 4. RACE SESSION MANAGER ---
class RaceSession:
    def __init__(self, voice):
        self.voice = voice
        self.current_lap = -1
        self.lap_start_time = 0
        self.is_racing = False
        self.turn_history = [] 
        
    def log_turn(self, turn_name, score, issue_summary):
        if self.is_racing:
            self.turn_history.append({'name': turn_name, 'score': score, 'issue': issue_summary})

    def update(self, packet_lap, flags):
        if not self.is_racing and packet_lap >= 1:
            self.is_racing = True
            self.current_lap = packet_lap
            self.lap_start_time = time.time()
            self.turn_history = [] 
            self.voice.say("Green flag.", important=True)
            log_system("🏁 RACE STARTED", "info")
            return

        if self.is_racing and packet_lap > self.current_lap:
            lap_time = time.time() - self.lap_start_time
            if CONFIG['LAP_ANALYSIS'] and self.turn_history:
                worst = min(self.turn_history, key=lambda x: x['score'])
                if worst['score'] < 80:
                    self.voice.say(f"Focus on {worst['name']}. {worst['issue']}.", important=True)
                else:
                    self.voice.say("Good consistency.", important=True)
            else:
                self.voice.say(f"Lap {self.current_lap} complete.", important=True)
            
            self.current_lap = packet_lap
            self.lap_start_time = time.time()
            self.turn_history = [] 

        if (flags & (1 << 13)) and self.is_racing: 
             self.is_racing = False
             self.voice.say("Checkered flag.", important=True)
             log_system("🏁 RACE FINISHED", "info")

# --- 5. COACH BRAIN (V58 - DATA DIET + GATEKEEPER) ---
class CoachBrain:
    def __init__(self, voice, chat_level, display_queue, session_manager):
        self.voice = voice
        self.display_queue = display_queue 
        self.session_manager = session_manager 
        self.lock = threading.Lock()
        self.ai_busy = False 
        
        # USE FLASH 2.5 (Fastest current model)
        self.model = 'models/gemini-2.5-flash-lite'
        self.level = chat_level
        self.word_limit = 12 
        self.persona = "MASTER COACH (Holistic)"

        try:
            self.client = genai.Client(api_key=CONFIG['GEMINI_KEY'])
            self.ai_available = True
            log_system(f"AI CONFIG: {self.model} | {self.persona}", "info")
        except Exception as e:
            self.ai_available = False
            log_system(f"AI: Connection Failed: {e}", "error")

    def process_incident(self, type, msg):
        self.voice.say(msg, important=True)
        self.display_queue.put(f"⚠️ {type}: {msg}")

    def process_turn(self, stats, turn_id):
        # 1. Physics Score
        score = (stats['trail_score'] + stats['stability_score']) / 2
        if stats['dive_score'] < -20: score -= 20
        if stats['slip_ratio_peak'] > 1.2: score -= 15
        if stats['coasting_time'] > 0.3: score -= 10 
        if stats['overlap_percent'] > 10: score -= 10
        if stats['throttle_smoothness'] < 60: score -= 5 
        
        score = max(0, min(100, score))
        
        # 3. Log Issue
        issue = "Check stability"
        if stats['slip_ratio_peak'] > 1.2: issue = "Wheelspin on exit"
        elif stats['coasting_time'] > 0.4: issue = "Hesitation/Coasting"
        elif stats['overlap_percent'] > 15: issue = "Pedal Overlap"
        elif stats['stability_score'] < 60: issue = "Unstable mid-corner"
        elif stats['trail_score'] < 60: issue = "Bad braking release"
        elif abs(stats['balance_ratio'] - 1.0) > 0.2: 
            issue = "Understeer" if stats['balance_ratio'] > 1.1 else "Oversteer"
        
        self.session_manager.log_turn(stats['turn_type'], score, issue)

        if not self.ai_busy:
            threading.Thread(target=self._call_ai, args=(stats, turn_id)).start()

    def _call_ai(self, s, turn_id):
        with self.lock: self.ai_busy = True
        try:
            log_system(f"🧠 AI REQUEST: {turn_id}", "debug")
            
            prompt = f"""
            ROLE: Race Engineer (Omniscient Physics Expert).
            GOAL: Analyze the raw trace data below from a recent cornering maneuver in Gran Turismo 7. 
            Provide concise, actionable feedback to help the driver improve their technique.
            
            [TRACE DATA]
            (Time|Spd|Brk|Thr|Yaw|G-Lat|G-Sum|Conf|Smth|Up|Gr)
            {s['trace_str']}
            
            [COLUMN LEGEND]
            - G-Sum: Total G-Force (Grip Usage).
            - Conf: Instant Braking Confidence (0=Panic, 10=Good).
            - Smth: Instant Throttle Smoothness (0=Sawing, 10=Good).
            - Up: Suspension Velocity (Upset).
            - Gr: Gear.
            
            [SCENARIO CONTEXT]
            - Type: {s['turn_type']} (Min Spd: {int(s['min_speed'])} kmh)
            - Lat G-Peak: {s['peak_g']:.2f}G
            
            [FORMAT]
            Max 12 words. Imperative mood. Natural speech.
            """
            
            response = self.client.models.generate_content(
                model=self.model, contents=prompt, config=genai.types.GenerateContentConfig(temperature=0.4)
            )
            
            text = response.text.replace('*', '').strip()
            if ":" in text: text = text.split(":")[-1].strip()
            
            log_ai_interaction(turn_id, prompt, text)
            self.display_queue.put(text) 
            self.voice.say(text)
        except Exception as e:
            logging.error(f"AI Error: {e}")
        finally:
            self.ai_busy = False 

    def trigger_spin_roast(self, turn_id):
        if not self.ai_busy:
            threading.Thread(target=self._call_ai_roast, args=(turn_id,)).start()

    def _call_ai_roast(self, turn_id):
        with self.lock: self.ai_busy = True
        try:
            prompt = "ACT AS: Sarcastic F1 Engineer. Driver spun. Max 6 words."
            response = self.client.models.generate_content(model=self.model, contents=prompt)
            text = response.text.strip()
            self.voice.say(text, important=True)
            self.display_queue.put(f"ROAST: {text}")
        except: pass
        finally: self.ai_busy = False

# --- 6. TRACK STATE (V56 - STABLE TRIGGER) ---
class TrackState:
    def __init__(self, brain):
        self.brain = brain
        self.state = "STRAIGHT" 
        self.turn_start = 0
        self.last_spin_time = 0 
        self.last_crash_time = 0
        self.data_buffer = []
        self.power_exit_timer = 0
        self.fast_exit_timer = 0 
        self.total_yaw_accum = 0.0 
        self.last_timestamp = 0.0
        
        fname = f"gt7_session_{SESSION_ID}.csv"
        self.f = open(os.path.join(os.getcwd(), fname), 'w', newline='', encoding='utf-8')
        self.writer = csv.writer(self.f)
        # FULL TELEMETRY HEADER
        self.writer.writerow(["Time", "Speed", "Thr", "Brk", "G_Lat", "G_Lon", "G_Sum", "Boost", "Yaw", "RPM", "FL", "FR", "RL", "RR", "sFL", "sFR", "sRL", "sRR", "Pitch", "Roll", "TCS", "ASM", "ABS", "AI"])
        log_system(f"📂 CSV LOG: {fname}", "info")

    def process(self, speed, rpm, thr, brk, yaw, gear, tire_temps, susp, flags, ws_val, g_force, pos, vel, ang_vel, assist_flags, rev_data, boost):
        now = time.time()
        
        yaw_rate = abs(yaw) 
        
        # Physics
        ws_fl, ws_fr, ws_rl, ws_rr = ws_val
        avg_f = (ws_fl + ws_fr) / 2
        avg_r = (ws_rl + ws_rr) / 2
        
        slip_ratio = 1.0
        if avg_f > 10.0: slip_ratio = avg_r / avg_f
        
        susp_f = (susp[0] + susp[1]) / 2
        susp_r = (susp[2] + susp[3]) / 2
        dive = susp_r - susp_f 
        
        temp_avg = sum(tire_temps) / 4

        # Safety
        if g_force > 8.0 and (now - self.last_crash_time > 10.0):
            self.last_crash_time = now
            self.brain.process_incident("CRASH", "Heavy impact.")
            self.state = "STRAIGHT"; self.data_buffer = []; return "CRASH", 0, 0

        if (yaw_rate > 2.5 and speed > 20) and (now - self.last_spin_time > 10.0):
            self.last_spin_time = now
            self.brain.trigger_spin_roast(datetime.datetime.now().strftime("%H%M%S"))
            self.state = "STRAIGHT"; self.data_buffer = []; return "SPIN", 0, 0

        # State Machine
        is_braking = (brk > 50 and speed > 30)
        is_turning = (g_force > 0.8)
        
        if self.state == "STRAIGHT":
            if is_braking or is_turning:
                self.state = "LATCHED"
                self.turn_start = now
                self.data_buffer = []
                self.total_yaw_accum = 0.0
                self.last_timestamp = now
                log_system(f"Entry: {int(speed)}kmh", "debug")
        
        if self.state == "LATCHED":
            dt = now - self.last_timestamp
            self.last_timestamp = now
            self.total_yaw_accum += math.degrees(yaw_rate * dt)

            self.data_buffer.append({
                't': now, 'spd': speed, 'thr': thr, 'brk': brk, 
                'yaw': yaw, 'rpm': rpm, 'g': g_force, 
                'slip': slip_ratio, 'dive': dive, 'temp': temp_avg,
                'vel': vel, 'ang_vel': ang_vel, 'assist': assist_flags,
                'rev_limit': rev_data, 'gear': gear,
                'ws': ws_val, 'susp': susp, 'boost': boost
            })
            
            if thr > 245 and abs(yaw) < 0.15:
                 if self.fast_exit_timer == 0: self.fast_exit_timer = now
                 if (now - self.fast_exit_timer) > CONFIG['TRIGGER_SENSITIVITY']: 
                     self._finish(now); return "EXIT", dive, slip_ratio
            else: self.fast_exit_timer = 0 

            if thr > 230 and abs(yaw) < 0.1: 
                if self.power_exit_timer == 0: self.power_exit_timer = now
                if (now - self.power_exit_timer) > CONFIG['TRIGGER_SENSITIVITY']: 
                    self._finish(now); return "EXIT", dive, slip_ratio
            else: self.power_exit_timer = 0 

            if (now - self.turn_start) > 15.0:
                 self.state = "STRAIGHT"; self.data_buffer = []

        return self.state, dive, slip_ratio

    def _finish(self, now):
        # --- THE GATEKEEPER LOGIC (NEW) ---
        if not self._validate_corner(self.data_buffer):
            self.state = "STRAIGHT"; self.data_buffer = []; return

        stats = self._analyze(self.data_buffer)
        if stats['duration'] > 1.5:
            self.brain.process_turn(stats, datetime.datetime.now().strftime("%H%M%S"))
        
        self.state = "STRAIGHT"; self.data_buffer = []

    def _validate_corner(self, data):
        if len(data) < 10: return False # Too short
        
        max_g_sum = 0
        max_brake = 0
        max_lat_g = 0
        min_thr = 255
        max_spd = 0
        min_spd = 999
        
        prev_t = 0
        prev_spd = 0
        
        for d in data:
            if d['brk'] > max_brake: max_brake = d['brk']
            if d['g'] > max_lat_g: max_lat_g = d['g']
            if d['thr'] < min_thr: min_thr = d['thr']
            if d['spd'] > max_spd: max_spd = d['spd']
            if d['spd'] < min_spd: min_spd = d['spd']
            
            # G-Sum calc with zero div protection
            dt = d['t'] - prev_t
            if dt <= 0.001: dt = 0.05
            
            dSpd = (d['spd'] - prev_spd) / 3.6
            g_lon = abs(dSpd / dt) / 9.81
            g_sum = math.sqrt(d['g']**2 + g_lon**2)
            if g_sum > max_g_sum and g_sum < 5.0: max_g_sum = g_sum
            
            prev_t = d['t']; prev_spd = d['spd']

        speed_drop = max_spd - min_spd

        # 1. Commitment Gate (Did you push?)
        if max_g_sum < 1.1: return False
        
        # 3. Noise Filter (Flat out or Parking)
        if min_thr > 230 and max_lat_g < 1.5: return False
        if max_spd < 40 and max_lat_g < 1.0: return False

        # 2. Valid Signatures
        if max_brake > 100 and speed_drop > 20: return True
        if min_thr > 127 and max_lat_g > 1.4 and self.total_yaw_accum > 45: return True
        if max_lat_g > 1.2 and self.total_yaw_accum > 40: return True
        
        return False

    def _analyze(self, data):
        start = data[0]['t']
        step = 4  # UPDATED: 0.066s Sampling (Requested ~0.07s)
        
        trace = ""
        slips, dives, temps, brks, thrs, yaws, gs = [], [], [], [], [], [], []
        rpms, speeds, gears = [], [], []
        
        prev_brk = 0
        prev_thr = 0
        prev_yaw = 0
        prev_spd = 0
        prev_t = 0
        
        for i in range(0, len(data), step):
            d = data[i]
            
            # --- INSTANT CALCULATIONS ---
            # G-Longitudinal calc with zero div protection
            dt_frame = d['t'] - prev_t
            if dt_frame <= 0.001: dt_frame = 0.066
            
            dSpd = (d['spd'] - prev_spd) / 3.6 # m/s
            G_Lon = (dSpd / dt_frame) / 9.81
            G_Sum = math.sqrt(d['g']**2 + G_Lon**2)
            if G_Sum > 5.0: G_Sum = 0 # Filter spikes
            
            # Confidence/Smoothness
            dBrk = abs(d['brk'] - prev_brk)
            Conf = max(0, 10 - int(dBrk / 10)) 
            dThr = abs(d['thr'] - prev_thr)
            Smth = max(0, 10 - int(dThr / 10))
            
            # Suspension Upset
            SusVal = abs(d['ang_vel'][0]) + abs(d['ang_vel'][2])
            Upset = min(9, int(SusVal * 10)) 
            
            prev_brk = d['brk']; prev_thr = d['thr']; prev_yaw = d['yaw']
            prev_spd = d['spd']; prev_t = d['t']
            
            # --- EXPANDED TRACE STRING (REMOVED BULKY COLS) ---
            trace += (f"{d['t']-start:.1f}s|{int(d['spd'])}|B:{d['brk']}|T:{d['thr']}|Y:{d['yaw']:.2f}|"
                      f"G:{d['g']:.1f}|GS:{G_Sum:.1f}|"
                      f"C:{Conf}|Sm:{Smth}|Up:{Upset}|Gr:{d['gear']}\n")

            slips.append(d['slip'])
            dives.append(d['dive'])
            temps.append(d['temp'])
            brks.append(d['brk'])
            thrs.append(d['thr'])
            yaws.append(d['yaw'])
            gs.append(d['g'])
            rpms.append(d['rpm'])
            speeds.append(d['spd'])
            gears.append(d['gear'])

        # --- V56 CALCULATIONS (For Logic Fallbacks) ---
        
        coast_frames = sum(1 for d in data if d['brk'] < 5 and d['thr'] < 5)
        dt_avg = (data[-1]['t'] - data[0]['t']) / len(data)
        coasting_time = coast_frames * dt_avg

        overlap_frames = sum(1 for d in data if d['brk'] > 10 and d['thr'] > 10)
        overlap_percent = int((overlap_frames / len(data)) * 100)

        rpm_delta = rpms[-1] - rpms[0]
        speed_delta = speeds[-1] - speeds[0]
        drive_efficiency = (rpm_delta / 1000) / (speed_delta / 10) if speed_delta > 5 else 1.0

        min_speed = min(speeds)
        
        has_sign_change = False
        start_sign = 1 if yaws[0] > 0 else -1
        for y in yaws:
            if abs(y) > 0.1: 
                current_sign = 1 if y > 0 else -1
                if current_sign != start_sign:
                    has_sign_change = True; break
        
        turn_type = "Corner"
        if has_sign_change: turn_type = "Chicane"
        elif min_speed < 65 and self.total_yaw_accum > 100: turn_type = "Hairpin"
        elif min_speed > 160 and self.total_yaw_accum < 60: turn_type = "Sweeper"
        elif min_speed > 100: turn_type = "Mid Corner"
        elif self.total_yaw_accum > 80: turn_type = "Sharp Corner"

        trail_score = 0
        brake_confidence = 100
        try:
            max_brake = max(brks)
            if max_brake > 80:
                idx = brks.index(max_brake)
                release = brks[idx:]
                drops = sum(1 for i in range(len(release)-1) if release[i] >= release[i+1])
                trail_score = int((drops / len(release)) * 100)
                brake_variance = statistics.variance(brks[:idx]) if idx > 2 else 0
                brake_confidence = max(0, 100 - int(brake_variance * 0.1))
        except: pass
        
        throttle_smoothness = 100
        try:
            accel_diffs = []
            for i in range(1, len(thrs)):
                is_shifting = False
                if i > 0 and gears[i] != gears[i-1]: is_shifting = True
                if i > 1 and gears[i] != gears[i-2]: is_shifting = True 
                if thrs[i] > 5 and not is_shifting:
                    diff = abs(thrs[i] - thrs[i-1])
                    accel_diffs.append(diff)
            if len(accel_diffs) > 5:
                avg_jerk = sum(accel_diffs) / len(accel_diffs)
                throttle_smoothness = max(0, 100 - int(avg_jerk * 2))
        except: pass
        
        avg_slip = sum(slips) / len(slips)
        balance_ratio = 1.0 / avg_slip if avg_slip > 0 else 1.0

        exit_yaws = yaws[len(yaws)//2:]
        yaw_changes = sum(1 for i in range(len(exit_yaws)-1) if abs(exit_yaws[i]-exit_yaws[i+1]) > 0.2)
        
        return {
            'duration': data[-1]['t'] - start,
            'speed_loss': data[0]['spd'] - min(speeds),
            'min_speed': min_speed,
            'peak_g': max(gs) if gs else 0,
            'turn_type': turn_type, 
            'coasting_time': coasting_time,
            'overlap_percent': overlap_percent, 
            'drive_efficiency': drive_efficiency, 
            'trail_score': trail_score, 
            'brake_confidence': brake_confidence,
            'throttle_smoothness': throttle_smoothness,
            'stability_score': max(0, 100 - (yaw_changes * 10)),
            'dive_score': min(dives) if dives else 0,
            'slip_ratio_peak': max(slips),
            'dive_avg': sum(dives)/len(dives),
            'temp_avg': sum(temps)/len(temps),
            'balance_ratio': balance_ratio,
            'total_angle': self.total_yaw_accum,
            'trace_str': trace
        }

    def log(self, elapsed, speed, rpm, gear, thr, brk, yaw, g, slip, dive, temp, status, ai_msg, extra_telemetry):
        # Unpack extra telemetry for logging
        pitch, roll, tcs, asm, abs_val, boost, G_Lon, G_Sum, ws_val, susp = extra_telemetry
        self.f.flush() 
        self.writer.writerow([f"{elapsed:.3f}", f"{speed:.1f}", thr, brk, f"{g:.2f}", f"{G_Lon:.2f}", f"{G_Sum:.2f}", f"{boost:.2f}",
                              f"{yaw:.3f}", int(rpm), 
                              int(ws_val[0]), int(ws_val[1]), int(ws_val[2]), int(ws_val[3]), 
                              f"{susp[0]:.2f}", f"{susp[1]:.2f}", f"{susp[2]:.2f}", f"{susp[3]:.2f}",
                              f"{pitch:.2f}", f"{roll:.2f}", tcs, asm, abs_val, ai_msg])

# --- MAIN ---
def main():
    log_system("--- GT7 AI COACH: V58 (PURE RAW + OPTIMIZED) ---")
    
    voice = VoiceSystem()
    q = queue.Queue()
    session = RaceSession(voice) 
    brain = CoachBrain(voice, CONFIG['COACH_LEVEL'], q, session)
    tracker = TrackState(brain)
    
    ps5_ip = CONFIG['PS5_IP']
    if ps5_ip is None:
        ps5_ip = NetworkScanner.find_ps5(voice)
        if not ps5_ip:
            voice.say("Console not found. Enter IP.")
            ps5_ip = input("Enter PS5 IP: ")
    
    bind_ip = CONFIG['LOCAL_IP'] or NetworkScanner.get_local_ip()
    voice.say(f"Connected to {ps5_ip}")
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    try: sock.bind((bind_ip, CONFIG['PORT_RX']))
    except: sock.bind(('0.0.0.0', CONFIG['PORT_RX']))

    sock.settimeout(1.0)
    sock.sendto(b'A', (ps5_ip, CONFIG['PORT_TX']))
    log_system("Waiting for Data...", "info")

    start_t = time.time()
    pkt_cnt = 0
    prev_spd = 0
    prev_t = 0
    
    try:
        while True:
            if pkt_cnt % 100 == 0: sock.sendto(b'A', (ps5_ip, CONFIG['PORT_TX']))
            try:
                data, _ = sock.recvfrom(4096)
                if len(data) < 296: continue
                pkt_cnt += 1

                nonce = struct.pack('<2I', struct.unpack('<I', data[0x40:0x44])[0] ^ MAGIC_NONCE_OFFSET, struct.unpack('<I', data[0x40:0x44])[0])
                decrypted = Salsa20.new(key=KEY, nonce=nonce).decrypt(data)
                
                # Decode offsets
                ws_fl = abs(struct.unpack('<f', decrypted[0x94:0x98])[0]) * 3.6
                ws_fr = abs(struct.unpack('<f', decrypted[0x98:0x9C])[0]) * 3.6
                ws_rl = abs(struct.unpack('<f', decrypted[0x9C:0xA0])[0]) * 3.6
                ws_rr = abs(struct.unpack('<f', decrypted[0xA0:0xA4])[0]) * 3.6
                ws_val = [ws_fl, ws_fr, ws_rl, ws_rr]
                avg_f = (ws_fl + ws_fr) / 2
                avg_r = (ws_rl + ws_rr) / 2

                s_fl = struct.unpack('<f', decrypted[0xB4:0xB8])[0]
                s_fr = struct.unpack('<f', decrypted[0xB8:0xBC])[0]
                s_rl = struct.unpack('<f', decrypted[0xBC:0xC0])[0]
                s_rr = struct.unpack('<f', decrypted[0xC0:0xC4])[0]
                susp = [s_fl, s_fr, s_rl, s_rr]

                t_fl = struct.unpack('<f', decrypted[0x60:0x64])[0]
                t_fr = struct.unpack('<f', decrypted[0x64:0x68])[0]
                t_rl = struct.unpack('<f', decrypted[0x68:0x6C])[0]
                t_rr = struct.unpack('<f', decrypted[0x6C:0x70])[0]
                tire_temps = [t_fl, t_fr, t_rl, t_rr]

                rpm = struct.unpack('<f', decrypted[0x3C:0x40])[0] 
                speed = struct.unpack('<f', decrypted[0x4C:0x50])[0] * 3.6
                boost = struct.unpack('<f', decrypted[0x48:0x4C])[0]
                
                pos_x = struct.unpack('<f', decrypted[0x04:0x08])[0]
                pos_y = struct.unpack('<f', decrypted[0x08:0x0C])[0]
                pos_z = struct.unpack('<f', decrypted[0x0C:0x10])[0]
                pos = [pos_x, pos_y, pos_z]

                vel_x = struct.unpack('<f', decrypted[0x10:0x14])[0]
                vel_y = struct.unpack('<f', decrypted[0x14:0x18])[0]
                vel_z = struct.unpack('<f', decrypted[0x18:0x1C])[0]
                vel = [vel_x, vel_y, vel_z]

                ang_vel_x = struct.unpack('<f', decrypted[0x2C:0x30])[0]
                ang_vel_y = struct.unpack('<f', decrypted[0x30:0x34])[0]
                ang_vel_z = struct.unpack('<f', decrypted[0x34:0x38])[0]
                ang_vel = [ang_vel_x, ang_vel_y, ang_vel_z]

                rev_min = struct.unpack('<f', decrypted[0x88:0x8C])[0]
                rev_max = struct.unpack('<f', decrypted[0x8C:0x90])[0]
                rev_data = [rev_min, rev_max]

                flags = struct.unpack('<H', decrypted[0x8E:0x90])[0]
                tcs_active = bool(flags & (1 << 7))
                asm_active = bool(flags & (1 << 6))
                abs_active = bool(flags & (1 << 12)) 
                assist_flags = {'tcs': tcs_active, 'asm': asm_active, 'abs': abs_active}

                thr = decrypted[0x91] 
                brk = decrypted[0x92] 
                gear = decrypted[0x90] & 0x0F 
                yaw = struct.unpack('<f', decrypted[0x30:0x34])[0]
                lap = struct.unpack('<h', decrypted[0x74:0x76])[0] 
                
                session.update(lap, flags)
                
                yaw_rate = abs(yaw)
                g_force = (speed/3.6)**2 / ((speed/3.6)/yaw_rate * 9.81) if yaw_rate > 0.05 else 0

                status, dive, slip = tracker.process(speed, rpm, thr, brk, yaw, gear, tire_temps, susp, flags, ws_val, g_force, pos, vel, ang_vel, assist_flags, rev_data, boost)
                
                # Calc G-Lon/Sum for live log
                now = time.time()
                dt_frame = now - prev_t
                if dt_frame <= 0.001: dt_frame = 0.066
                
                dSpd = (speed - prev_spd) / 3.6 
                G_Lon = (dSpd / dt_frame) / 9.81 
                G_Sum = math.sqrt(g_force**2 + G_Lon**2)
                prev_spd = speed; prev_t = now

                # Get AI Message for CSV
                ai_msg_to_log = ""
                try: ai_msg_to_log = q.get_nowait()
                except: pass

                tracker.log(now-start_t, speed, rpm, gear, thr, brk, yaw, g_force, slip, dive, tire_temps[0], status, ai_msg_to_log, 
                            [ang_vel_x, ang_vel_z, int(tcs_active), int(asm_active), int(abs_active), boost, G_Lon, G_Sum, ws_val, susp])
                
                sys.stdout.write(f"\r🏎️ {speed:3.0f} | Slip:{slip:.2f} | Dive:{dive:.2f} | {status} | {ai_msg_to_log:30}")
                sys.stdout.flush()

            except socket.timeout: sock.sendto(b'A', (ps5_ip, CONFIG['PORT_TX']))
    except KeyboardInterrupt: voice.stop()

if __name__ == "__main__":
    main()