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
from helpers import Pillar, extract_ROI, print_past_time, Straight_Section, Lines, bound, setup_logging, Car
from pipeline import Pipeline
from statemachine import StateMachine
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

roi_center_w, roi_center_h = 100, 50
roi_center_x, roi_center_y = 320 - roi_center_w // 2, 180

roi_width = 100
roi_height = 150

current_streams = ["viz", "black", "viz"]
has_sent_streams_info = False
active_websocket = None
# Shared dictionary to hold the latest streams from cycle()
latest_streams = {}

car = Car()

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

last_error = 0.0

# create a list with four elements of Straight_Section
straight_sections = [Straight_Section(i) for i in range(4)]

kp = configloader.get_property("PD")['kp']
kd = configloader.get_property("PD")['kd']

car_paused = True

def pause_robot(resume=False):
    global car_paused
    if resume:
        car_paused = False
        print("Robot resumed")
    else:
        car_paused = True
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
        print("Gyro initialized successfully")
        await write_serial("o\n")
        while car_paused:
            await read_and_handle_serial_line()
            await asyncio.sleep(0.1)
        print("Arduino connected and start signal received.")
    except Exception as e:
        print(f"Exception in connect_arduino: {e}")


async def arduino_communication() -> bool:
    try:
        if not car_paused: # currently a lag of about 30 ms to communicate to robot
            car.distance, car.angle = await request_and_parse_float(f"y{int(car.speed)},{int(car.steering)}", "gyro and distance: ")
        else:
            car.distance, car.angle = await request_and_parse_float(f"z", "gyro and distance: ")
        # print(f"Passed time: {await request_and_parse_float('x', 'x (passed time)')}")
                
    except Exception as e:
        print(f"Exception in arduino_communication: {e}")
        return False

    
async def arduino_communication_loop():
    while car_paused:
        await asyncio.sleep(0.01)
    while True:
        await asyncio.sleep(0.001)
        arduino_ok = await arduino_communication()
        if arduino_ok is False:
            # Optionally, handle error state here (e.g., skip processing, log, etc.)
            print("Arduino communication failed, skipping processing.")



def process_image(picam2, pipeline):
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
    img = cv2.cvtColor(picam2.capture_array(), cv2.COLOR_RGB2BGR) # 5-10ms
    color_image = pipeline.crop(img)
    viz = color_image.copy()
    hsv_image = cv2.cvtColor(color_image, cv2.COLOR_BGR2HSV)

    roi_center = extract_ROI(hsv_image, [roi_center_x, roi_center_y], [roi_center_x + roi_center_w, roi_center_y + roi_center_h])

    rgbl = pipeline.filter_RG_Bl(hsv_image, color_image)

    roi_left_side = extract_ROI(rgbl["black"], [0, 0], [roi_width, rgbl["black"].shape[0]])
    roi_right_side = extract_ROI(rgbl["black"], [640-roi_width, 0], [640, rgbl["black"].shape[0]])

    roi_left = extract_ROI(roi_left_side, [0, 0], [roi_width, roi_height])
    roi_right = extract_ROI(roi_right_side, [0, 0], [roi_width, roi_height])

    portion_black_l = cv2.countNonZero(roi_left) / (roi_left.shape[0] * roi_left.shape[1])
    portion_black_r = cv2.countNonZero(roi_right) / (roi_right.shape[0] * roi_right.shape[1])

    return {
        "color_image": color_image,
        "viz": viz,
        "hsv_image": hsv_image,
        "roi_center": roi_center,
        "rgbl": rgbl,
        "roi_left_side": roi_left_side,
        "roi_right_side": roi_right_side,
        "roi_left": roi_left,
        "roi_right": roi_right,
        "portion_black_l": portion_black_l,
        "portion_black_r": portion_black_r
    }

def detect_edge_lines(roi_left_side, roi_right_side, roi_width, headless, viz) -> dict:
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
    for image, key in zip([roi_left_side, roi_right_side], ["L", "R"]):
        # remove the 5 uppest rows from the image just in case the robot sees over the barriers
        image = image[5:, :]
        blurredImg = cv2.GaussianBlur(image, (3, 3), 0)
        lower = 30
        upper = 90
        edges_img = cv2.Canny(blurredImg, lower, upper, 3)
        roi_lines[key] = cv2.HoughLinesP(edges_img, 1, np.pi/180, 10, minLineLength=25, maxLineGap=50)
    border_lines = {"L": Lines(roi_lines["L"], (0, 5), (0, 0)), "R": Lines(roi_lines["R"], (640-roi_width, 5), (roi_width, 0))}

    if not headless:
        for line_group in border_lines.values():
            if line_group is not None:
                b = 200
                for line in line_group.lines:
                    cv2.line(viz, (line["x1"] + line["x_offset"], line["y1"] + line["y_offset"]), 
                        (line["x2"] + line["x_offset"], line["y2"] + line["y_offset"]), (b, 100, 0), 2)
                    cv2.putText(viz, f"{line['x1'] + line['x_offset']} {line['y1'] + line['y_offset']} {line['x2'] + line['x_offset']} {line['y2'] + line['y_offset']}", 
                        (line["x1"] + line["x_offset"], line["y1"] + line["y_offset"]), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
                    # create a green dot for x1 y1 and a red dot for x2 y2
                    cv2.circle(viz, (line["x1"] + line["x_offset"], line["y1"] + line["y_offset"]), 3, (0, 255, 0), -1)
                    cv2.circle(viz, (line["x2"] + line["x_offset"], line["y2"] + line["y_offset"]), 3, (0, 0, 255), -1)
                    if b == 200:
                        cv2.putText(viz, f"y={line['m']:.2f}x+{line['b']:.2f}", 
                            (line["x1"] + line["x_offset"], 200 + line["y_offset"]), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
                    b *= 0.6
    return border_lines



def cycle():
    global last_error, straight_sections, angle_following, latest_streams

    processed_images = process_image(picam2, pipeline)

    # todo: some of these variables can be removed
    color_image = processed_images["color_image"]
    viz = processed_images["viz"]
    hsv_image = processed_images["hsv_image"]
    roi_center = processed_images["roi_center"]
    rgbl = processed_images["rgbl"]
    roi_left_side = processed_images["roi_left_side"]
    roi_right_side = processed_images["roi_right_side"]
    roi_left = processed_images["roi_left"]
    roi_right = processed_images["roi_right"]
    portion_black_l = processed_images["portion_black_l"]
    portion_black_r = processed_images["portion_black_r"]

    if pillars:
        border_lines = detect_edge_lines(roi_left_side, roi_right_side, roi_width, headless, viz)

        roi_front_width = 10
        roi_front = extract_ROI(rgbl["black"], [640//2-roi_front_width, 0], [640//2+roi_front_width, 140])
        portion_black_front = cv2.countNonZero(roi_front) / (roi_front.shape[0] * roi_front.shape[1])
        distance_front = portion_black_front
        # sm.distance_front = portion_black_front
        if not headless:
            cv2.rectangle(viz, (640//2-roi_front_width, 0), (640//2+roi_front_width, 140), (255, 0, 0), 3)
            cv2.putText(viz, f"{portion_black_front:.2f}", (300, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            cv2.putText(viz, f"{portion_black_l:.2f}", (110, 130), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            cv2.putText(viz, f"{portion_black_r:.2f}", (510, 130), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            # cv2.putText(viz, f"o: {portion_orange:.2f}", (300, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
            # cv2.putText(viz, f"b: {portion_blue:.2f}", (300, 210), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)

        # filter out the red and green colors of the pillars and walls
        detected_pillars_r = pipeline.get_pillars(rgbl["red"], "RED")
        detected_pillars_g = pipeline.get_pillars(rgbl["green"], "GREEN")
        detected_pillars = detected_pillars_r + detected_pillars_g

        detected_pillars.sort(key=lambda x: x.width*x.height, reverse=True)
        
        for i in ["L", "R"]: # todo use this information
            lines = border_lines[i].lines
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
                cv2.circle(viz, (corner_x, detected_corner[1]), 5, (255, 255 if detected_corner[3] == "different" else 100, 0), -1)
                cv2.putText(viz, f"{detected_corner[0]} {detected_corner[1]}", (corner_x, roi_height), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)


    # A state machine is used to model the car's behavior
    # This checks if the car should transition to a new state, and if so, transitions
    # states may be PD-CENTER, PD-RIGHT, PD-LEFT, TURNING-L, TURNING-R, etc.
    if not calibrate:
        if not pillars:
            orange_roi = extract_ROI(rgbl["orange"], [roi_center_x, roi_center_y], [roi_center_x + roi_center_w, roi_center_y + roi_center_h])
            blue_roi = extract_ROI(rgbl["blue"], [roi_center_x, roi_center_y], [roi_center_x + roi_center_w, roi_center_y + roi_center_h])
            portion_orange = cv2.countNonZero(orange_roi) / (orange_roi.shape[0] * orange_roi.shape[1])
            portion_blue = cv2.countNonZero(blue_roi) / (blue_roi.shape[0] * blue_roi.shape[1])


    correction = 0
    error = 0
    # viz stuff
    if not headless:
        cv2.rectangle(viz, (roi_center_x, roi_center_y), (roi_center_x + roi_center_w, roi_center_y + roi_center_h), (0, 255, 0), 2)
        cv2.rectangle(viz, (0, 0), (roi_width, 150), (255, 0, 0), 2)
        cv2.rectangle(viz, (640-roi_width, 0), (640, 150), (255, 0, 0), 2)
        cv2.putText(viz, f"Angle: {car.angle:.2f}°, Distance: {car.distance:.2f} mm", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz, f"Speed: {car.speed} mm/s, Steering: {car.steering:.2f}", (10, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1) # change to current function that is running
        # cv2.putText(viz, f"direction: {sm.round_dir}, gyro: {sm.following_angle}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        cv2.putText(viz, f"Correction: {round(correction, 2)}", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        # cv2.putText(viz, f"{12 - sm.turns_left} / 12", (580, 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1)
        if pillars:
            for p in detected_pillars:
                cv2.line(viz, (p.screen_x, 0), (p.screen_x, 480), (0, 0, 255) if p.color == "RED" else (0, 255, 0), 2)    

    last_error = error
    latest_streams = {
        "viz": viz,
        "roi_left": roi_left,
        "roi_right": roi_right,
        "roi_center": roi_center,
        "red": rgbl["red"],
        "green": rgbl["green"],
        "black": rgbl["black"],
        "orange": rgbl["orange"],
        "blue": rgbl["blue"],
        "hsv_image": hsv_image,
        "color_image": color_image,
        # "lines": pipeline.filter_OB(hsv_image)['orange'],
        # "parking": pipeline.filter_parking(hsv_image)
    }
    return latest_streams


async def cycle_loop():
    """
    Main loop that continuously reads data from the Arduino, processes images, and applies control logic.

    This function captures images from the camera, processes them to extract relevant features,
    and applies a PD control algorithm to drive the robot. It also handles state transitions
    based on the robot's current state and sensor readings.

    Returns:
        None
    """
    global last_error, has_sent_streams_info
    last_error = 0.0
    has_sent_streams_info = False

    while True:
        cycle()
        await asyncio.sleep(0.05)  # Sleep for a short duration to prevent blocking

async def img_stream(websocket, path):
    # todo: make sure old webserver is closed properly before starting a new one
    global current_streams, has_sent_streams_info, latest_streams, active_websocket
    print("Websocket connection established")
    # If there is already an active websocket, close it
    if active_websocket is not None and not active_websocket.closed:
        print("Closing previous websocket connection...")
        await active_websocket.close()
    active_websocket = websocket
    # Send initial stream info
    if not has_sent_streams_info:
        has_sent_streams_info = True
        await websocket.send(json.dumps({
            "streams": list(latest_streams.keys()),
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

                    # Save the updated config back to the file
                    with open("config.json", "w") as f:
                        json.dump(config, f, indent=2)
                current_streams[0] = res["streamA"]
                current_streams[1] = res["streamB"]
                current_streams[2] = res["streamC"]
                # print_past_time("received streams")
            except:
                pass

            # Only send if changed
            data = {}
            for idx, stream_name in enumerate(current_streams):
                value = latest_streams.get(stream_name)
                if value is not None:
                    # Use encode_image for images, else send as is
                    if isinstance(value, np.ndarray):
                        encoded = encode_image(value)
                    else:
                        encoded = value
                    data[chr(ord('a') + idx)] = encoded
            await websocket.send(json.dumps(data))
            await asyncio.sleep(0.1)
            # print_past_time("sent images")
    finally:
        # If this websocket is the active one, clear it on disconnect
        if active_websocket == websocket:
            active_websocket = None


async def run_webserver():
    async with serve(img_stream, "0.0.0.0", 8765):
        print("Webserver started on ws://0.0.0.0:8765")
        await asyncio.Future()  # Run forever

async def main():
    global headless, pillars, shutdown, calibrate, skip_arduino, ser
    parser = argparse.ArgumentParser(description="Check if --headless flag was given.")
    parser.add_argument('--headless', action='store_true', help='Run in headless mode')
    parser.add_argument('--pillars', action='store_true', help='Run in pillar mode')
    parser.add_argument('--shutdown', action='store_true', help='Shutdown after run')
    parser.add_argument('--calibrate', action='store_true', help='Disable driving and moving to next states')
    parser.add_argument('--skip-arduino', action='store_true', help='Skip Arduino connection')
    args = parser.parse_args()
    headless = args.headless
    pillars = args.pillars
    shutdown = args.shutdown
    calibrate = args.calibrate
    skip_arduino = args.skip_arduino
    
    # setup_logging() # todo reactivate
    
    # connect_arduino is now called at the start of main_program

    tasks = [asyncio.create_task(cycle_loop()), # todo reactivate
             asyncio.create_task(main_program()),
             asyncio.create_task(arduino_communication_loop())]
    if not headless:
        tasks.append(asyncio.create_task(run_webserver()))
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
        raise

def encode_image(image):
    retval, buffer = cv2.imencode('.jpg', image, [int(cv2.IMWRITE_JPEG_QUALITY), 99])
    base64_str = base64.b64encode(buffer).decode('utf-8')
    return base64_str

MAX_STEERING_ANGLE = 25.0

async def drive(speed, distance):
    # todo add gyro following
    distance_beg = car.distance
    angle_beg = car.angle
    car.speed = speed
    while (car.distance - distance_beg) < distance:
        error = (angle_beg - car.angle) / 80
        correction = error * kp + (error - last_error) * kd # todo: reactivate
        car.steering = bound(correction) * MAX_STEERING_ANGLE
        await asyncio.sleep(0.001)
    car.speed = 0  # Stop the car after driving the distance

async def turn(speed, degrees, steering):
    """   
    Args:
        speed (int): Speed of the turn.
        degrees (float): Degrees to turn.
        steering (float): Radius of the turn.
    
    Returns:
        None
    """
    angle_beg = car.angle
    # Calculate the target angle based on the current angle and degrees to turn
    direction = degrees / abs(degrees)
    car.speed = speed
    car.steering = abs(steering) * direction
    # Adjust the steering based on the radius
    while (car.angle - angle_beg) * direction < degrees * direction:
        await asyncio.sleep(0.1)
    car.speed = 0  # Stop the car after turning

async def main_program():
    # Wait for Arduino trigger before starting main logic
    if ser and not calibrate:
        await connect_to_arduino()
    speed = 300 if not pillars else 200
    print("Starting main program...")
    await drive(speed, 1000)  # Example drive command
    # await turn(speed, 90, 100)  # Example turn command
    # await turn(speed, -90, 100)  # Example turn command
    await write_serial("o\n")
    print("Main program completed. Exiting...")
    await asyncio.sleep(0.5)
    if shutdown:
        print("Shutting down the robot...")
        if ser and not calibrate:
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