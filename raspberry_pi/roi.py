#!/usr/bin/env python3
import asyncio
import base64
import json
import math
import os
from time import sleep, time
import cv2
import numpy as np
import serial
import serial.tools.list_ports
from websockets import WebSocketServerProtocol, serve
from config import ConfigLoader
from helpers import Pillar, extract_ROI, print_past_time, Straight_Section, Lines, bound, setup_logging, Car, SharedState, process_pillars, find_direction
from pipeline import Pipeline
from picamera2 import Picamera2
from rounddir import find_round_dir
import argparse
import libcamera
import sys

#?: Webviewer controls for tuning colors, pd, and selecting stream

# todo: move most of this code to the main function

# turn the camera image by 180 degrees

configloader = ConfigLoader("config.json")
pipeline = Pipeline(configloader)

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

current_streams = ["viz", "black", "viz"]
has_sent_streams_info = False
active_websocket = None
# Shared dictionary to hold the latest streams from cycle()

car = Car()

# Create a single shared state object
state = SharedState()

# Helper buffer to keep recent angle samples with timestamps
from collections import deque
class AngleBuffer:
    def __init__(self, window_seconds: float = 1.0):
        self.window = window_seconds
        self.buf = deque()  # stores (timestamp, angle)

    def append(self, timestamp: float, angle: float):
        self.buf.append((timestamp, angle))
        self._trim(timestamp)

    def _trim(self, now: float):
        # remove samples older than window
        while self.buf and (now - self.buf[0][0]) > self.window:
            self.buf.popleft()

    def mean_and_mse(self):
        if not self.buf:
            return None, None
        angles = [a for _, a in self.buf]
        mean = sum(angles) / len(angles)
        mse = sum((a - mean) ** 2 for a in angles) / len(angles)
        return mean, mse

    def clear(self):
        self.buf.clear()

    def covers_full_window(self, now: float = None) -> bool:
        """Return True if the buffer currently contains samples that span at least self.window seconds."""
        if now is None:
            now = time()
        if not self.buf:
            return False
        return (now - self.buf[0][0]) >= self.window

# attach an angle buffer to the shared state
state.angle_buffer = AngleBuffer(window_seconds=3.0)

ports = serial.tools.list_ports.comports()

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

# Load the config file
with open("config.json", "r") as f:
    config = json.load(f)

# create a list with four elements of Straight_Section
straight_sections = [Straight_Section(i) for i in range(4)]

def pause_robot(resume=False):
    global car
    if resume:
        car.paused = False
        print("Robot resumed")
    else:
        car.paused = True
        print("Robot paused")

async def write_serial(msg):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, ser.write, msg.encode())
    
async def read_serial_line():
    loop = asyncio.get_running_loop()
    line = await loop.run_in_executor(None, ser.readline)
    return line.decode('utf-8').strip()

async def read_and_handle_serial_line():
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
            while car.paused and not state.skip_arduino:
                await read_and_handle_serial_line()
                await asyncio.sleep(0.1)
        print("Arduino connected and start signal received.")
    except Exception as e:
        print(f"Exception in connect_arduino: {e}")


async def arduino_communication() -> bool:
    try:
        if not car.paused: # currently a lag of about 30 ms to communicate to robot
            car.distance, car.angle = await request_and_parse_float(f"y{int(car.speed)},{int(car.steering)}", "gyro and distance: ")
        else:
            car.distance, car.angle = await request_and_parse_float("z", "gyro and distance: ")
        # print(f"Passed time: {await request_and_parse_float('x', 'x (passed time)')}")
                
    except Exception as e:
        print(f"Exception in arduino_communication: {e}")
        return False

    
async def arduino_communication_loop():
    # await asyncio.sleep(2)
    while car.paused: # and not state.skip_arduino:
        await asyncio.sleep(0.01)
    while True:
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
    img = await asyncio.to_thread(cv2.cvtColor, picam2.capture_array(), cv2.COLOR_RGB2BGR)
    color_image = await asyncio.to_thread(pipeline.crop, img)
    viz_stream = color_image.copy()
    hsv_image = await asyncio.to_thread(cv2.cvtColor, color_image, cv2.COLOR_BGR2HSV)

    state.update_stream("roi_center", await asyncio.to_thread(extract_ROI, hsv_image, [roi_center_x, roi_center_y], [roi_center_x + roi_center_w, roi_center_y + roi_center_h]))

    rgbl = await asyncio.to_thread(pipeline.filter_RG_Bl, hsv_image, color_image)

    roi_left_side = await asyncio.to_thread(extract_ROI, rgbl["black"], [0, 0], [roi_width, rgbl["black"].shape[0]])
    roi_right_side = await asyncio.to_thread(extract_ROI, rgbl["black"], [640-roi_width, 0], [640, rgbl["black"].shape[0]])

    roi_left = await asyncio.to_thread(extract_ROI, roi_left_side, [0, 0], [roi_width, roi_height])
    roi_right = await asyncio.to_thread(extract_ROI, roi_right_side, [0, 0], [roi_width, roi_height])

    portion_black_l = await asyncio.to_thread(cv2.countNonZero, roi_left) / (roi_left.shape[0] * roi_left.shape[1])
    portion_black_r = await asyncio.to_thread(cv2.countNonZero, roi_right) / (roi_right.shape[0] * roi_right.shape[1])

    orange_roi = await asyncio.to_thread(extract_ROI, rgbl["orange"], [roi_center_x, roi_center_y], [roi_center_x + roi_center_w, roi_center_y + roi_center_h])
    blue_roi = await asyncio.to_thread(extract_ROI, rgbl["blue"], [roi_center_x, roi_center_y], [roi_center_x + roi_center_w, roi_center_y + roi_center_h])
    state.portion_orange = await asyncio.to_thread(cv2.countNonZero, orange_roi) / (orange_roi.shape[0] * orange_roi.shape[1])
    state.portion_blue = await asyncio.to_thread(cv2.countNonZero, blue_roi) / (blue_roi.shape[0] * blue_roi.shape[1])

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
    roi_lines = {"L": [], "R": []}
    for image, key in zip([state.latest_streams["roi_left"], state.latest_streams["roi_right"]], ["L", "R"]):
        # remove the 5 uppest rows from the image just in case the robot sees over the barriers
        image = image[5:, :]
        blurredImg = await asyncio.to_thread(cv2.GaussianBlur, image, (3, 3), 0)
        lower = 30
        upper = 90
        edges_img = await asyncio.to_thread(cv2.Canny, blurredImg, lower, upper, 3)
        roi_lines[key] = await asyncio.to_thread(cv2.HoughLinesP, edges_img, 1, np.pi/180, 10, minLineLength=25, maxLineGap=50)
    state.border_lines = {"L": Lines(roi_lines["L"], (0, 5), (0, 0)), "R": Lines(roi_lines["R"], (640-roi_width, 5), (roi_width, 0))}

    for side_letter in ["L", "R"]:
        lines = state.border_lines[side_letter].lines
        # calculate the slope and intercept of the line
        state.detected_corners[side_letter] = None
        for line in lines:
            # detect if any line forms a corner with the first line
            max_diff = 400 # max difference squared of the two points to be the same point
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
            cv2.circle(viz_stream, (corner_x, state.detected_corners[side_letter][1]), 5, (255, 255 if state.detected_corners[side_letter][3] == "different" else 100, 0), -1)
            cv2.putText(viz_stream, f"{state.detected_corners[side_letter][0]} {state.detected_corners[side_letter][1]}", (corner_x, roi_height), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)

    roi_front_width = 10
    roi_front = extract_ROI(state.latest_streams["black"], [640//2-roi_front_width, 0], [640//2+roi_front_width, 140])
    portion_black_front = cv2.countNonZero(roi_front) / (roi_front.shape[0] * roi_front.shape[1])
    state.distance_front = portion_black_front

    # visualisation
    if not state.headless:
        for line_group in state.border_lines.values():
            if line_group is not None:
                b = 200
                for line in line_group.lines:
                    cv2.line(viz_stream, (line["x1"] + line["x_offset"], line["y1"] + line["y_offset"]), 
                        (line["x2"] + line["x_offset"], line["y2"] + line["y_offset"]), (b, 100, 0), 2)
                    cv2.putText(viz_stream, f"{line['x1'] + line['x_offset']} {line['y1'] + line['y_offset']} {line['x2'] + line['x_offset']} {line['y2'] + line['y_offset']}", 
                        (line["x1"] + line["x_offset"], line["y1"] + line["y_offset"]), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
                    # create a green dot for x1 y1 and a red dot for x2 y2
                    cv2.circle(viz_stream, (line["x1"] + line["x_offset"], line["y1"] + line["y_offset"]), 3, (0, 255, 0), -1)
                    cv2.circle(viz_stream, (line["x2"] + line["x_offset"], line["y2"] + line["y_offset"]), 3, (0, 0, 255), -1)
                    if b == 200:
                        cv2.putText(viz_stream, f"y={line['m']:.2f}x+{line['b']:.2f}", 
                            (line["x1"] + line["x_offset"], 200 + line["y_offset"]), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
                    b *= 0.6
    return viz_stream


async def cycle():
    viz_stream = await process_image(picam2, pipeline)

    roi_front_width = 10
    roi_front = extract_ROI(state.latest_streams["black"], [640//2-roi_front_width, 0], [640//2+roi_front_width, 140])
    portion_black_front = cv2.countNonZero(roi_front) / (roi_front.shape[0] * roi_front.shape[1])
    if not state.headless:
        cv2.rectangle(viz_stream, (640//2-roi_front_width, 0), (640//2+roi_front_width, 140), (255, 0, 0), 3)
        cv2.putText(viz_stream, f"{portion_black_front:.2f}", (300, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz_stream, f"{state.portion_black_l:.2f}", (110, 130), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz_stream, f"{state.portion_black_r:.2f}", (510, 130), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz_stream, f"o: {state.portion_orange:.2f}", (300, 210), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz_stream, f"b: {state.portion_blue:.2f}", (300, 230), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)

    if state.pillars:
        viz_stream = await detect_edge_lines(state, roi_width, viz_stream)

        distance_front = portion_black_front
        # sm.distance_front = portion_black_front

        # filter out the red and green colors of the pillars and walls
        detected_pillars_r = pipeline.get_pillars(state.latest_streams["red"], "RED")
        detected_pillars_g = pipeline.get_pillars(state.latest_streams["green"], "GREEN")
        state.detected_pillars = detected_pillars_r + detected_pillars_g

        state.detected_pillars.sort(key=lambda x: x.width*x.height, reverse=True)
        
        for i in ["L", "R"]: # todo use this information
            lines = state.border_lines[i].lines
            # calculate the slope and intercept of the line
            detected_corner = None
            for line in lines:
                # detect if any line forms a corner with the first line
                max_diff = 400 # max difference squared of the two points to be the same point
                different_slopes = line["m"] != 0 and lines[0]["m"] != 0 and line["m"]/abs(line["m"]) != lines[0]["m"]/abs(lines[0]["m"])
                if (line["x2"] - lines[0]["x1"]) ** 2 + (line["y2"] - lines[0]["y1"]) ** 2 < max_diff and different_slopes:
                    detected_corner = ((line["x2"] + lines[0]["x1"]) // 2, (line["y2"] + lines[0]["y1"]) // 2, lines.index(line), "different")
                    break
                elif (line["x1"] - lines[0]["x2"]) ** 2 + (line["y1"] - lines[0]["y2"]) ** 2 < max_diff and different_slopes:
                    detected_corner = ((line["x1"] + lines[0]["x2"]) // 2, (line["y1"] + lines[0]["y2"]) // 2, lines.index(line), "different")
                    break
                elif (line["x2"] - lines[0]["x2"]) ** 2 + (line["y2"] - lines[0]["y2"]) ** 2 < max_diff and different_slopes:
                    detected_corner = ((line["x2"] + lines[0]["x2"]) // 2, (line["y2"] + lines[0]["y2"]) // 2, lines.index(line), "same")
                    break
                elif (line["x1"] - lines[0]["x1"]) ** 2 + (line["y1"] - lines[0]["y1"]) ** 2 < max_diff and different_slopes:
                    detected_corner = ((line["x1"] + lines[0]["x1"]) // 2, (line["y1"] + lines[0]["y1"]) // 2, lines.index(line), "same")
                    break
            if detected_corner != None:
                # if not headless:
                # Determine the correct x position for the detected corner based on round_dir
                if i == "L":
                    corner_x = detected_corner[0]
                else:
                    corner_x = 640 + detected_corner[0]
                if not state.headless:
                    cv2.circle(viz_stream, (corner_x, detected_corner[1]), 5, (255, 255 if detected_corner[3] == "different" else 100, 0), -1)
                    cv2.putText(viz_stream, f"{detected_corner[0]} {detected_corner[1]}", (corner_x, roi_height), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)


    # viz stuff
    if not state.headless:
        cv2.rectangle(viz_stream, (roi_center_x, roi_center_y), (roi_center_x + roi_center_w, roi_center_y + roi_center_h), (0, 255, 0), 2)
        cv2.rectangle(viz_stream, (0, 0), (roi_width, 150), (255, 0, 0), 2)
        cv2.rectangle(viz_stream, (640-roi_width, 0), (640, 150), (255, 0, 0), 2)
        cv2.putText(viz_stream, f"Speed: {car.speed} mm/s, Steering: {car.steering:.2f}", (10, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1) # change to current function that is running
        cv2.putText(viz_stream, f"Angle: {car.angle:.2f} deg, Distance: {car.distance:.2f} mm", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz_stream, f"Direction: {state.round_dir}, Rounds: {state.rounds}, Position: {state.position}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz_stream, f"Current function: {state.current_function}", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        # cv2.putText(viz_stream, f"{12 - sm.turns_left} / 12", (580, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        if state.pillars:
            for p in state.detected_pillars:
                cv2.line(viz_stream, (p.screen_x, 0), (p.screen_x, 480), (0, 0, 255) if p.color == "RED" else (0, 255, 0), 2)
    state.latest_streams["viz"] = viz_stream

async def cycle_loop():
    """
    Main loop that continuously reads data from the Arduino, processes images, and applies control logic.

    This function captures images from the camera, processes them to extract relevant features,
    and applies a PD control algorithm to drive the robot. It also handles state transitions
    based on the robot's current state and sensor readings.

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
        await asyncio.sleep(0.02)  # Sleep for a short duration to prevent blocking

async def img_stream(websocket, path):
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
                    return await asyncio.to_thread(encode_image, val)
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
    async with serve(img_stream, "0.0.0.0", 8765):
        print("Webserver started on ws://0.0.0.0:8765")
        await asyncio.Future()  # Run forever

async def main():
    parser = argparse.ArgumentParser(description="Check if --headless flag was given.")
    parser.add_argument('--headless', action='store_true', help='Run in headless mode')
    parser.add_argument('--pillars', action='store_true', help='Run in pillar mode')
    parser.add_argument('--shutdown', action='store_true', help='Shutdown after run')
    parser.add_argument('--calibrate', action='store_true', help='Disable driving and moving to next states')
    parser.add_argument('--skip-arduino', action='store_true', help='Skip Arduino connection')
    args = parser.parse_args()

    state.set_flags(
        headless=args.headless,
        pillars=args.pillars,
        shutdown=args.shutdown,
        calibrate=args.calibrate,
        skip_arduino=args.skip_arduino
    )

    state.kp = configloader.get_property("PD")['kp']
    state.kd = configloader.get_property("PD")['kd']
    
    setup_logging() # todo reactivate
    
    # connect_arduino is now called at the start of main_program

    tasks = [asyncio.create_task(cycle_loop()),
             asyncio.create_task(arduino_communication_loop())]
    if not state.headless:
        tasks.append(asyncio.create_task(run_webserver()))
    if not state.calibrate:
        tasks.append(asyncio.create_task(main_program()))

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

def encode_image(image):
    retval, buffer = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 99])
    base64_str = base64.b64encode(buffer).decode('utf-8')
    return base64_str

MAX_STEERING_ANGLE = 25.0

async def calculate_steering(error) -> float:
    correction = error * state.kp + (error - state.last_error) * state.kd
    state.last_error = error
    return bound(correction) * MAX_STEERING_ANGLE

def current_function(func):
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
async def turn(speed, degrees, steering):
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
    await turn(speed, angle, steering)
    await turn(speed, -angle, steering)
    car.speed = 0
    
REF_PORTION = 0.45 if not state.pillars else 0.30
REF_PORTION_SIDE = 0.8

@current_function
async def pd_middle(speed: int, side: str, stop_condition: callable):
    car.speed = speed
    car.finished = False
    while True: # todo find out how to determine when to finish
        if side == "R":
            error = (REF_PORTION - state.portion_black_r)
        elif side == "L":
            error = (state.portion_black_l - REF_PORTION)
        else:
            raise ValueError(f"side must be 'L' or 'R', currently it is set to '{side}'")
        if stop_condition():
            break

        car.steering = await calculate_steering(error*1.2)
        await asyncio.sleep(0.01)
        
@current_function
async def follow_wall(speed: int, side: str = state.position, stop_condition: callable = lambda: False, wait_for_corner = True, angle_beg: float = car.straight_direction):
    """ follows the wall until an a corner is detected  or the stop_condition is satisfied"""
    car.speed = speed
    error = 0
    roi_side = "L" if state.round_dir * (1 if side == "inner" else -1) == 1 else "R"
    # start-time for this follow_wall invocation and reset buffer
    start_time = time()
    try:
        state.angle_buffer.clear()
    except Exception:
        pass
    
    updated_mean_angle = None
    updated_mse = None

    while True:
        diff_angle = car.angle - angle_beg
        # collect angle samples for stability check (mean + MSE)
        try:
            now = time()
            state.angle_buffer.append(now, car.angle)
            mean_angle, mse = state.angle_buffer.mean_and_mse()
            if mean_angle is not None and mse is not None and (now - start_time) >= state.angle_buffer.window:
                # only change straight_direction when the buffer spans the configured window
                if mse < 20 and abs(mean_angle - car.straight_direction) < 10:
                    # print(f"Updating straight direction to {mean_angle:.2f} deg (MSE: {mse:.2f})")
                    car.straight_direction = mean_angle
                    updated_mean_angle = mean_angle
                    updated_mse = mse
                elif mse < 20:
                    pass
                    # print(f"Not updating straight direction (MSE: {mse:.2f}), because mean angle deviation is {abs(mean_angle - car.straight_direction):.2f} deg")
        except Exception:
            # be defensive: do not break the control loop on buffer errors
            # keep silent in production, but print for debugging
            print("Angle buffer error")
            pass
        detected_corner = state.detected_corners[roi_side]
        lines = state.border_lines[roi_side].lines
        if len(lines) > 0:
            slope = lines[0]["m"]
            intercept = lines[0]["b"]
            if side == "inner":
                # if we detected a corner, we should start gyro following
                if detected_corner != None and wait_for_corner:
                    slope_2 = lines[detected_corner[2]]["m"]
                    if (abs(slope) > 3 or abs(slope_2) > 3) and detected_corner[1] > 100 and detected_corner[3] == "same":
                        # if we reach the end of the wall
                        # sm.following_angle = True
                        cv2.imwrite(f"logs/image_corner_{state.rounds}.jpg", state.latest_streams["viz"])
                        # error = (sm.diff_angle / 80) ** 2
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
                error = (intercept - 80) / 250 * -state.round_dir
                if diff_angle != 0:
                    error = bound(error) - (diff_angle / 50) ** 2 * diff_angle / abs(diff_angle)
            elif side == "middle":
                if detected_corner != None and wait_for_corner:
                    slope_2 = lines[detected_corner[2]]["m"]
                    if detected_corner[1] > 10:
                        cv2.imwrite(f"logs/image_corner_{state.rounds}.jpg", state.latest_streams["viz"])
                        break
                error = (intercept - 53) / 100 * -state.round_dir
            else:
                raise ValueError(f"side must be 'inner', 'outer' or 'middle', currently it is set to '{side}'")
        else:
            if roi_side == "L":
                error = state.portion_black_l - REF_PORTION_SIDE
            else:
                error = REF_PORTION_SIDE - state.portion_black_r
        if stop_condition():
            break

        car.steering = await calculate_steering(error)            
        await asyncio.sleep(0.01)
    if (updated_mean_angle is not None and updated_mse is not None):
        print(f"Updated straight direction to {updated_mean_angle:.2f} deg (MSE: {updated_mse:.2f})")

@current_function
async def stop(directly = False):
    car.speed = 0
    if directly:
        await write_serial("m\n")
        await asyncio.sleep(0.1)
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

def distance_front_camera(percentage: float) -> bool:
    if state.distance_front > percentage:
        return True
    else:
        return False

def distance(distance: float, distance_beg: float) -> bool:
    if car.distance - distance_beg < distance:
        return False
    return True

DISTANCE_TO_WALL = 0.95  # Distance to the wall for state transition

async def main_program():
    # Wait for Arduino trigger before starting main logic
    if ser and not state.calibrate:
        await connect_to_arduino()
    speed = 300 if not state.pillars else 200
    print("Starting main program...")
    run_time = time()
    
    # state.round_dir = -1
    # # Parking: 
    # SPEED_PARK = 100
    # print("Start parking...")
    # await turn(-SPEED_PARK, -80 * state.round_dir, 1)
    # await turn(SPEED_PARK, 65 * state.round_dir, 1.2)
    # await turn(SPEED_PARK, -10 * state.round_dir, 1.2)
    # print("parking completet.")
    # await asyncio.sleep(100)
    # await stop()
    # return
    
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
            # todo: reset angle more often
            car.straight_direction = car.angle
            print(f"Initial straight direction: {car.straight_direction}")
            SPEED_UNPARK = 100
            await turn(SPEED_UNPARK, 10 * state.round_dir, 1.2)
            await turn(-SPEED_UNPARK, -65 * state.round_dir, 1.2)
            # await drive(speed, 10, )
            await turn(-SPEED_UNPARK, 75 * state.round_dir, 1)
            state.position = "middle_parking"
            driving_pos = process_pillars(state, straight_sections) # todo take a new picture (make sure no motion blur)
            for i in range(2):
                if driving_pos[i] == inner_colour and state.position == "middle_parking":
                    await drive(speed, 50)
                    await double_turn(speed, -75 * state.round_dir, 1)
                    state.position = "inner"
            print(f"current position: {state.position}")
            # print(f"straight direction: {car.straight_direction}")
            if state.position == "inner":
                await drive(speed, -250) # just to make sure that corner will be detected
                await follow_wall(speed, "inner")
                await drive(speed, 250)
                await double_turn(speed, 65 * state.round_dir, 1)
                state.position = "middle"
                await drive(speed, 0, lambda: distance_front_camera(DISTANCE_TO_WALL))
            else:
                await follow_wall(speed, state.position, lambda: distance_front_camera(0.4), False) # todo: move the middle part a bit more to the wall -> should work
                await drive(speed, 0, lambda: distance_front_camera(DISTANCE_TO_WALL))
            print(f"straight direction: {car.straight_direction}")
            state.rounds += 1
            
            if car.distance < 1400:
                state.parking_field_location = "back"
            else:
                state.parking_field_location = "front"
            print(f"Parking field location detected: {state.parking_field_location}")
            print(f"car distance: {car.distance}")

            # state.rounds = 13

            while state.rounds < 13:
                # print(f"straight direction: {car.straight_direction}")
                # car.straight_direction = car.angle # todo test if this is the correct position to reset the angle
                if state.rounds < 5:
                    await turn(-500, 70 * state.round_dir, 0.75)
                    car.straight_direction -= 20 * state.round_dir
                    await drive(-speed, 400)
                    await write_serial("p\n")
                    await asyncio.sleep(0.2)
                    print(f"car.stalled = {car.stalled}")
                    await write_serial("m\n")
                    print("stalled and starting again")
                    car.stalled = False
                    await follow_wall(speed*0.6, "middle", (lambda start=car.distance: lambda: distance(450, start))(), False)
                    await stop(True)
                    # car.straight_direction = car.angle # todo: improve the resetting of the angle to not drift too much
                    driving_pos = process_pillars(state, straight_sections)
                    last_driving_pos = state.position
                    if driving_pos[0] == inner_colour:
                        state.position = "inner"
                    elif straight_sections[state.rounds % 4].parking_lot:
                        state.position = "middle_parking"
                    else:
                        state.position = "outer"
                    direction = find_direction(state.round_dir, last_driving_pos, state.position)
                    await double_turn(speed, 60 * direction)
                    if direction == 0:
                        await follow_wall(speed, state.position, (lambda start=car.distance: lambda: distance(300, start))(), False if "middle" in state.position else True, car.straight_direction)
                if driving_pos[0] != driving_pos[1]:
                    if straight_sections[state.rounds % 4].parking_lot:
                        state.position = "middle_parking" if state.position == "inner" else "inner"
                    else:
                        state.position = "outer" if state.position == "inner" else "inner"
                follow_wall_distance = 900 + (driving_pos[0] != driving_pos[1]) * 250 + (state.position == "middle_parking") * 400 + (last_driving_pos == "middle_parking") * 50 # check the distance for middle_parking, they seem to be wrong
                await follow_wall(speed, state.position, (lambda start=car.distance: lambda: distance(follow_wall_distance, start))(), False if "middle" in state.position else True, car.straight_direction)
                if state.position == "inner":
                    await drive(speed, 0, (lambda start=car.distance: lambda: distance(200, start))())
                # await stop(True)
                # await asyncio.sleep(2)
                direction = find_direction(state.round_dir, state.position, "middle")
                state.rounds += 1

                if state.rounds == 13:
                    
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
                    speed *= 1.25

                if state.rounds < 5:
                    await double_turn(speed, 60 * direction)
                    state.position = "middle"
                    await drive(speed, 0, lambda: distance_front_camera(DISTANCE_TO_WALL))
                else:
                    # todo start
                    # determine the next position to drive in the next section. This enables the robot to decide what turn to do to end up at the correct position in the next section
                    driving_pos = process_pillars(state, straight_sections)
                    last_driving_pos = state.position
                    if driving_pos[0] == inner_colour:
                        state.position = "inner"
                    elif straight_sections[state.rounds % 4].parking_lot:
                        state.position = "middle_parking"
                    else:
                        state.position = "outer"

                    # todo end
                    print(f"Final rounds (No. {state.rounds}), currently at {state.position}, driving_pos: {driving_pos}, last driving pos: {last_driving_pos}")
                    DISTANCE_FRONT_OUTSIDE_TURN = 0.6
                    if last_driving_pos == "inner":
                        if state.position == "inner":
                            await drive(speed, 100)
                            await turn(speed, -90 * state.round_dir, 1)
                        elif state.position == "middle_parking":
                            await drive(speed, 400)
                            await turn(speed, -90 * state.round_dir, 1)
                        else:
                            await drive(speed, 0, lambda: distance_front_camera(DISTANCE_FRONT_OUTSIDE_TURN))
                            await turn(speed, -90 * state.round_dir, 1)
                    elif last_driving_pos == "outer":
                        if state.position == "inner":
                            await follow_wall(speed, "outer", lambda: blue_orange("orange" if state.round_dir < 0 else "blue"), False, car.straight_direction) # todo fix error that the robot turns right when driving ccw
                            await turn(speed, -90 * state.round_dir, 1)
                            await double_turn(speed, -70 * state.round_dir, 1)
                            await drive(speed, 200)
                        elif state.position == "middle_parking":
                            await drive(speed, 450) # 350 for middle
                            await turn(speed, -90 * state.round_dir, 1)
                            await drive(speed, 450)
                        else:
                            await follow_wall(speed, "outer", lambda: distance_front_camera(DISTANCE_FRONT_OUTSIDE_TURN), False)
                            await turn(speed, -90 * state.round_dir, 1)
                            await follow_wall(speed, "outer", (lambda start=car.distance: lambda: distance(550, start))(), False)
                    elif last_driving_pos == "middle_parking":
                        if state.position == "inner":
                            await drive(speed, 100)
                            await turn(speed, -90 * state.round_dir, 1)
                            await drive(speed, 300)
                        elif state.position == "middle_parking":
                            await drive(speed, 400)
                            await turn(speed, -90 * state.round_dir, 1)
                            await drive(speed, 300)
                        else:
                            await drive(speed, 0, lambda: distance_front_camera(DISTANCE_FRONT_OUTSIDE_TURN))
                            await turn(speed, -90 * state.round_dir, 1)
                            await follow_wall(speed, "outer", (lambda start=car.distance: lambda: distance(300, start))(), False)
                    # await asyncio.sleep(2)
                    # car.straight_direction = car.angle

                    # await turn(speed, -90 * state.round_dir, 1)
            
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
    print(f"Main program completed in {time() - run_time} s. Exiting...")
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