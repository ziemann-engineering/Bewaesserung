# SPDX-FileCopyrightText: 2023 Thomas Ziemann
#
# SPDX-License-Identifier: GPL-3.0-or-later

#import microcontroller
#import binascii
#binascii.hexlify(microcontroller.cpu.uid).decode('ascii').upper()

# general imports
import time
import board
import digitalio
import neopixel
import microcontroller
import os

import ssl
import wifi
import socketpool
import rtc
#import adafruit_ntp

import displayio
from adafruit_display_text import bitmap_label
from adafruit_bitmap_font import bitmap_font
#import adafruit_imageload
from adafruit_display_shapes.roundrect import RoundRect

#import adafruit_miniqr

#from adafruit_max1704x import MAX17048 # battery monitor

import adafruit_requests
import json
import adafruit_minimqtt.adafruit_minimqtt as MQTT
from adafruit_io.adafruit_io import IO_MQTT
from adafruit_httpserver import Server, Request, Response, POST
import mdns
from version import VERSION

from cptoml import put
from storage import remount

# try to mount filesystem for settings, if it fails we are connected to USB
try:
    remount("/", False)
except Exception as _:
    print("Could not mount storage, not saving settings.")

# Wait for WiFi (auto-connected via CIRCUITPY_WIFI_SSID in settings.toml)
if not wifi.radio.connected:
    print("WiFi not connected, attempting explicit connect...")
    try:
        wifi.radio.connect(os.getenv("CIRCUITPY_WIFI_SSID"), os.getenv("CIRCUITPY_WIFI_PASSWORD"))
    except Exception as e:
        print(f"WiFi connect failed: {e}")

pool = socketpool.SocketPool(wifi.radio)
requests = adafruit_requests.Session(pool, ssl.create_default_context())

# OTA update check – runs once at boot before the main application starts
from update import check_and_update
check_and_update(requests)

### defines ###

# mode type, can be time (set next watering time), duration (set next watering duration), valve (open/close)
current_mode = "valve"
changing_mode = True  # changing mode or changing value

# defaults
start_hour = os.getenv("START_HOUR")
start_minute = os.getenv("START_MINUTE")
duration = os.getenv("DURATION")  # in seconds
min_daily_minutes = os.getenv("MIN_DAILY_MINUTES")  # minimum daily watering time in minutes
max_session_minutes = os.getenv("MAX_DAILY_MINUTES")  # max minutes per single watering session
second_watering_offset = os.getenv("SECOND_WATERING_OFFSET")  # hours after session 1 to run session 2
last_watering_date = os.getenv("LAST_WATERING_DATE")  # last watering date in YYYY-MM-DD
auto_mode = os.getenv("AUTO_MODE") != 0  # if True, duration is set daily from ET0
mqtt_enabled = os.getenv("MQTT_ENABLED") == 1
forecast_lat = os.getenv("FORECAST_LAT")
forecast_lon = os.getenv("FORECAST_LON")
min_per_mm_et0 = os.getenv("MIN_PER_MM_ET0")  # watering minutes per mm of daily ET0
last_auto_fetch_date = "1970-01-01"  # tracks when ET0 was last fetched (in-memory only)
last_et0 = None  # last fetched ET0 value in mm
valve = "zu"
valve_open_monotonic = time.monotonic() + 3610  # ~1 h safety timer
active_session_duration = 0  # duration of the currently active watering session (seconds)

# Statistics – persisted to stats.json across reboots
boot_monotonic = time.monotonic()
daily_watering = {}  # date_str -> total seconds watered that day (up to 365 days)
try:
    with open("stats.json", "r") as _f:
        daily_watering = json.loads(_f.read())
    print(f"Loaded stats: {len(daily_watering)} days")
except Exception:
    pass  # file missing or corrupt – start fresh
valve_session_start = None  # monotonic time when valve was opened

def save_stats():
    """Persist daily_watering to stats.json, keeping at most 365 days."""
    while len(daily_watering) > 365:
        del daily_watering[sorted(daily_watering.keys())[0]]
    try:
        with open("stats.json", "w") as f:
            f.write(json.dumps(daily_watering))
    except Exception as e:
        print(f"Could not save stats: {e}")

def write_toml(key, value):
    try:
        put(key, value)
    except Exception as e:
        print(f"Could not write settings.toml: {e}")

### display ###

# Display constants
DISPLAY_BRIGHTNESS = 0.2

# Button appearance constants
BUTTON_WIDTH = 55
BUTTON_HEIGHT = 40
BUTTON_RADIUS = 15

# Button positions (normal state)
BUTTON_X_NORMAL = -10
BUTTON_Y_UP_NORMAL = -10
BUTTON_Y_MIDDLE_NORMAL = 50
BUTTON_Y_DOWN_NORMAL = 106

# Button positions (pressed state)
BUTTON_X_CHANGE = 2
BUTTON_Y_CHANGE = 2

# Button colors
BUTTON_COLOR_UP_DOWN = 0x00FF00  # Green
BUTTON_COLOR_MIDDLE = 0x006699   # Blue
BUTTON_TEXT_COLOR = 0x000000     # Black

# Label positions
LABEL_X_BUTTON = 10
LABEL_Y_UP = 10
LABEL_Y_MIDDLE = 69
LABEL_Y_DOWN = 120
LABEL_X_STATUS = 65
LABEL_Y_VALVE = 18
LABEL_Y_START = 69            # y for the static "Start:" label
LABEL_Y_TIME = 69             # time digits inline with "Start:" (single session)
LABEL_Y_TIME_SPLIT = 56       # time digits above "Start:" (split session)
LABEL_X_TIME2 = 163           # x for time digit labels (both sessions)
LABEL_Y_TIME2 = 82            # second session time, below "Start:"
LABEL_Y_DURATION = 120

# Colors
COLOR_SELECTED = 0x555555
COLOR_CHANGING = 0x000000
COLOR_TEXT_NORMAL = 0xFFFFFF
COLOR_TEXT_HIGHLIGHT = 0x000000

# Make the display context
display = board.DISPLAY
display.brightness = DISPLAY_BRIGHTNESS

font_medium = bitmap_font.load_font("/fonts/LiberationSans-Bold-24.pcf")
font_small = bitmap_font.load_font("/fonts/LiberationSans-Bold-10.pcf")

default = displayio.Group()

up = RoundRect(BUTTON_X_NORMAL, BUTTON_Y_UP_NORMAL, BUTTON_WIDTH, BUTTON_HEIGHT, BUTTON_RADIUS, fill=BUTTON_COLOR_UP_DOWN)
middle = RoundRect(BUTTON_X_NORMAL, BUTTON_Y_MIDDLE_NORMAL, BUTTON_WIDTH, BUTTON_HEIGHT, BUTTON_RADIUS, fill=BUTTON_COLOR_MIDDLE)
down = RoundRect(BUTTON_X_NORMAL, BUTTON_Y_DOWN_NORMAL, BUTTON_WIDTH, BUTTON_HEIGHT, BUTTON_RADIUS, fill=BUTTON_COLOR_UP_DOWN)

default.append(up)
default.append(middle)
default.append(down)

button_label_up = bitmap_label.Label(font_medium, text="+", color=BUTTON_TEXT_COLOR, x=LABEL_X_BUTTON, y=LABEL_Y_UP)
button_label_middle = bitmap_label.Label(font_medium, text="OK", color=COLOR_TEXT_NORMAL, x=3, y=LABEL_Y_MIDDLE)
button_label_down = bitmap_label.Label(font_medium, text="-", color=BUTTON_TEXT_COLOR, x=LABEL_X_BUTTON, y=LABEL_Y_DOWN)
default.append(button_label_up)
default.append(button_label_middle)
default.append(button_label_down)

label_valve = bitmap_label.Label(font_medium, text=f"Ventil: {valve}", color=COLOR_TEXT_NORMAL, x=LABEL_X_STATUS, y=LABEL_Y_VALVE)
label_start = bitmap_label.Label(font_medium, text="Start:", color=COLOR_TEXT_NORMAL, x=LABEL_X_STATUS, y=LABEL_Y_START)
label_time = bitmap_label.Label(font_medium, text=f"{start_hour:02}:{start_minute:02}", color=COLOR_TEXT_NORMAL, x=LABEL_X_TIME2, y=LABEL_Y_TIME)
label_time2 = bitmap_label.Label(font_medium, text="", color=COLOR_TEXT_NORMAL, x=LABEL_X_TIME2, y=LABEL_Y_TIME2)
label_duration = bitmap_label.Label(font_medium, text=f"{'Auto' if auto_mode else 'Man'}: {duration // 60:02d} min", color=COLOR_TEXT_NORMAL, x=LABEL_X_STATUS, y=LABEL_Y_DURATION)

#label_weather = bitmap_label.Label(font_medium, text="Wetter: trocken", color=0xFFFFFF, x=65, y=110)
default.append(label_start)
default.append(label_time)
default.append(label_time2)
default.append(label_duration)
default.append(label_valve)
#default.append(label_weather)

display.root_group = default
label_valve.background_color = COLOR_SELECTED

def update_start():
    second_hour = (start_hour + second_watering_offset) % 24
    if duration > max_session_minutes * 60:
        label_time.y = LABEL_Y_TIME_SPLIT
        label_time2.text = f"{second_hour:02}:{start_minute:02}"
    else:
        label_time.y = LABEL_Y_TIME
        label_time2.text = ""
    label_time.text = f"{start_hour:02}:{start_minute:02}"
def update_duration():
    prefix = "Auto" if auto_mode else "Manual"
    label_duration.text = f"{prefix}: {duration // 60:02d} min"
def update_status():
    label_valve.text = f"Ventil: {valve}"

def fetch_auto_duration():
    """Fetch today's ET0 from Open-Meteo (free, no key) and set duration.
    duration = et0_mm * min_per_mm_et0 * 60 seconds."""
    global duration, last_auto_fetch_date, last_et0
    current_time = time.localtime()
    today_str = f"{current_time.tm_year:04d}-{current_time.tm_mon:02d}-{current_time.tm_mday:02d}"
    if last_auto_fetch_date == today_str:
        return  # already fetched today
    try:
        url = ("https://api.open-meteo.com/v1/forecast"
               f"?latitude={forecast_lat}&longitude={forecast_lon}"
               "&daily=et0_fao_evapotranspiration&forecast_days=1&timezone=Europe%2FZurich")
        resp = requests.get(url)
        data = resp.json()
        et0 = float(data["daily"]["et0_fao_evapotranspiration"][0])  # mm for today
        last_et0 = et0
        duration = max(0, int(et0 * min_per_mm_et0 * 60))
        last_auto_fetch_date = today_str
        write_toml("DURATION", duration)
        update_duration()
        update_start()
        print(f"Auto mode: ET0={et0:.2f} mm -> duration={duration // 60} min")
    except Exception as e:
        print(f"Auto mode fetch failed: {e}")

### internet things ###

aio_username = os.getenv("ADAFRUIT_IO_USERNAME")
aio_key = os.getenv("ADAFRUIT_IO_KEY")

location = "Europe/Zurich"
TIME_URL = "https://io.adafruit.com/api/v2/%s/integrations/time/struct?x-aio-key=%s&tz=%s" % (aio_username, aio_key, location)

try:
    response = requests.get(TIME_URL)
    json_time = response.json()
    rtc.RTC().datetime = (json_time['year'], json_time['mon'], json_time['mday'], json_time['hour'], json_time['min'], json_time['sec']+1, json_time['wday'], json_time['yday'], json_time['isdst'])
except Exception as e: # could be network error, bad response, etc., now runnig with relative time
    print(f"Could not get time from Adafruit IO: {e}")

# Callback function which will be called when a connection is established
def connected(client):
    client.subscribe("bewasserung-command")

# Callback function which will be called when a message comes from a subscribed feed
def message(client, feed_id, payload):
    global start_hour, start_minute, duration
    if feed_id == "bewasserung-command":
        #print(payload)
        payload = payload.split(" ")
        if len(payload) > 0:
            command = payload[0]
        else:
            command = ""
        if command == "ventil":
            if len(payload) == 2 and payload[1] == "auf":
                valve_open()
            elif len(payload) == 2 and payload[1] == "zu":
               valve_close()
            io.publish("bewasserung-status", f"ventil ist {valve}") # print status in any case
        if command == "termin" and len(payload) >= 3: # syntax: termin 17:15 30 min or termin 2024-01-30T17:15:00+01:00 30 min
            if len(payload[1]) > 5: # ISO datetime string
                start_hour = int(payload[1][11:13])
                start_minute = int(payload[1][14:16])
            else:
                start = payload[1].split(":")
                start_hour = int(start[0])
                start_minute = int(start[1])
            duration = int(float(payload[2]))
            update_start()
            update_duration()
        elif command == "reset" and len(payload) == 1:
            microcontroller.reset()

# Initialize MQTT if enabled
if mqtt_enabled:
    mqtt_client = MQTT.MQTT(
        broker="io.adafruit.com",
        port=1883,
        is_ssl=False,
        username=aio_username,
        password=aio_key,
        socket_pool=pool,
        ssl_context=ssl.create_default_context(),
        socket_timeout=0.05,  # prevent io.loop() from blocking the button poll loop
    )
    io = IO_MQTT(mqtt_client)
    io.on_connect = connected
    io.on_message = message
    try:
        io.connect()
    except Exception as e:
        print(f"Could not connect to Adafruit IO MQTT: {e}")
else:
    io = None

# Server things
server = Server(pool, "/static", debug=False)

font_family = "monospace"

def _bar_graph_html():
    """Build an HTML bar chart of daily watering minutes for the last 7 days."""
    t = time.localtime()
    y, m, d = t.tm_year, t.tm_mon, t.tm_mday
    dim = [31,
           29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
           31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    dates = []
    dy, dm, dd = y, m, d
    for _ in range(7):
        dates.insert(0, f"{dy:04d}-{dm:02d}-{dd:02d}")
        dd -= 1
        if dd == 0:
            dm -= 1
            if dm == 0:
                dm = 12
                dy -= 1
            dd = dim[dm - 1]
    today_str = dates[-1]
    values = [daily_watering.get(dt, 0) / 60.0 for dt in dates]
    max_v = max(values) if any(v > 0 for v in values) else 1.0
    bars = ""
    for dt, v in zip(dates, values):
        bar_h = int(v / max_v * 80) if v > 0 else 0
        label_day = dt[8:]
        color = "#1a4fa0" if dt == today_str else "#4488ff"
        val_label = f"{v:.1f}" if v > 0 else ""
        bars += (
            f'<div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;">'
            f'<span style="font-size:10px;">{val_label}</span>'
            f'<div style="width:70%;background:{color};height:{bar_h}px;"></div>'
            f'<span style="font-size:11px;margin-top:2px;">{label_day}</span>'
            f'</div>'
        )
    return (
        '<b>Bewässerung letzte 7 Tage, in Minuten</b>'
        '<div style="display:flex;align-items:flex-end;gap:3px;height:100px;margin:10px auto;'
        'width:90%;border-bottom:2px solid #aaa;border-left:2px solid #aaa;padding-bottom:2px;">'
        + bars +
        '</div>'
    )

#  the HTML script
#  setup as an f string
#  this way, can insert string variables from code.py directly
#  of note, use {{ and }} if something from html *actually* needs to be in brackets
#  i.e. CSS style formatting
def webpage():
    # Recompute schedule text so it reflects current settings
    max_sec = max_session_minutes * 60
    if max_sec <= 0 or duration <= max_sec:
        session1_dur = duration
        session2_dur = 0
    elif duration <= 2 * max_sec:
        session1_dur = max_sec
        session2_dur = duration - max_sec
    else:
        session1_dur = duration // 2
        session2_dur = duration - session1_dur

    if duration > 0 and duration / 60 >= min_daily_minutes:
        frequency_text = "jeden Tag"
    elif duration > 0:
        frequency_text = f"alle {min_daily_minutes / (duration / 60):.0f} Tage"
    else:
        frequency_text = "nie"

    duration_prefix = "Auto" if auto_mode else "Manual"
    if auto_mode and last_et0 is not None:
        duration_prefix = f"Auto: {duration / 60:.2f} min (ET0: {last_et0:.1f} mm, {min_per_mm_et0} min/mm)"
    auto_btn_class = "btn on" if auto_mode else "btn"
    auto_btn_label = "Auto: AN" if auto_mode else "Auto: AUS"
    valve_dot_color = "#2563eb" if valve == "auf" else "#aaa"  # bright blue dot, dark buttons
    if auto_mode and last_et0 is not None:
        duration_display = duration_prefix  # already the full label
    else:
        duration_display = f"{duration_prefix}: {duration / 60:.2f} min"
    auto_note = " <span style='color:green;font-size:0.8em;'>(Auto)</span>" if auto_mode else ""
    second_hour = (start_hour + second_watering_offset) % 24
    if session2_dur > 0:
        schedule_text = (
            f"Bewässerung {frequency_text}{auto_note}:<br>"
            f"&nbsp;&nbsp;{start_hour:02}:{start_minute:02} Uhr – {session1_dur//60:.0f} min<br>"
            f"&nbsp;&nbsp;{second_hour:02}:{start_minute:02} Uhr – {session2_dur//60:.0f} min<br><br>"
            f"Letzte Bewässerung am {last_watering_date}."
        )
    else:
        schedule_text = (
            f"Bewässerung {frequency_text}{auto_note} um {start_hour:02}:{start_minute:02} Uhr "
            f"für {session1_dur//60:.0f} min.<br>Letzte Bewässerung am {last_watering_date}."
        )

    with open("webpage.html", "r") as f:
        html = f.read()
    html = html.replace("[[SCHEDULE_TEXT]]", schedule_text)
    html = html.replace("[[VALVE_DOT_COLOR]]", valve_dot_color)
    html = html.replace("[[VALVE]]", valve)
    html = html.replace("[[DURATION_DISPLAY]]", duration_display)
    html = html.replace("[[AUTO_BTN_CLASS]]", auto_btn_class)
    html = html.replace("[[AUTO_BTN_LABEL]]", auto_btn_label)
    html = html.replace("[[START_TIME]]", f"{start_hour:02d}:{start_minute:02d}")
    html = html.replace("[[BAR_GRAPH]]", _bar_graph_html())
    html = html.replace("[[MIN_DAILY_MINUTES]]", str(min_daily_minutes))
    html = html.replace("[[MAX_SESSION_MINUTES]]", str(max_session_minutes))
    html = html.replace("[[SECOND_WATERING_OFFSET]]", str(second_watering_offset))
    html = html.replace("[[MAX_X2]]", str(2 * max_session_minutes))
    html = html.replace("[[MIN_PER_MM_ET0]]", str(min_per_mm_et0))
    return html


@server.route("/")
def base(request: Request):
    return Response(request, f"{webpage()}", content_type='text/html')

@server.route("/status")
def status(request: Request):
    valve_state = "open" if valve == "auf" else "closed"
    uptime = int(time.monotonic() - boot_monotonic)
    t = time.localtime()
    today = f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
    watered_today = daily_watering.get(today, 0)
    if valve_session_start is not None:
        watered_today += time.monotonic() - valve_session_start
    watered_today = int(watered_today)
    second_hour = (start_hour + second_watering_offset) % 24
    max_sec = max_session_minutes * 60
    if max_sec <= 0 or duration <= max_sec:
        session1_s, session2_s = duration, 0
    elif duration <= 2 * max_sec:
        session1_s, session2_s = max_sec, duration - max_sec
    else:
        session1_s = duration // 2
        session2_s = duration - session1_s
    auto_s = 1 if auto_mode else 0
    et0_s = f"{last_et0:.2f}" if last_et0 is not None else "null"
    sessions = f'[{{"start":"{start_hour:02d}:{start_minute:02d}","duration_s":{session1_s}}}'
    if session2_s > 0:
        sessions += f',{{"start":"{second_hour:02d}:{start_minute:02d}","duration_s":{session2_s}}}'
    sessions += ']'
    return Response(request,
        f'{{"status":"ok","version":"{VERSION}","valve":"{valve_state}","uptime_s":{uptime},'
        f'"watered_today_s":{watered_today},'
        f'"duration_s":{duration},"auto_mode":{auto_s},"et0_mm":{et0_s},'
        f'"sessions":{sessions}}}',
        content_type='application/json')

def _web_toggle_auto():
    global auto_mode, last_auto_fetch_date
    if auto_mode:
        auto_mode = False
        write_toml("AUTO_MODE", 0)
    else:
        auto_mode = True
        last_auto_fetch_date = "1970-01-01"
        write_toml("AUTO_MODE", 1)
    update_duration()

def _web_change_time(change):
    global start_hour, start_minute
    start_minute += 15 * change
    if start_minute >= 60:
        start_minute = 0
        start_hour += 1
    elif start_minute < 0:
        start_minute = 45
        start_hour -= 1
    if start_hour < 0:
        start_hour = 23
    elif start_hour > 23:
        start_hour = 0
    write_toml("START_HOUR", start_hour)
    write_toml("START_MINUTE", start_minute)
    update_start()

def _process_command(raw_text):
    if "valve=auf" in raw_text or "=auf" in raw_text:
        valve_open()
    elif "valve=zu" in raw_text or "=zu" in raw_text:
        valve_close()
    elif "dur_up" in raw_text:
        _web_change_duration(1)
    elif "dur_down" in raw_text:
        _web_change_duration(-1)
    elif "time_up" in raw_text:
        _web_change_time(1)
    elif "time_down" in raw_text:
        _web_change_time(-1)
    elif "auto_toggle" in raw_text:
        _web_toggle_auto()

def _web_change_duration(change):
    """Mirror the physical button duration logic; saves to toml immediately."""
    global duration, auto_mode, last_auto_fetch_date
    if auto_mode:
        auto_mode = False
        write_toml("AUTO_MODE", 0)
    elif duration == 0 and change < 0:
        auto_mode = True
        last_auto_fetch_date = "1970-01-01"
        write_toml("AUTO_MODE", 1)
    else:
        if duration + change * 60 >= 60:
            duration += change * 60
        else:
            duration += change * 15
        if duration < 0:
            duration = 0
        write_toml("DURATION", duration)
    update_duration()
    update_start()

@server.route("/cmd", POST)
def cmd_handler(request: Request):
    _process_command(request.raw_request.decode("utf8"))
    if auto_mode and last_et0 is not None:
        dur_label = f"Auto: {duration / 60:.2f} min (ET0: {last_et0:.1f} mm, {min_per_mm_et0} min/mm)"
    elif auto_mode:
        dur_label = f"Auto: {duration / 60:.2f} min"
    else:
        dur_label = f"Manual: {duration / 60:.2f} min"
    start_time = f"{start_hour:02d}:{start_minute:02d}"
    auto_label = "Auto: AN" if auto_mode else "Auto: AUS"
    auto_m = 1 if auto_mode else 0
    return Response(request,
        f'{{"valve":"{valve}","duration_label":"{dur_label}","start_time":"{start_time}","auto_label":"{auto_label}","auto_mode":{auto_m}}}',
        content_type='application/json')

@server.route("/api", POST)
def api_handler(request: Request):
    global duration, start_hour, start_minute, auto_mode, last_auto_fetch_date
    try:
        raw = request.raw_request.decode("utf8")
        sep = raw.find("\r\n\r\n")
        if sep >= 0:
            body = raw[sep + 4:].strip()
        else:
            sep = raw.find("\n\n")
            body = raw[sep + 2:].strip() if sep >= 0 else raw.strip()
        data = json.loads(body)
    except Exception as e:
        return Response(request, f'{{"error":"parse error: {e}"}}', content_type='application/json')
    start_changed = False
    if "valve" in data:
        if data["valve"] == "open":
            valve_open()
        elif data["valve"] == "closed":
            valve_close()
    if "duration_s" in data:
        duration = max(0, int(data["duration_s"]))
        write_toml("DURATION", duration)
        update_duration()
        start_changed = True
    if "start_hour" in data:
        start_hour = int(data["start_hour"]) % 24
        write_toml("START_HOUR", start_hour)
        start_changed = True
    if "start_minute" in data:
        start_minute = int(data["start_minute"]) % 60
        write_toml("START_MINUTE", start_minute)
        start_changed = True
    if "auto_mode" in data:
        new_auto = bool(data["auto_mode"])
        if new_auto != auto_mode:
            auto_mode = new_auto
            if auto_mode:
                last_auto_fetch_date = "1970-01-01"
            write_toml("AUTO_MODE", 1 if auto_mode else 0)
            update_duration()
    if start_changed:
        update_start()
    valve_state = "open" if valve == "auf" else "closed"
    auto_v = 1 if auto_mode else 0
    return Response(request,
        f'{{"ok":true,"valve":"{valve_state}","duration_s":{duration},'
        f'"start_hour":{start_hour},"start_minute":{start_minute},"auto_mode":{auto_v}}}',
        content_type='application/json')

#  if a button is pressed on the site
@server.route("/", POST)
def buttonpress(request: Request):
    raw_text = request.raw_request.decode("utf8")
    print(raw_text)
    _process_command(raw_text)
    return Response(request, webpage(), content_type='text/html')

try:
    mdns_server = mdns.Server(wifi.radio)
    mdns_server.hostname = "bewaesserung"
    mdns_server.advertise_service(service_type="_http", protocol="_tcp", port=80)
except Exception as e:
    print(f"Could not start mDNS: {e}")

try:
    server.start(str(wifi.radio.ipv4_address), port=80)
except Exception as e:
    print(f"Could not start HTTP server: {e}")

### IO

enable_24V = digitalio.DigitalInOut(board.D10)
enable_24V.direction = digitalio.Direction.OUTPUT
enable_24V.value = False

BUTTON_D0 = digitalio.DigitalInOut(board.D0)
BUTTON_D0.direction = digitalio.Direction.INPUT
BUTTON_D0.pull = digitalio.Pull.UP

BUTTON_D1 = digitalio.DigitalInOut(board.D1)
BUTTON_D1.direction = digitalio.Direction.INPUT
BUTTON_D1.pull = digitalio.Pull.DOWN

BUTTON_D2 = digitalio.DigitalInOut(board.D2)
BUTTON_D2.direction = digitalio.Direction.INPUT
BUTTON_D2.pull = digitalio.Pull.DOWN

RGB = neopixel.NeoPixel(board.NEOPIXEL, 1)
RGB.brightness = 1

# initialize labels (refresh after network setup in case globals changed)
update_status()
update_start()
update_duration()

### functions ###

# check if watering is scheduled
def check_schedule():
    global start_hour, start_minute, duration, valve_open_monotonic, valve, min_daily_minutes, last_watering_date, active_session_duration
    if auto_mode:
        fetch_auto_duration()
    current_time = time.localtime()
    now_monotonic = time.monotonic()
    today_str = f"{current_time.tm_year:04d}-{current_time.tm_mon:02d}-{current_time.tm_mday:02d}"

    # Split total daily duration across two sessions if it exceeds MAX_SESSION_MINUTES.
    # If total > 2x max, split evenly; otherwise first session gets max, second gets the rest.
    max_sec = max_session_minutes * 60
    if max_sec <= 0 or duration <= max_sec:
        session1_dur = duration
        session2_dur = 0
    elif duration <= 2 * max_sec:
        session1_dur = max_sec
        session2_dur = duration - max_sec
    else:  # total > 2x max: split evenly
        session1_dur = duration // 2
        session2_dur = duration - session1_dur

    # Close valve when the active session's duration has elapsed
    if now_monotonic >= valve_open_monotonic + active_session_duration:
        valve_open_monotonic = now_monotonic + 3610  # ~1 h safety timer
        valve_close()

    # Determine if we should water today based on the configured interval
    should_water_today = False
    if duration > 0:
        daily_duration_minutes = duration / 60.0
        if daily_duration_minutes >= min_daily_minutes:
            should_water_today = True
        else:
            interval_days = max(1, int(min_daily_minutes / daily_duration_minutes))
            try:
                last_parts = last_watering_date.split("-")
                last_year, last_month, last_day = int(last_parts[0]), int(last_parts[1]), int(last_parts[2])
                current_day_of_year = current_time.tm_yday
                last_day_of_year = 0
                if last_year == current_time.tm_year:
                    days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
                    if last_year % 4 == 0 and (last_year % 100 != 0 or last_year % 400 == 0):
                        days_in_month[1] = 29  # leap year
                    last_day_of_year = sum(days_in_month[:last_month-1]) + last_day
                days_since_last_watering = current_day_of_year - last_day_of_year
                should_water_today = days_since_last_watering >= interval_days
            except Exception as e:
                print("Error parsing last watering date, watering today: ", e)
                should_water_today = True

    # Session 2 fires at start_hour + SECOND_WATERING_OFFSET hours (same minute)
    second_hour = (start_hour + second_watering_offset) % 24

    # Session 1: fire at start_hour:start_minute
    if (should_water_today
            and current_time.tm_hour == start_hour
            and current_time.tm_min == start_minute
            and valve == "zu"
            and current_time.tm_sec < 10
            and session1_dur > 0):
        active_session_duration = session1_dur
        valve_open()
        valve_open_monotonic = now_monotonic
        last_watering_date = today_str
        write_toml("LAST_WATERING_DATE", last_watering_date)

    # Session 2: only runs if session 1 already fired today (last_watering_date == today)
    elif (session2_dur > 0
            and last_watering_date == today_str
            and current_time.tm_hour == second_hour
            and current_time.tm_min == start_minute
            and valve == "zu"
            and current_time.tm_sec < 10):
        active_session_duration = session2_dur
        valve_open()
        valve_open_monotonic = now_monotonic

def valve_open():
    global valve, valve_session_start
    if not enable_24V.value:
        enable_24V.value = True
        valve = "auf"
        valve_session_start = time.monotonic()
        update_status()
        RGB[0] = (0,0,255)

def valve_close():
    global valve, valve_session_start
    if enable_24V.value:
        enable_24V.value = False
        valve = "zu"
        if valve_session_start is not None:
            elapsed = time.monotonic() - valve_session_start
            valve_session_start = None
            t = time.localtime()
            today = f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
            daily_watering[today] = daily_watering.get(today, 0) + elapsed
            save_stats()
        update_status()
        RGB[0] = (0,0,0)

def activate_valve_mode():
    global current_mode
    label_duration.background_color = None
    label_time.background_color = None
    label_start.background_color = None
    label_time2.background_color = None
    current_mode = "valve"
    label_valve.background_color = COLOR_SELECTED

def activate_time_mode():
    global current_mode
    label_duration.background_color = None
    label_valve.background_color = None
    current_mode = "time"
    label_time.background_color = COLOR_SELECTED
    label_start.background_color = COLOR_SELECTED
    label_time2.background_color = COLOR_SELECTED

def activate_duration_mode():
    global current_mode
    label_valve.background_color = None
    label_time.background_color = None
    label_start.background_color = None
    label_time2.background_color = None
    current_mode = "duration"
    label_duration.background_color = COLOR_SELECTED

# change mode or value up (-) or down (+)
def change_value_or_mode(change):
    global current_mode, start_hour, start_minute, duration, valve, auto_mode, last_auto_fetch_date
    if current_mode == "valve":
        if changing_mode:
            if change > 0:
                activate_duration_mode()
            else:
                activate_time_mode()
        else:
            if valve == "zu":
                valve_open()
                print("Opening")
            else:
                valve_close()
    elif current_mode == "time":
        if changing_mode:
            if change > 0:
                activate_valve_mode()
            else:
                activate_duration_mode()
        else:
            start_minute += 15 * change
            if start_minute >= 60:
                start_minute = 0
                start_hour += 1
            elif start_minute < 0:
                start_minute = 45
                start_hour -= 1
            if start_hour < 0:
                start_hour = 23
            elif start_hour > 23:
                start_hour = 0
            update_start()
    elif current_mode == "duration":
        if changing_mode:

            if change > 0:
                activate_time_mode()
            else:
                activate_valve_mode()
        else:
            if auto_mode:
                # any press while in auto: switch back to manual
                auto_mode = False
                write_toml("AUTO_MODE", 0)
                update_duration()
            elif duration == 0 and change < 0:  # press - at 0: enable auto mode
                auto_mode = True
                last_auto_fetch_date = "1970-01-01"  # force fetch on next cycle
                write_toml("AUTO_MODE", 1)
                update_duration()
            else:
                if duration + change * 60 >= 60:
                    duration += change * 60
                else:
                    duration += change * 15
                if duration < 0:
                    duration = 0
                update_duration()

### inf loop ###

counter = 0
_mqtt_reconnect_not_before = 0  # monotonic time before which reconnect is suppressed
short_press_counter = 1
long_press_counter = 10
long_press_repeat_interval = 3  # repeat action every N iterations (~60 ms) when held

D0_pressed_counter, D1_pressed_counter, D2_pressed_counter = 0, 0, 0

while True:
    if not BUTTON_D0.value: # + button pressed (inverse logic, pulled up)
        up.x = BUTTON_X_NORMAL + BUTTON_X_CHANGE
        up.y = BUTTON_Y_UP_NORMAL + BUTTON_Y_CHANGE
        D0_pressed_counter += 1
        if D0_pressed_counter == short_press_counter:
            change_value_or_mode(1)
        elif D0_pressed_counter > long_press_counter and (D0_pressed_counter - long_press_counter) % long_press_repeat_interval == 0:
            change_value_or_mode(1)
    else:
        D0_pressed_counter = 0
        up.x = BUTTON_X_NORMAL
        up.y = BUTTON_Y_UP_NORMAL
    if BUTTON_D1.value: # OK button pressed
        middle.x = BUTTON_X_NORMAL + BUTTON_X_CHANGE
        #middle.height = 44
        D1_pressed_counter += 1
        if D1_pressed_counter == short_press_counter:
            label_valve.color = COLOR_TEXT_NORMAL
            label_time.color = COLOR_TEXT_NORMAL
            label_start.color = COLOR_TEXT_NORMAL
            label_time2.color = COLOR_TEXT_NORMAL
            label_duration.color = COLOR_TEXT_NORMAL
            if changing_mode:
                changing_mode = False
                if current_mode == "valve":
                    label_valve.color = COLOR_TEXT_HIGHLIGHT
                elif current_mode == "time":
                    label_time.color = COLOR_TEXT_HIGHLIGHT
                    label_start.color = COLOR_TEXT_HIGHLIGHT
                    label_time2.color = COLOR_TEXT_HIGHLIGHT
                elif current_mode == "duration":
                    label_duration.color = COLOR_TEXT_HIGHLIGHT
            else:
                changing_mode = True
                # save settings when pressing OK to leave value changing mode
                write_toml("START_HOUR", start_hour)
                write_toml("START_MINUTE", start_minute)
                write_toml("DURATION", duration)
                write_toml("MIN_DAILY_MINUTES", min_daily_minutes)
    else:
        D1_pressed_counter = 0
        middle.x = BUTTON_X_NORMAL
    if BUTTON_D2.value: # - button pressed
        down.x = BUTTON_X_NORMAL + BUTTON_X_CHANGE
        down.y = BUTTON_Y_DOWN_NORMAL + BUTTON_Y_CHANGE
        D2_pressed_counter += 1
        if D2_pressed_counter == short_press_counter:
            change_value_or_mode(-1)
        elif D2_pressed_counter > long_press_counter and (D2_pressed_counter - long_press_counter) % long_press_repeat_interval == 0:
            change_value_or_mode(-1)
    else:
        D2_pressed_counter = 0
        down.x = BUTTON_X_NORMAL
        down.y = BUTTON_Y_DOWN_NORMAL
        down.y = 106

    time.sleep(0.020)
    counter += 1
    if counter == 50: # every second, check schedule and optionally get updates from Adafruit IO
        counter = 0
        check_schedule()
        if io is not None:
            try:
                io.loop() # get updates from adafruit IO
            except Exception as e:
                print(f"Error getting update from Adafruit IO: {e}")
                if time.monotonic() >= _mqtt_reconnect_not_before:
                    try:
                        try:
                            io.disconnect()
                        except Exception:
                            pass
                        io.connect()
                        _mqtt_reconnect_not_before = 0
                    except Exception as e2:
                        print(f"MQTT reconnect failed: {e2}")
                        _mqtt_reconnect_not_before = time.monotonic() + 30
    try:
        server.poll()
    except Exception as e:
        print(f"Error updating server {e}")
