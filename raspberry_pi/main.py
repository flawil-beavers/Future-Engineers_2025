#!/usr/bin/env python3
import asyncio
import base64
import json
import math
import os
from time import sleep, time, perf_counter
import cv2
import numpy as np
from datetime import datetime
import shutil
import subprocess
import serial
import serial.tools.list_ports
from websockets import WebSocketServerProtocol, serve
from config import ConfigLoader
from helpers import *#extract_ROI, print_past_time, Straight_Section, Lines, bound, setup_logging, Car, SharedState, find_direction, AngleBuffer, LoopTimerRegistry
from pipeline import Pipeline
from picamera2 import Picamera2
from rounddir import find_round_dir
import argparse
import libcamera
import sys
from collections import deque
import traceback

"""
raspberry_pi/main.py
---------------------
Main entrypoint for the Raspberry Pi robot. Configures camera and pipeline,
coordinates Arduino communication (gyro/odometry and motor commands), runs
the vision pipeline (color filters, ROI extraction, line detection) and
provides PD-based control primitives and a high-level state machine used
for rounds and parking maneuvers.

This module is intended to run on a Raspberry Pi with a Picamera and an
Arduino connected for motion and gyro feedback.
"""

#?: Webviewer controls for tuning colors, pd, and selecting stream

# todo: move most of this code to the main function

# turn the camera image by 180 degrees

configloader = ConfigLoader("config.json")
pipeline = Pipeline(configloader)
# Load the config file
with open("config.json", "r") as f:
    config = json.load(f)

picam2 = Picamera2()
# picam2.configure(picam2.create_preview_configuration())
preview_config = picam2.create_preview_configuration(main={"size": (640, 480)})
preview_config["transform"] = libcamera.Transform(vflip=True, hflip=True)
picam2.configure(preview_config)
picam2.start()

picam2.set_controls({
    "AwbEnable": True,
    "ScalerCrop": [
        0,
        1520,
        4065,
        1520
    ],
    # controls.AWB_TEMPERATURE: fixed_temperature
})

roi_center_w, roi_center_h = 100, 40
roi_center_x, roi_center_y = 320 - roi_center_w // 2, 200

roi_width = 100
roi_height = 150

roi_front_width = 10

current_streams = ["viz", "black", "viz"]
has_sent_streams_info = False
active_websocket = None
# Shared dictionary to hold the latest streams from cycle()

car = Car()

# Create a single shared state object
state = SharedState()

# attach an angle buffer to the shared state
state.angle_buffer = AngleBuffer(window_seconds=3.0)

# create a list with four elements of Straight_Section
straight_sections = [Straight_Section(i) for i in range(4)]

ports = serial.tools.list_ports.comports()

loop_timer = LoopTimerRegistry()

# get some frames so camera can adjust
for i in range(30):
    picam2.capture_array()

try:
    ser = serial.Serial(configloader.get_property("ArduinoSerialPort"), configloader.get_property("ArduinoBaudRate"))
except:
    print("Arduino not connected, available devices")
    ser = None
    for port, desc, hwid in sorted(ports):
        print("{}: {} [{}]".format(port, desc, hwid))

def pause_robot(resume=False):
    """Pause or resume the robot.

    Args:
        resume (bool): If True, unpause the robot; otherwise pause it.

    Toggles `car.paused` and prints a short status message. This helper is
    used by serial commands and by the main program to temporarily stop
    motion for safety or calibration.
    """
    global car
    if resume:
        car.paused = False
        print("Robot resumed")
    else:
        car.paused = True
        print("Robot paused")

async def write_serial(msg):
    """Write a message to the Arduino serial port without blocking the loop.

    The actual serial write is performed in a threadpool so the asyncio
    event loop remains responsive.

    Args:
        msg (str): Message to send (including newline if required).
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, ser.write, msg.encode())
    
async def read_serial_line():
    """Read one line from the serial port in a background thread.

    Returns the decoded, stripped line as a string.
    """
    loop = asyncio.get_running_loop()
    line = await loop.run_in_executor(None, ser.readline)
    return line.decode('utf-8').strip()

async def read_and_handle_serial_line():
    """Read a serial line and handle common control messages.

    Recognized protocol messages include:
      - "enable start" / "enable stop": toggle pause
      - Lines beginning with "Stall": mark the car as stalled
      - Lines beginning with "Error": log error

    Returns a tuple (handled: bool, line: str) where handled indicates if
    the caller-side handled the message.
    """
    line = await read_serial_line()
    if line == "enable start":
        pause_robot(True)
        return True, line
    elif line == "enable stop":
        pause_robot()
        return True, line
    elif line.startswith("Stall"):
        print(f"Stall detected: {line}")
        car.stalled = True
        return True, line
    elif line.startswith("Error"):
        print(f"Arduino error: {line}")
        return True, line
    return False, line

async def request_and_parse_float(letter=None, prompt=None):
    """Send a single-letter command to the Arduino and parse its response.

    The Arduino protocol used by this project commonly uses single-letter
    requests. This helper drains pending serial lines, sends the request and
    returns a typed response depending on the command.

    Behavior:
      - 'y' or 'z' -> expect two comma-separated floats (distance, angle)
      - 'x' -> expect a raw string
      - other letters -> expect a single float

    Args:
        letter (str|None): Command letter to send
        prompt (str|None): Optional context used in error messages

    Returns:
        float | (float,float) | str | None on parse error
    """
    while ser.in_waiting > 0:
        result, line = await read_and_handle_serial_line()
        if not result:
            print(f"Unexpected line from Arduino: {line}")

    if letter != None:
        await write_serial(f"{letter}\n")
        if letter[0] == "z" or letter[0] == "y":
            result, line = await read_and_handle_serial_line()
            while result:
                result, line = await read_and_handle_serial_line()
            # split line by comma
            parts = line.split(',')
            if len(parts) != 2:
                print(f"Parse error: {prompt or ''}{line} (expected 2 parts, got {len(parts)})")
                return None
            try:
                return float(parts[0]), float(parts[1])
            except Exception as e:
                print(f"Parse error: {prompt or ''}{line} ({e})")
                return None
        if letter[0] == "x":
            result, line = await read_and_handle_serial_line()
            while result:
                result, line = await read_and_handle_serial_line()
            try:
                return line
            except Exception as e:
                print(f"Parse error: {prompt or ''}{line} ({e})")
                return None
        else:
            result, line = await read_and_handle_serial_line()
            while result:
                result, line = await read_and_handle_serial_line()
            try:
                return float(line)
            except Exception as e:
                print(f"Parse error: {prompt or ''}{line} ({e})")
                return None

async def connect_to_arduino():
    """Perform the Arduino connection handshake and initial drift monitoring.

    This coroutine waits for the Arduino to indicate gyro readiness ("Gyro OK"),
    toggles DTR to reset the device if needed, sends an open command and then
    reads gyro temperature and stabilization information. When the robot is
    paused the function continuously samples gyro readings to compute a
    recent drift rate used to correct angles.
    """
    try:
        if ser is None:
            print("No Arduino connected, skipping communication.")
            return False
        print("Connecting to Arduino (async)")
        while True:
            ser.timeout = 2
            error, msg = await read_and_handle_serial_line()
            if msg == "Gyro OK":
                break
            elif msg == "Gyro error":
                print("Gyro error, closing serial and restarting connection")
            else:
                print(f"Received unexpected message: {msg}, closing serial and restarting connection")
            ser.setDTR(False)
            await asyncio.sleep(1)
            ser.setDTR(True)
        ser.timeout = None
        # print("Gyro initialized successfully")
        await write_serial("o\n")
        await asyncio.sleep(1) # seems to be necessary and lowest possible is 1 s
        gyro_temp = await request_and_parse_float("t", "getting Gyro temperature")
        print(f"Gyro temperature: {gyro_temp}")
        if state.skip_arduino:
            # Directly start Arduino without waiting for button press
            car.paused = False
            print("Arduino started directly due to skip_arduino flag.")
        else:
            gyro_beg = await request_and_parse_float("g")
            time_gyro_beg = perf_counter()
            last_print = time_gyro_beg

            # Keep a deque of (timestamp, gyro_value) for the last second
            gyro_history = deque()
            print_interval = 5
            while car.paused and not state.skip_arduino:
                gyro = await request_and_parse_float("g")
                now = perf_counter()
                
                # Add current reading to history
                gyro_history.append((now, gyro))
                
                # Remove readings older than print_interval second
                while gyro_history and now - gyro_history[0][0] > print_interval:
                    gyro_history.popleft()
                
                if now - last_print > print_interval:
                    last_print = now
                    
                    # Total drift since beginning
                    drift_total = gyro - gyro_beg
                    duration_total = now - time_gyro_beg
                    drift_rate_total = drift_total / duration_total if duration_total > 0 else 0
                    
                    # Average drift over last print_interval second
                    if len(gyro_history) > 1:
                        dt = gyro_history[-1][0] - gyro_history[0][0]
                        dg = gyro_history[-1][1] - gyro_history[0][1]
                        car.drift_rate_last_sec = dg / dt if dt > 0 else 0
                    else:
                        car.drift_rate_last_sec = 0
                    car.drift_rate_time = now
                    
                    print(
                        f"Gyro Drift: {drift_total:5.2f}° in {duration_total:6.2f}s => {drift_rate_total:5.2f}°/s, "
                        f"Average last {print_interval}s: {car.drift_rate_last_sec:4.2f}°/s, temp: {await request_and_parse_float('t'):2.0f}°"
                    )
                
                await asyncio.sleep(0.1)
        print(f"Arduino connected and start signal received. Drift rate: {car.drift_rate_last_sec}")
    except Exception as e:
        print(f"Exception in connect_arduino: {e}")


async def arduino_communication() -> bool:
    """Exchange commands with the Arduino and update odometry/angle.

    When running, this coroutine sends the current motor command to the
    Arduino and parses back distance and angle telemetry. When paused a
    lighter-weight request is used.

    Returns True on success, False on exception.
    """
    try:
        if not car.paused:  # currently ~30 ms lag to communicate with robot
            command = f"y{int(car.speed)},{int(car.steering)}"
            car.distance, angle = await request_and_parse_float(command, "gyro and distance: ")
            car.angle = angle - (perf_counter() - car.drift_rate_time) * car.drift_rate_last_sec
        else:
            command = "z"
            car.distance, car.angle = await request_and_parse_float(command, "gyro and distance: ")

    except Exception as e:
        print(f"Exception in arduino_communication while sending '{command}': {e}")
        traceback.print_exc()
        return False
    return True    

async def arduino_communication_loop():
    """Background task that periodically calls `arduino_communication`.

    The loop records timings to `loop_timer` and sleeps a short interval
    between calls. Errors are logged and can be extended with recovery
    logic if needed.
    """
    # await asyncio.sleep(2)
    while car.paused: # and not state.skip_arduino:
        loop_timer.record("arduino")
        await asyncio.sleep(0.01)
    while True:
        loop_timer.record("arduino")
        await asyncio.sleep(0.01)
        arduino_ok = await arduino_communication()
        if arduino_ok is False:
            # Optionally, handle error state here (e.g., skip processing, log, etc.)
            print("Arduino communication failed, skipping processing.")



async def process_image(picam2, pipeline):
    """
    Captures an image from the camera, crops it, converts it to HSV, and extracts regions of interest (ROIs)
    for turn marker detection and PD control.

    Args:
        picam2 (Picamera2): The camera object to capture images.
        pipeline (Pipeline): The pipeline object for cropping and color filtering.

    Returns:
        dict: A dictionary containing the processed images and ROIs:
            - color_image: Cropped color image.
            - viz: Copy of the cropped color image for visualization.
            - hsv_image: HSV converted image.
            - roi_center: Center ROI for turn marker detection.
            - rgbl: Dictionary of filtered color masks.
            - roi_left_side: Left side ROI for PD control.
            - roi_right_side: Right side ROI for PD control.
            - roi_left: Cropped left ROI for PD control.
            - roi_right: Cropped right ROI for PD control.
            - portion_black_l: Portion of black in the left ROI.
            - portion_black_r: Portion of black in the right ROI.
    """
    # Combine all CPU-bound image operations into a single synchronous function
    # and run it once in a worker thread. This avoids creating many small
    # threads per frame (one per cv2 call) which caused scheduling overhead.
    def _sync_process():
        img = cv2.cvtColor(picam2.capture_array(), cv2.COLOR_RGB2BGR)
        color_image = pipeline.crop(img)
        # reduce work by smoothing at lower resolution
        # color_image = cv2.bilateralFilter(color_image, 5, 20, 10)
        viz_stream = color_image.copy()
        hsv_image = cv2.cvtColor(color_image, cv2.COLOR_BGR2HSV)

        state.update_stream("roi_center", extract_ROI(hsv_image, [roi_center_x, roi_center_y], [roi_center_x + roi_center_w, roi_center_y + roi_center_h]))

        rgbl = pipeline.filter_RG_Bl(hsv_image, color_image, state.calibrate)

        if "pink" not in state.latest_streams:
            state.update_stream("pink", None)

        if state.parking is not None:
            pink = pipeline.filter_pink(hsv_image)
            state.update_stream("pink", pink)
            portion_pink = extract_ROI(pink, [640//2-roi_front_width, 0], [640//2+roi_front_width, 240])
            state.distance_pink = cv2.countNonZero(portion_pink) / (portion_pink.shape[0] * portion_pink.shape[1])
        elif state.latest_streams.get("pink") is not None:
            state.update_stream("pink", None)

        roi_left_side = extract_ROI(rgbl["black"], [0, 0], [roi_width, rgbl["black"].shape[0]])
        roi_right_side = extract_ROI(rgbl["black"], [640-roi_width, 0], [640, rgbl["black"].shape[0]])

        roi_left = extract_ROI(roi_left_side, [0, 0], [roi_width, roi_height])
        roi_right = extract_ROI(roi_right_side, [0, 0], [roi_width, roi_height])

        portion_black_l = cv2.countNonZero(roi_left) / (roi_left.shape[0] * roi_left.shape[1])
        portion_black_r = cv2.countNonZero(roi_right) / (roi_right.shape[0] * roi_right.shape[1])

        orange_roi = extract_ROI(rgbl["orange"], [roi_center_x, roi_center_y], [roi_center_x + roi_center_w, roi_center_y + roi_center_h])
        blue_roi = extract_ROI(rgbl["blue"], [roi_center_x, roi_center_y], [roi_center_x + roi_center_w, roi_center_y + roi_center_h])
        state.portion_orange = cv2.countNonZero(orange_roi) / (orange_roi.shape[0] * orange_roi.shape[1])
        state.portion_blue = cv2.countNonZero(blue_roi) / (blue_roi.shape[0] * blue_roi.shape[1])

        if state.pillars:
            roi_front = extract_ROI(rgbl["black"], [640//2-roi_front_width, 0], [640//2+roi_front_width, 140])
            state.distance_front = cv2.countNonZero(roi_front) / (roi_front.shape[0] * roi_front.shape[1])

        state.update_stream("color_image", color_image)
        state.update_stream("hsv_image", hsv_image)
        state.update_stream("red", rgbl["red"])
        state.update_stream("green", rgbl["green"])
        state.update_stream("black", rgbl["black"])
        state.update_stream("orange", rgbl["orange"])
        state.update_stream("blue", rgbl["blue"])
        state.update_stream("roi_left", roi_left_side)
        state.update_stream("roi_right", roi_right_side)

        state.portion_black_l = portion_black_l
        state.portion_black_r = portion_black_r
        return viz_stream

    viz_stream = await asyncio.to_thread(_sync_process)
    return viz_stream

async def detect_edge_lines(state: SharedState, roi_width, viz_stream):
    """
    Detects edge lines in the left and right side ROIs using Canny edge detection and Hough line transform.
    Draws detected lines and their properties on the visualization image if not in headless mode.

    Args:
        roi_left_side (np.ndarray): Left side ROI image (mask).
        roi_right_side (np.ndarray): Right side ROI image (mask).
        roi_width (int): Width of the ROI.
        headless (bool): If True, disables visualization drawing.
        viz (np.ndarray): Visualization image to draw lines and annotations on.

    Returns:
        dict: Dictionary with keys 'L' and 'R' containing detected lines for left and right sides as Lines objects.
    """
    # active_roi_sides is either None or a set containing one or both of 'L'/'R'.
    # If None -> skip all line detection (fast). If contains both -> detect both.
    active = getattr(state, 'active_roi_sides', None)
    # If no active ROI sides are requested and we're not in calibrate mode
    # then normally skip detection to save CPU. However, when `state.parking`
    # is set we still want to run detection for the pink parking marker.
    if active is None and not state.calibrate and state.latest_streams.get("pink") is None:
        # Nothing to detect: return early
        return viz_stream

    # Normalize active to a set for easy checks; if calibrate is on, always use both
    if state.calibrate:
        active_sides = {"L", "R"}
    else:
        active_sides = set(c for c in (active or []) if c in ("L", "R"))
    labels = []
    if "L" in active_sides:
        labels.append("L")
    if "R" in active_sides:
        labels.append("R")
    # include pink when present and either calibrate or both sides active
    if state.latest_streams.get("pink") is not None:
        labels.append("P")

    # Run the full detection pipeline for all ROIs in one worker thread to
    # reduce task-switching overhead.
    def _sync_detect():
        roi_lines = {label: [] for label in labels}
        image_map = {
            "L": state.latest_streams.get("roi_left"),
            "R": state.latest_streams.get("roi_right"),
            "P": state.latest_streams.get("pink")
        }
        for key in labels:
            image = image_map.get(key)
            if image is None:
                roi_lines[key] = None
                continue
            img = image[5:, :]
            blurredImg = cv2.GaussianBlur(img, (3, 3), 0)
            lower = 30
            upper = 90
            edges_img = cv2.Canny(blurredImg, lower, upper, 3)
            min_len = 10 if key == "P" else 25
            roi_lines[key] = cv2.HoughLinesP(edges_img, 1, np.pi/180, 10, minLineLength=min_len, maxLineGap=50)
        return roi_lines

    roi_lines = await asyncio.to_thread(_sync_detect)

    # Fill border_lines for processed labels; set empty Lines for skipped ones
    processed = set()
    for key in labels:
        processed.add(key)
        if key == "L":
            state.border_lines[key] = Lines(roi_lines.get(key), (0, 5), (0, 0))
        elif key == "R":
            state.border_lines[key] = Lines(roi_lines.get(key), (640 - roi_width, 5), (roi_width, 0))
        elif key == "P":
            state.border_lines[key] = Lines(roi_lines.get(key), (0, 5), (640 // 2, 0))
    if "L" not in processed:
        state.border_lines["L"] = Lines(None, (0, 5), (0, 0))
    if "R" not in processed:
        state.border_lines["R"] = Lines(None, (640 - roi_width, 5), (roi_width, 0))

    max_diff = 400 # max difference squared of the two points to be the same point
    for side_letter in ["L", "R"]:
        # If this side was not processed, its border_lines entry may be None
        line_group = state.border_lines.get(side_letter)
        state.detected_corners[side_letter] = None
        if line_group is None:
            continue
        lines = line_group.lines or []
        if not lines:
            continue
        # calculate the slope and intercept of the line
        for line in lines:
            # detect if any line forms a corner with the first line
            if not (line["m"] != 0 and lines[0]["m"] != 0 and line["m"]/abs(line["m"]) != lines[0]["m"]/abs(lines[0]["m"])):
                continue
            if (line["x2"] - lines[0]["x1"]) ** 2 + (line["y2"] - lines[0]["y1"]) ** 2 < max_diff:
                state.detected_corners[side_letter] = ((line["x2"] + lines[0]["x1"]) // 2, (line["y2"] + lines[0]["y1"]) // 2, lines.index(line), "different")
                break
            elif (line["x1"] - lines[0]["x2"]) ** 2 + (line["y1"] - lines[0]["y2"]) ** 2 < max_diff:
                state.detected_corners[side_letter] = ((line["x1"] + lines[0]["x2"]) // 2, (line["y1"] + lines[0]["y2"]) // 2, lines.index(line), "different")
                break
            elif (line["x2"] - lines[0]["x2"]) ** 2 + (line["y2"] - lines[0]["y2"]) ** 2 < max_diff:
                state.detected_corners[side_letter] = ((line["x2"] + lines[0]["x2"]) // 2, (line["y2"] + lines[0]["y2"]) // 2, lines.index(line), "same")
                break
            elif (line["x1"] - lines[0]["x1"]) ** 2 + (line["y1"] - lines[0]["y1"]) ** 2 < max_diff:
                state.detected_corners[side_letter] = ((line["x1"] + lines[0]["x1"]) // 2, (line["y1"] + lines[0]["y1"]) // 2, lines.index(line), "same")
                break
                
        if not state.headless and state.detected_corners[side_letter] != None:
            corner_x = state.detected_corners[side_letter][0] + lines[0]["x_offset"]
            cv2.circle(viz_stream, (corner_x, state.detected_corners[side_letter][1] + lines[0]["y_offset"]), 4, (255, 255 if state.detected_corners[side_letter][-1] == "different" else 100, 0), -1)
            cv2.putText(viz_stream, f"({state.detected_corners[side_letter][0]:4.0f}, {state.detected_corners[side_letter][1]:4.0f})", (int((roi_width-72)/2 + (640-roi_width if lines[0]["x_offset"] != 0 else 0)), 190 + lines[0]["y_offset"]), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)

    if "P" in labels:
        p_group = state.border_lines.get("P")
        lines = p_group.lines if (p_group is not None) else []
        if len(lines) != 0:
            lines.sort(key=lambda x: (x["x"]), reverse=(state.round_dir == 1))
            highest_y = 0
            index_highest_y = 0
            index = 0
            lines = [
                line for line in lines
                if not (line["x"] * state.round_dir > 50)
            ]
            if len(lines) != 0:
                for line in lines:
                    if abs(line["m"]) < 10 or abs(line["x1"] - lines[max(index-1, 0)]["x1"]) > 20:
                        break
                    index += 1
                    if highest_y < line["y1"] or highest_y < line["y2"]:
                        highest_y = max(highest_y, line["y1"], line["y2"])
                        index_highest_y = index
                if index_highest_y >= 1:
                    index_highest_y -= 1
                index = index_highest_y
                # print(f"index: {index}, lines: {lines}")
                if lines[index]["y1"] > lines[index]["y2"]:
                    state.parking_x = lines[index]["x1"]
                    state.parking_y = lines[index]["y1"]
                else:
                    state.parking_x = lines[index]["x2"]
                    state.parking_y = lines[index]["y2"]
                if not state.headless:
                    cv2.circle(viz_stream, (state.parking_x + lines[index]["x_offset"], state.parking_y +lines[index]["y_offset"]), 5, (255, 150, 0), -1)
                    cv2.putText(viz_stream, f"index = {index}", (320, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
                    cv2.putText(viz_stream, f"x = {state.parking_x}", (320, 180), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
                    cv2.putText(viz_stream, f"y = {state.parking_y}", (320, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
                    cv2.putText(viz_stream, f"y = {lines[index]['m']} * x + {lines[index]['b']}", (320, 160), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
    # visualisation
    if not state.headless:
        for line_group in state.border_lines.values():
            if line_group is not None:
                b = 200
                for line in line_group.lines:
                    cv2.line(viz_stream, (line["x1"] + line["x_offset"], line["y1"] + line["y_offset"]), 
                        (line["x2"] + line["x_offset"], line["y2"] + line["y_offset"]), (b, 100, 0), 1)
                    # cv2.putText(viz_stream, f"{line['x1'] + line['x_offset']} {line['y1'] + line['y_offset']} {line['x2'] + line['x_offset']} {line['y2'] + line['y_offset']}", 
                    #     (line["x1"] + line["x_offset"], line["y1"] + line["y_offset"]), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
                    # create a green dot for x1 y1 and a red dot for x2 y2
                    cv2.circle(viz_stream, (line["x1"] + line["x_offset"], line["y1"] + line["y_offset"]), 2, (0, 255, 0), -1)
                    cv2.circle(viz_stream, (line["x2"] + line["x_offset"], line["y2"] + line["y_offset"]), 2, (0, 0, 255), -1)
                    if b == 200:
                        cv2.putText(viz_stream, f"q: {line['b']:5.1f}", 
                            (int((roi_width-48)/2 + (640-roi_width if line["x_offset"] != 0 else 0)), 200 + line["y_offset"]), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
                    b *= 0.6
    return viz_stream


async def cycle():
    """Perform a single image processing cycle and update visualization.

    Captures an image, runs the detection pipeline (when enabled) and draws
    debug overlays on the visualization stream which is saved into
    `state.latest_streams['viz']`.
    """
    viz_stream = await process_image(picam2, pipeline)
    if state.pillars:
        viz_stream = await detect_edge_lines(state, roi_width, viz_stream)
        # # filter out the red and green colors of the pillars and walls
        # detected_pillars_r = pipeline.get_pillars(state.latest_streams["red"], "RED")
        # detected_pillars_g = pipeline.get_pillars(state.latest_streams["green"], "GREEN")
        # state.detected_pillars = detected_pillars_r + detected_pillars_g

        # state.detected_pillars.sort(key=lambda x: x.width*x.height, reverse=True)

    # viz stuff
    if not state.headless:
        cv2.rectangle(viz_stream, (0, 0), (roi_width, 150), (255, 0, 0), 1)
        cv2.rectangle(viz_stream, (640-roi_width, 0), (640, 150), (255, 0, 0), 1)
        cv2.putText(viz_stream, f"{state.portion_black_l:4.2f}", ((roi_width-26)//2, 160), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz_stream, f"{state.portion_black_r:4.2f}", (640-roi_width + (roi_width-26)//2, 160), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz_stream, f"o: {state.portion_orange:4.2f}", (300, 210), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz_stream, f"b: {state.portion_blue:4.2f}", (300, 222), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.rectangle(viz_stream, (roi_center_x, roi_center_y), (roi_center_x + roi_center_w, roi_center_y + roi_center_h), (0, 255, 0), 1)
        cv2.putText(viz_stream, f"Speed: {car.speed:3.0f} mm/s, Steering: {car.steering:4.2f}", (10, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz_stream, f"Angle: {car.angle:6.1f} deg, Distance: {car.distance:5.0f} mm", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz_stream, f"Direction: {state.round_dir:2.0f}, Corners: {state.rounds:2.0f}", (640-150, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz_stream, f"Position: {state.position}", (640-150, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz_stream, f"Current function: {state.current_function}", (10, 230), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        if state.parking == "Start":
            cv2.putText(viz_stream, f"p: {state.distance_pink:4.2f}", (300, 234), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        if state.parking in ["L", "R"]:
            line = lambda x: -0.35 * state.round_dir * x - 13
            await asyncio.to_thread(cv2.line, viz_stream, (0+640//2, int(line(0))), (-200*state.round_dir+640//2, int(line(-200*state.round_dir))), (255, 255, 255))
            line = lambda x: -0.95 * state.round_dir * x - 13
            await asyncio.to_thread(cv2.line, viz_stream, (0+640//2, int(line(0))), (-200*state.round_dir+640//2, int(line(-200*state.round_dir))), (255, 255, 255))
            m = -0.95 * state.round_dir
            q = -13
            x1 = state.parking_x
            y1 = state.parking_y
            x2 = ((y1 - q) * m + x1)/(m**2 + 1)
            y2 = m * x2 + q
            await asyncio.to_thread(cv2.line, viz_stream, (int(x2)+640//2, int(y2)), (int(x1)+640//2, int(y1)), (255, 255, 255))
        if state.pillars:
            cv2.rectangle(viz_stream, (640//2-roi_front_width, 0), (640//2+roi_front_width, 140), (255, 0, 0), 1)
            cv2.putText(viz_stream, f"{state.distance_front:4.2f}", ((640-26)//2, 150), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            # for p in state.detected_pillars:
            #     cv2.line(viz_stream, (p.screen_x, 0), (p.screen_x, 480), (0, 0, 255) if p.color == "RED" else (0, 255, 0), 1)
    state.latest_streams["viz"] = viz_stream
    # pipeline.process_pillars(state, straight_sections)

async def cycle_loop():
    """
    Main loop that continuously processes images

    This function captures images from the camera, processes them to extract relevant features

    Returns:
        None
    """
    state.last_error = 0.0
    state.has_sent_streams_info = False
    # time_now = time()
    while True:
        # time_beg = time()
        await cycle()
        # print(f"passed cycle time: {time() - time_now:.3f} s; loop cycle: {time() - time_beg:.3f} s")
        # time_now = time()
        loop_timer.record("cycle")
        await asyncio.sleep(0.02)  # Sleep for a short duration to prevent blocking

async def img_stream(websocket, path):
    """Websocket handler for the image streaming web UI.

    Maintains a single active websocket connection; accepts small JSON
    commands from the client to select streams or update filters and
    responds with base64-encoded JPEG images for the requested streams.
    """
    # todo: make sure old webserver is closed properly before starting a new one
    print("Websocket connection established")
    # If there is already an active websocket, close it
    if state.active_websocket is not None and not state.active_websocket.closed:
        print("Closing previous websocket connection...")
        await state.active_websocket.close()
    state.active_websocket = websocket
    # Send initial stream info
    if not state.has_sent_streams_info:
        state.has_sent_streams_info = True
        await websocket.send(json.dumps({
            "streams": list(state.latest_streams.keys()),
        }))

    try:
        while True:
            # check if the websocket has sent a stream request, wait at most for 0.05 seconds
            loop_timer.record("img_stream")
            try:
                res = json.loads(await asyncio.wait_for(websocket.recv(), timeout=0.01))
                if "updateGray" in res:
                    # Update the GRAY value in the config
                    config["filters"]["GRAY"] = res["updateGray"]
                    print(f"Updated GRAY value: {config['filters']['GRAY']}")

                    # Save the updated config back to the file asynchronously
                    async def async_write_config(cfg):
                        await asyncio.to_thread(lambda: open("config.json", "w").write(json.dumps(cfg, indent=2)))
                    await async_write_config(config)
                state.current_streams[0] = res["streamA"]
                state.current_streams[1] = res["streamB"]
                state.current_streams[2] = res["streamC"]
            except:
                pass

            # Only send if changed
            data = {}
            # Asynchronously encode all images in parallel
            async def async_encode(val):
                if isinstance(val, np.ndarray):
                    return await asyncio.to_thread(encode_image, val, state.hq)
                return val
            tasks = [async_encode(state.latest_streams.get(stream_name)) for stream_name in state.current_streams]
            results = await asyncio.gather(*tasks)
            for idx, encoded in enumerate(results):
                if encoded is not None:
                    data[chr(ord('a') + idx)] = encoded
            await websocket.send(json.dumps(data))
            await asyncio.sleep(0.05)
    finally:
        # If this websocket is the active one, clear it on disconnect
        if state.active_websocket == websocket:
            state.active_websocket = None


async def run_webserver():
    """Start the websocket server for the image web viewer.

    Binds to 0.0.0.0:8765 and serves `img_stream` handler until the
    program exits.
    """
    async with serve(img_stream, "0.0.0.0", 8765):
        print("Webserver started on ws://0.0.0.0:8765")
        await asyncio.Future()  # Run forever

async def printer_task():
    """Print all loop durations on one line, once per second."""
    while True:
        await asyncio.sleep(1)
        durations = loop_timer.get_last_durations()
        line = " | ".join(f"{name}: {dt*1000:.2f} ms" for name, dt in durations.items())
        print(line)

async def main():
    """Program entrypoint that parses CLI flags and starts asyncio tasks.

    Supported CLI flags include `--headless`, `--pillars`, `--shutdown`,
    `--calibrate`, `--skip-arduino` and `--hq`. The function configures the
    shared `state` object, creates background tasks (camera loop, Arduino
    comms, webserver, main_program) and waits for them to complete.
    """
    parser = argparse.ArgumentParser(description="Check if --headless flag was given.")
    parser.add_argument('--headless', action='store_true', help='Run in headless mode')
    parser.add_argument('--pillars', action='store_true', help='Run in pillar mode')
    parser.add_argument('--shutdown', action='store_true', help='Shutdown after run')
    parser.add_argument('--calibrate', action='store_true', help='Disable driving and moving to next states')
    parser.add_argument('--skip-arduino', action='store_true', help='Skip Arduino connection')
    parser.add_argument('--hq', action='store_true', help='Stream in High Quality')
    parser.add_argument('--record', action='store_true', help='Record the whole run at low quality to a file')
    args = parser.parse_args()
 
    state.set_flags(
        headless=args.headless,
        pillars=args.pillars,
        shutdown=args.shutdown,
        calibrate=args.calibrate,
        skip_arduino=args.skip_arduino,
        hq=args.hq
    )

    state.kp = configloader.get_property("PD")['kp']
    state.kd = configloader.get_property("PD")['kd']
    
    setup_logging()
    
    # connect_arduino is now called at the start of main_program

    # # During startup (and when calibrate is active) we want to run full
    # # edge/corner detection on both sides so problems are visible.
    # if state.calibrate:
    #     # In calibrate mode, always process both sides so you can see issues
    #     state.active_roi_sides = {"L", "R"}

    tasks = [asyncio.create_task(cycle_loop()),
             asyncio.create_task(arduino_communication_loop())]
    if not state.headless:
        tasks.append(asyncio.create_task(run_webserver()))
    if not state.calibrate:
        tasks.append(asyncio.create_task(main_program()))
    if args.record:
        tasks.append(asyncio.create_task(record_run(low_quality=True, fps=10)))
    # tasks.append(asyncio.create_task(printer_task()))

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("KeyboardInterrupt received. Stopping robot...")
        try:
            if 'ser' in globals() and ser is not None:
                ser.write(b's0\n')  # Stop steering
                ser.write(b'p\n')   # Stop driving
                print("Sent stop commands to robot via serial.")
        except Exception as e:
            print(f"Error sending stop commands: {e}")
        # Cancel all running tasks and exit immediately
        for task in asyncio.all_tasks():
            task.cancel()
        await asyncio.sleep(0.1)  # Give tasks a moment to cancel
        os._exit(0)

def encode_image(image, hq: bool = False):
    """Encode an image (numpy array) to a base64 JPEG string for websocket transport.

    Args:
        image (np.ndarray | None): Image to encode. If None, returns None.
        hq (bool): If True encode at high JPEG quality and full size; otherwise
                   downscale large images and use a lower quality setting for
                   bandwidth savings.

    Returns:
        str | None: Base64 encoded JPEG string or None if image was None.
    """
    if image is None:
        return None
    if hq:
        retval, buffer = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 99])
        base64_str = base64.b64encode(buffer).decode('utf-8')

    else:
        # Downscale large frames for faster JPEG encoding and reduced bandwidth
        h, w = image.shape[:2]
        if w > 480:
            new_w = 480
            new_h = int(h * new_w / w)
            image = cv2.resize(image, (new_w, new_h))
        # Use slightly lower quality to speed up encoding and reduce size
        retval, buffer = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        base64_str = base64.b64encode(buffer).decode('utf-8')
    return base64_str

async def record_run(low_quality: bool = True, fps: int = 10, segment_seconds: int = 8):
    """Record the `viz` stream to a single file using ffmpeg if available.

    Preferred method: spawn `ffmpeg` and stream JPEG frames to its stdin
    (image2pipe). We use `-movflags +frag_keyframe+empty_moov -g 1` so the
    output MP4 is fragmented and playable while being written. If `ffmpeg`
    isn't on PATH, fall back to segmenting MJPG .avi files (previous
    behavior).
    """
    os.makedirs("logs", exist_ok=True)
    base_ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    fps = int(fps)

    ffmpeg_exe = shutil.which("ffmpeg")
    if ffmpeg_exe:
        outname = f"logs/run_{base_ts}.mp4"
        # Build ffmpeg command: read MJPEG frames from stdin and write
        # fragmented MP4 that is playable while writing.
        cmd = [
            ffmpeg_exe,
            '-y',
            '-f', 'mjpeg',
            '-i', '-',
            '-r', str(fps),
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-crf', '28',
            '-pix_fmt', 'yuv420p',
            '-g', '1',
            '-movflags', '+frag_keyframe+empty_moov',
            outname
        ]

        print(f"Starting ffmpeg recording to {outname}")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

        try:
            interval = 1.0 / float(fps)
            while True:
                frame = state.latest_streams.get('viz')
                if frame is not None:
                    # encode using same helper (downscales when hq=False)
                    b64 = encode_image(frame, hq=False)
                    if b64:
                        jpg = base64.b64decode(b64)
                        # write to ffmpeg stdin in a thread
                        await asyncio.to_thread(proc.stdin.write, jpg)
                        await asyncio.to_thread(proc.stdin.flush)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            # Close stdin to let ffmpeg finalize
            try:
                proc.stdin.close()
            except Exception:
                pass
            await asyncio.to_thread(proc.wait)
            print(f"ffmpeg recording finished: {outname}")
            raise
        except Exception as e:
            print(f"Exception in ffmpeg record_run: {e}")
            try:
                proc.stdin.close()
            except Exception:
                pass
            await asyncio.to_thread(proc.wait)
            raise
    else:
        # Fallback: segment into small MJPG .avi files (keeps previous behavior)
        print("ffmpeg not found; falling back to segmented AVI recording")
        segment_index = 0
        writer = None
        current_filename = None
        fourcc = cv2.VideoWriter_fourcc(*'MJPG')
        interval = 1.0 / float(fps)
        try:
            seg_start = perf_counter()
            while True:
                frame = state.latest_streams.get("viz")
                if frame is not None:
                    frame_to_write = frame
                    if low_quality:
                        h, w = frame.shape[:2]
                        target_w = 320
                        if w != target_w:
                            new_h = int(h * target_w / w)
                            frame_to_write = cv2.resize(frame, (target_w, new_h))
                    if writer is None:
                        current_filename = f"logs/run_{base_ts}_part{segment_index:04d}.avi"
                        writer = cv2.VideoWriter(current_filename, fourcc, float(fps), (frame_to_write.shape[1], frame_to_write.shape[0]))
                        seg_start = perf_counter()
                        print(f"Started recording segment: {current_filename}")
                    await asyncio.to_thread(writer.write, frame_to_write)

                if writer is not None and (perf_counter() - seg_start) >= float(segment_seconds):
                    await asyncio.to_thread(writer.release)
                    try:
                        fd = os.open(current_filename, os.O_RDONLY)
                        await asyncio.to_thread(os.fsync, fd)
                        os.close(fd)
                    except Exception:
                        pass
                    print(f"Finalized segment: {current_filename}")
                    segment_index += 1
                    writer = None

                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            if writer is not None:
                try:
                    await asyncio.to_thread(writer.release)
                except Exception:
                    pass
                try:
                    fd = os.open(current_filename, os.O_RDONLY)
                    await asyncio.to_thread(os.fsync, fd)
                    os.close(fd)
                except Exception:
                    pass
                print(f"Recording finished, last segment: {current_filename}")
            raise
        except Exception as e:
            print(f"Exception in record_run fallback: {e}")
            if writer is not None:
                try:
                    await asyncio.to_thread(writer.release)
                except Exception:
                    pass
            raise
    
MAX_STEERING_ANGLE = 25.0

async def calculate_steering(error, speed = 200) -> float:
    """PD controller wrapper that computes a steering command from an error.

    Uses state.kp and state.kd and a time-aware derivative term. Stores the
    last sampled error and timestamp in the global SharedState instance so the
    derivative is computed across calls.

    Args:
        error (float): Current error signal (positive/negative).
        speed (int): Current driving speed (optional). Some tuning parameters
                     change when driving at high speed.

    Returns:
        float: Steering command scaled to [-MAX_STEERING_ANGLE, MAX_STEERING_ANGLE].
    """
    # Use time-aware derivative: derivative = (error - last_error) / dt
    kp = state.kp
    kd = state.kd
    if (speed > 220 and state.pillars):
        kp = 4
        kd = 0.1
    if not state.pillars:
        kp = 5
        kd = 0.1
        
    now = perf_counter()
    last_time = getattr(state, 'last_error_time', None)
    if last_time is None:
        dt = None
    else:
        dt = now - last_time

    # proportional term
    p = kp * error

    # derivative term (guard against zero or very small dt)
    if dt is None or dt <= 0:
        d = 0.0
    else:
        derivative = (error - state.last_error) / dt
        d = kd * derivative

    correction = p + d
    state.last_error = error
    state.last_error_time = now
    return bound(correction) * MAX_STEERING_ANGLE

def current_function(func):
    """Decorator that records the currently executing function call in state.

    The decorator stores a short string representation of the function name
    and its arguments into `state.current_function` for runtime diagnostics
    and then calls the wrapped function.
    """
    def wrapper(*args, **kwargs):
        # saves the whole function call as in the python file
        state.current_function = func.__name__ + "(" + ", ".join([str(a) for a in args] + [f"{k}={v}" for k, v in kwargs.items()]) + ")"
        return func(*args, **kwargs)
    return wrapper

@current_function
async def drive(speed, distance, stop_condition: callable = lambda: False, angle_beg: float = None):
    if angle_beg is None:
        angle_beg = car.straight_direction
    if speed == 0:
        return
    if distance < 0:
        speed = -speed
        distance = -distance
    distance_beg = car.distance
    car.speed = speed
    direction = speed / abs(speed)
    while ((distance != 0 and (car.distance - distance_beg) * direction < distance) or (distance == 0 and not stop_condition())) and not car.stalled:
        error = (angle_beg - car.angle) / 80 * direction
        car.steering = await calculate_steering(error)
        await asyncio.sleep(0.01)
    car.speed = 0  # Stop the car after driving the distance

@current_function
async def turn(speed, degrees, steering, angle_beg = None):
    """   
    Args:
        speed (int): Speed of the turn.
        degrees (float): Degrees to turn.
        steering (float, 0.0 to 1.0): Steering intensity.
    
    Returns:
        None
    """
    if degrees == 0 or speed == 0:
        return
    if angle_beg == None:
        angle_beg = car.angle
    # Calculate the target angle based on the current angle and degrees to turn
    direction = degrees / abs(degrees)
    car.speed = speed
    car.steering = bound(abs(steering) * direction) * MAX_STEERING_ANGLE
    degrees *= speed / abs(speed)
    direction *= speed / abs(speed)
    # Adjust the steering based on the radius
    while (car.angle - angle_beg) * direction < degrees * direction:
        await asyncio.sleep(0.01)
    car.straight_direction += degrees
    car.speed = 0  # Stop the car after turning
    
@current_function
async def double_turn(speed, angle, steering=0.75):
    """Perform two consecutive turns in opposite directions.

    This is a small helper that executes `turn()` twice to produce an S-like
    steering maneuver and then stops the car.
    """
    await turn(speed, angle, steering)
    await turn(speed, -angle, steering)
    car.speed = 0
    
REF_PORTION = 0.45 if not state.pillars else 0.30
REF_PORTION_SIDE = 0.8

@current_function
async def pd_middle(speed: int, side: str, stop_condition: callable):
    car.speed = speed
    while not stop_condition():
        if side == "R":
            error = (REF_PORTION - state.portion_black_r)
        elif side == "L":
            error = (state.portion_black_l - REF_PORTION)
        else:
            raise ValueError(f"side must be 'L' or 'R', currently it is set to '{side}'")
        car.steering = await calculate_steering(error*1.2)
        await asyncio.sleep(0.01)
        
@current_function
async def follow_wall(speed: int, side: str = state.position, stop_condition: callable = lambda: False, wait_for_corner = True, angle_beg: float = None):
    """ follows the wall until an a corner is detected  or the stop_condition is satisfied"""
    if angle_beg == None:
        angle_beg = car.straight_direction
    car.speed = speed
    error = 0
    roi_side = "L" if state.round_dir * (1 if side == "inner" else -1) == 1 else "R"
    # start-time for this follow_wall invocation and reset buffer
    start_time = perf_counter()
    try:
        state.angle_buffer.clear()
    except Exception:
        pass

    # Indicate which ROI side(s) should be processed by the detector.
    # While following a wall we only need the ROI for the active side.
    # Use a simple set containing one of {'L','R'}.
    state.active_roi_sides = {roi_side}

    updated_mean_angle = None
    updated_mse = None

    while True:
        error = 0
        loop_timer.record("follow_wall")
        diff_angle = car.angle - angle_beg
        # collect angle samples for stability check (mean + MSE)
        try:
            now = perf_counter()
            state.angle_buffer.append(now, car.angle)
            mean_angle, mse = state.angle_buffer.mean_and_mse()
            if mean_angle is not None and mse is not None and (now - start_time) >= state.angle_buffer.window:
                # only change straight_direction when the buffer spans the configured window
                if mse < 10 and abs(mean_angle - car.straight_direction) < 10:
                    # print(f"Updating straight direction to {mean_angle:.2f} deg (MSE: {mse:.2f})")
                    car.straight_direction = mean_angle
                    updated_mean_angle = mean_angle
                    updated_mse = mse
                elif mse < 10:
                    pass
                    # print(f"Not updating straight direction (MSE: {mse:.2f}), because mean angle deviation is {abs(mean_angle - car.straight_direction):.2f} deg")
        except Exception:
            # be defensive: do not break the control loop on buffer errors
            # keep silent in production, but print for debugging
            print("Angle buffer error")
            pass
        detected_corner = state.detected_corners.get(roi_side)
        line_group = state.border_lines.get(roi_side)
        lines = line_group.lines if (line_group is not None) else []
        if len(lines) > 0:
            slope = lines[0]["m"]
            intercept = lines[0]["b"]
            if side == "inner":
                # if we detected a corner, we should start gyro following
                if detected_corner != None and wait_for_corner:
                    slope_2 = lines[detected_corner[2]]["m"]
                    if (abs(slope) > 3 or abs(slope_2) > 3) and detected_corner[1] > 100 and detected_corner[3] == "same":
                        cv2.putText(state.latest_streams["viz"], f"slope {slope}, slope2 {slope_2}, ({detected_corner[0]}, {detected_corner[1]})", (10, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
                        # if we reach the end of the wall
                        cv2.imwrite(f"logs/image_corner_{state.rounds}.jpg", state.latest_streams["viz"])
                        break
                else:
                    error = (160 - intercept) / 250 * -state.round_dir
                    # increase the bounded error quadratically if the angle is too high
                    if diff_angle != 0:
                        error = bound(error) - (diff_angle / 80) ** 2 * diff_angle / abs(diff_angle)
            elif side == "outer":
                error = (intercept - 150) / 250 * -state.round_dir
                if diff_angle != 0:
                    error = bound(error) - (diff_angle / 80) ** 2 * diff_angle / abs(diff_angle)
            elif side == "middle_parking":
                if (slope * state.round_dir > 0):
                    error = (intercept - 74) / 250 * -state.round_dir
                    if diff_angle != 0:
                        error = bound(error) - (diff_angle / 160) ** 2 * diff_angle / abs(diff_angle)
                else:
                    if diff_angle != 0:
                        error = bound(error) - (diff_angle / 25) ** 2 * diff_angle / abs(diff_angle)
            elif side == "middle_parking_end":
                error = (intercept - 80) / 250 * -state.round_dir
                if diff_angle != 0:
                    error = bound(error) - (diff_angle / 50) ** 2 * diff_angle / abs(diff_angle)
            elif side == "middle":
                if (slope * state.round_dir > 0):
                    error = (intercept - 53) / 100 * -state.round_dir
                    if diff_angle != 0:
                        error = bound(error) - (diff_angle / 80) ** 2 * diff_angle / abs(diff_angle)
                else:
                    if diff_angle != 0:
                        error = bound(error) - (diff_angle / 25) ** 2 * diff_angle / abs(diff_angle)
            else:
                raise ValueError(f"side must be 'inner', 'outer' or 'middle', currently it is set to '{side}'")
        else:
            # if diff_angle != 0:
            #     error = bound(error) - (diff_angle / 25) ** 2 * diff_angle / abs(diff_angle)
            print(f"Falling back to wall following with percentage; side: {roi_side}")
            if roi_side == "L":
                error = state.portion_black_l - REF_PORTION_SIDE
            else:
                error = REF_PORTION_SIDE - state.portion_black_r
        if stop_condition():
            break

        car.steering = await calculate_steering(error, speed)            
        await asyncio.sleep(0.01)
    state.active_roi_sides = None

    if (updated_mean_angle is not None and updated_mse is not None):
        print(f"Updated straight direction to {updated_mean_angle:.2f} deg (MSE: {updated_mse:.2f}) ------------------------------------------")

@current_function
async def pd_point(speed: int, error: callable, stop_condition: callable, scaler: float = 1):
    car.speed = speed
    while not stop_condition():
        car.steering = await calculate_steering(error()*scaler)
        await asyncio.sleep(0.01)
    print(f"lower {state.lower_point}")
    
@current_function
async def stop(directly = False):
    car.speed = 0
    if directly:
        await write_serial("p\n")
        await asyncio.sleep(0.1)                        
        await write_serial("m\n")
    else:
        await asyncio.sleep(0.5)

def blue_orange(colour: str) -> bool:
    MIN_PORTION = 0.15
    if colour == "blue":
        if state.portion_blue > MIN_PORTION:
            return True
    elif colour == "orange":
        if state.portion_orange > MIN_PORTION:
            return True
    else:
        raise ValueError(f"colour has to be 'blue' or 'orange', currently it is set to '{colour}'")
    return False

def distance_front_camera(percentage: float, colour: str = "black") -> bool:
    if colour == "black":
        distance = state.distance_front
    elif colour == "pink":
        distance = state.distance_pink
    else:
        raise ValueError(f"Colour is not known. The current colour is '{colour}'")
    if distance > percentage:
        return True
    else:
        return False

def distance(distance: float, distance_beg: float) -> bool:
    if car.distance - distance_beg < distance:
        return False
    return True

def calculate_xy_error():
    """Compute signed euclidean error between parking point and reference line.

    Projects the detected parking point onto a reference line and returns the
    euclidean distance; sign indicates lateral offset direction.
    """
    m = -0.95 if state.round_dir == 1 else 0.9
    q = -13
    x1 = state.parking_x
    y1 = state.parking_y
    x2 = ((y1 - q) * m + x1)/(m**2 + 1)
    y2 = m * x2 + q
    error = (x1-x2)**2 + (y1-y2)**2
    error = math.sqrt(error)
    if x1 != x2:
        error *= (x1-x2)/abs(x1-x2)
    return error

async def parking():
    state.parking = "R" if state.round_dir == -1 else "L"
    # Parking: 
    SPEED_PARK = 100
    print(f"state.parking = {state.parking}")
    # wall_line = lambda x: -0.35 * state.round_dir * x - 13
    # parking_line = lambda x: m * x + q
    car.straight_direction = car.angle
    await pd_point(150, lambda: (calculate_xy_error()), (lambda: abs(state.parking_x) > 90), 0.006)
    await pd_point(100, lambda: (calculate_xy_error()), (lambda: abs(state.parking_x) > 150), 0.007)
    cv2.imwrite(f"logs/park1.jpg", state.latest_streams["viz"])
    await pd_point(50, lambda: (calculate_xy_error()), (lambda: abs(state.parking_x) > (160 if state.round_dir == -1 else 180)), 0.009)
    cv2.imwrite(f"logs/park2.jpg", state.latest_streams["viz"])

    await turn(SPEED_PARK, (63 if state.round_dir == -1 else 63) * state.round_dir, 1) # turning to little for round = 1
    car.straight_direction += 2 * state.round_dir
    car.steering = 0
    await stop(True)
    await asyncio.sleep(0.2)
    await drive(-SPEED_PARK, 255 + (0 if state.round_dir == -1 else 5)) # 260 seems to be about the maximum
    await turn(-SPEED_PARK, 45 * state.round_dir, 1)
    car.steering = state.round_dir
    await stop(True)
    await turn(SPEED_PARK, -10 * state.round_dir, 1)
    await stop(True)
    car.steering = 0
    await asyncio.sleep(0.1)



DISTANCE_TO_WALL = 0.95  # Distance to the wall for state transition

async def main_program():
    # Wait for Arduino trigger before starting main logic
    # if not state.headless:
    #     if state.pillars:
    #         state.round_dir = -1
        
    #     # state.parking = "R" if state.round_dir == -1 else "L"
    #     # state.rounds = 1
    #     # state.parking = "Start"
    #     state.active_roi_sides = "RL"
        
    #     if ser and not state.calibrate:
    #         await connect_to_arduino()
    #     speed = 300 if not state.pillars else 200
    #     fspeed = speed * 1.5
    #     print("Starting main program...")
    #     run_time = perf_counter()
        
    #     # distance_beg, angle_beg = car.distance, car.angle
    #     # await double_turn(speed, 65, 1)
    #     # print(f"current distance: {car.distance - distance_beg} cm and angle {car.angle - angle_beg}")
    #     # await follow_wall(fspeed, "outer")
    #     # await follow_wall(0.6*speed, "middle", lambda: False, False)
    #     # await stop(True)
    #     if state.pillars:
    #         await parking()
    #         await asyncio.sleep(2)

    if ser and not state.calibrate:
        await connect_to_arduino()
    speed = 300 if not state.pillars else 200
    fspeed = speed * 1.5
    print("Starting main program...")
    run_time = perf_counter()
    
    try:
        if not state.pillars:
            car.speed = speed
        while abs(state.round_dir) < 5:
            state.round_dir += find_round_dir(state, state.pillars)
            await asyncio.sleep(0.01)
        print(f"Round direction determined from {state.round_dir}: {'clockwise' if state.round_dir < 0 else 'counter-clockwise'}")
        state.round_dir = 1 if state.round_dir > 0 else -1
        inner_colour = "RED" if state.round_dir < 0 else "GREEN"
        outer_colour = "GREEN" if state.round_dir < 0 else "RED"
        if not state.pillars:
            while state.rounds < 12:
                await pd_middle(speed, "L" if state.round_dir < 0 else "R", lambda: blue_orange("orange" if state.round_dir < 0 else "blue"))
                state.rounds += 1
                print(f"detected line: rounds done: {state.rounds}")
                await pd_middle(speed, "L" if state.round_dir < 0 else "R", (lambda start=car.distance: lambda: distance(170, start))())
                await turn(speed, -80 * state.round_dir, 0.75)
            await pd_middle(speed, "L" if state.round_dir < 0 else "R", (lambda start=car.distance: lambda: distance(1200, start))())
        else: # pillar round
            state.parking = "Start"
            car.straight_direction = car.angle
            print(f"Initial straight direction: {car.straight_direction}")
            SPEED_UNPARK = 100
            distance_front_unparking = 0.85
            if distance_front_camera(distance_front_unparking, "pink"):
                await drive(-SPEED_UNPARK/2, 0, lambda: not distance_front_camera(distance_front_unparking+0.05, "pink"))
            else:
                await drive(SPEED_UNPARK/2, 0, lambda: distance_front_camera(distance_front_unparking, "pink"))
            await stop(True)
            await turn(SPEED_UNPARK, 10 * state.round_dir, 1.2)
            await stop(True)
            state.parking = "Start"
            await turn(-speed, -65 * state.round_dir, 1.2)
            await turn(-speed, 75 * state.round_dir, 1)
            await stop(True)
            state.position = "middle_parking"
            parking_drive_distance = 0
            await asyncio.sleep(0.5)
            driving_pos = pipeline.process_pillars(state, straight_sections)
            for i in range(2):
                if driving_pos[i] == inner_colour and state.position == "middle_parking":
                    await drive(fspeed, 50)
                    await double_turn(fspeed, -75 * state.round_dir, 1)
                    state.position = "inner"
            print(f"current position: {state.position}")
            # print(f"straight direction: {car.straight_direction}")
            if state.position == "inner":
                parking_drive_distance = -600
                await drive(fspeed, -300) # just to make sure that corner will be detected
                await stop(True)
                await follow_wall(speed, "inner")
                await drive(fspeed, 250)
                await double_turn(fspeed, 65 * state.round_dir, 1)
                state.position = "middle"
                await drive(fspeed, 0, lambda: distance_front_camera(DISTANCE_TO_WALL))
            else:
                await follow_wall(speed, state.position, lambda: distance_front_camera(0.4), False)
                await drive(fspeed, 0, lambda: distance_front_camera(DISTANCE_TO_WALL))
            print(f"straight direction: {car.straight_direction}")
            state.rounds += 1
            
            if car.distance + parking_drive_distance < 1200:
                state.parking_field_location = "back"
            else:
                state.parking_field_location = "front"
            print(f"Parking field location detected: {state.parking_field_location}")
            print(f"car distance: {car.distance}")

            # state.rounds = 13

            while state.rounds < 13:
                follow_wall_distance_extra = 0
                if state.rounds < 5:
                    await turn(-500, 70 * state.round_dir, 0.75)
                    car.straight_direction -= 20 * state.round_dir
                    await drive(-500, 400)
                    await write_serial("p\n")
                    await asyncio.sleep(0.2)
                    print(f"car.stalled = {car.stalled}")
                    await write_serial("m\n")
                    print("stalled and starting again")
                    car.stalled = False
                    await follow_wall(speed*0.6, "middle", (lambda start=car.distance: lambda: distance(450, start))(), False)
                    await stop(True)
                    driving_pos = pipeline.process_pillars(state, straight_sections)
                    last_driving_pos = state.position
                    if driving_pos[0] == inner_colour:
                        state.position = "inner"
                    elif straight_sections[state.rounds % 4].parking_lot:
                        state.position = "middle_parking"
                    else:
                        state.position = "outer"
                    direction = find_direction(state.round_dir, last_driving_pos, state.position)
                    await double_turn(fspeed, 59 * direction)
                    if direction == 0: # if we are driving through middle_parking
                        await follow_wall(speed, state.position, (lambda start=car.distance: lambda: distance(300, start))(), False if "middle" in state.position else True)
                if driving_pos[0] != driving_pos[1]:
                    if straight_sections[state.rounds % 4].parking_lot:
                        follow_wall_distance_extra += 50
                        state.position = "middle_parking" if state.position == "inner" else "inner"
                    else:
                        follow_wall_distance_extra += 250 # extra distance for the big "double turn"
                        state.position = "outer" if state.position == "inner" else "inner"
                await follow_wall(speed, state.position, (lambda start=car.distance: lambda: distance(follow_wall_distance_extra + 900, start))(), False if "middle" in state.position else True)
                if state.position == "inner":
                    await drive(fspeed, 0, (lambda start=car.distance: lambda: distance(200, start))())
                direction = find_direction(state.round_dir, state.position, "middle")
                state.rounds += 1

                if state.rounds == 13:
                    print(f"Starting parallel parking procedure")
                    if state.position == "inner":
                        await double_turn(speed, 60 * state.round_dir)
                    await drive(speed, 0, lambda: distance_front_camera(DISTANCE_TO_WALL))
                    await stop()
                    await turn(-speed, -70 * state.round_dir, 0.75)
                    car.straight_direction += 20 * state.round_dir
                    await stop()
                    await drive(speed, 0, lambda: distance_front_camera(0.37))
                    await turn(speed, 70 * state.round_dir, 1)
                    car.straight_direction += 20 * state.round_dir
                    await stop()
                    await drive(-speed, 450)
                    await stop()
                    state.round_dir *= -1
                    await follow_wall(150, "middle_parking_end", (lambda start=car.distance: lambda: distance(600, start))())
                    state.round_dir *= -1
                    await parking()
                    break
                    
                    print("Parking back into the parking spot...")
                    SPEED_PARK = 100

                    
                    if state.position == "middle_parking":
                        if state.parking_field_location == "front":
                            await drive(-speed, 500)
                        await turn(-SPEED_PARK, 90 * state.round_dir, 1)
                        await drive(-speed, 400)
                        await write_serial("p\n")
                        await asyncio.sleep(0.2)
                        
                        await write_serial("m\n")
                        # await turn(SPEED_PARK, 65 * state.round_dir, 1.2)
                        # await turn(-SPEED_PARK, -10 * state.round_dir, 1.2)

                    elif state.position == "inner":
                        await double_turn(-SPEED_PARK, 75 * state.round_dir, 1)
                        if state.parking_field_location == "back":
                            await drive(speed, 400)
                        await turn(-SPEED_PARK, 90 * state.round_dir, 1)
                        await drive(-speed, 400)
                        await write_serial("p\n")
                        await asyncio.sleep(0.2)
                        
                        await write_serial("m\n")
                        # await turn(SPEED_PARK, 65 * state.round_dir, 1.2)
                        # await turn(-SPEED_PARK, -10 * state.round_dir, 1.2)
                    else:
                        print(f"Unknown parking position: {state.position}")
                        
                    print("Parking completed.")
                    break

                if state.rounds == 5:
                    speed = fspeed
                    

                if state.rounds < 5:
                    await double_turn(fspeed, 59 * direction)
                    state.position = "middle"
                    await drive(fspeed, 0, lambda: distance_front_camera(DISTANCE_TO_WALL))
                    await stop(True)
                else:
                    # determine the next position to drive in the next section. This enables the robot to decide what turn to do to end up at the correct position in the next section
                    driving_pos = pipeline.process_pillars(state, straight_sections)
                    last_driving_pos = state.position
                    if driving_pos[0] == inner_colour:
                        state.position = "inner"
                    elif straight_sections[state.rounds % 4].parking_lot:
                        state.position = "middle_parking"
                    else:
                        state.position = "outer"

                    print(f"Final rounds (No. {state.rounds}), currently at {state.position}, driving_pos: {driving_pos}, last driving pos: {last_driving_pos}")
                    DISTANCE_FRONT_OUTSIDE_TURN = 0.6
                    if last_driving_pos == "inner":
                        if state.position == "inner":
                            # await drive(speed, 100)
                            await turn(speed, -90 * state.round_dir, 1)
                        elif state.position == "middle_parking":
                            await drive(speed, 300)
                            await turn(speed, -90 * state.round_dir, 1)
                        else:
                            await drive(speed, 0, lambda: distance_front_camera(DISTANCE_FRONT_OUTSIDE_TURN))
                            await turn(speed, -90 * state.round_dir, 1)
                    elif last_driving_pos == "outer":
                        if state.position == "inner":
                            await follow_wall(speed, "outer", lambda: blue_orange("orange" if state.round_dir < 0 else "blue"), False, car.straight_direction)
                            await turn(speed, -90 * state.round_dir, 1)
                            await double_turn(speed, -70 * state.round_dir, 1)
                            await drive(speed, 200)
                        elif state.position == "middle_parking":
                            await follow_wall(speed, "outer", lambda: blue_orange("orange" if state.round_dir < 0 else "blue"), False, car.straight_direction)
                            await drive(speed, 100)
                            await turn(speed, -90 * state.round_dir, 1)
                            await drive(speed, 600)
                        else:
                            await follow_wall(speed, "outer", lambda: distance_front_camera(DISTANCE_FRONT_OUTSIDE_TURN), False)
                            await turn(speed, -90 * state.round_dir, 1)
                            await follow_wall(speed, "outer", (lambda start=car.distance: lambda: distance(550, start))(), False)
                    elif last_driving_pos == "middle_parking":
                        if state.position == "inner":
                            await follow_wall(speed, "middle_parking", lambda: blue_orange("orange" if state.round_dir < 0 else "blue"), False, car.straight_direction)
                            await turn(speed, -90 * state.round_dir, 1)
                            await double_turn(speed, -50 * state.round_dir, 0.8)
                        elif state.position == "middle_parking": # never used
                            await drive(speed, 400)
                            await turn(speed, -90 * state.round_dir, 1)
                            await drive(speed, 300)
                        else:
                            await drive(speed, 0, lambda: distance_front_camera(DISTANCE_FRONT_OUTSIDE_TURN))
                            await turn(speed, -90 * state.round_dir, 1)
                            await follow_wall(speed, "outer", (lambda start=car.distance: lambda: distance(300, start))(), False)
                    # await asyncio.sleep(2)
            
    except Exception as e:
        print(f"Exception in main_program: {e}")
        ser.write(b's0\n')
        ser.write(b'p\n')
        # Cancel all running tasks and exit immediately
        for task in asyncio.all_tasks():
            task.cancel()
        await asyncio.sleep(0.1)  # Give tasks a moment to cancel
        os._exit(0)

    car.speed = 0
    await write_serial("o\n")
    await write_serial("p\n")
    print(f"Main program completed in {perf_counter() - run_time} s. Exiting...")
    await asyncio.sleep(0.5)
    if state.shutdown:
        print("Shutting down the robot...")
        if ser and not state.calibrate:
            ser.write(b's0\n')  # Stop steering
            ser.write(b'p\n')   # Stop driving
            print("Sent stop commands to robot via serial.")
        sleep(1)  # Give some time for the commands to be sent
        os.system("sudo shutdown now")  # Shutdown the Raspberry Pi
    else:
        print("Main program finished without shutdown. Robot is ready for next commands.")
        # stop the whole program
        os._exit(0)

if __name__ == "__main__":
    asyncio.run(main())